"""入口：python -m brain.main（Ctrl+C 下播）。

默认数据流：网页文字/语音对话 -> 队列 -> 仲裁器 -> 陪伴人格 LLM -> TTS + 皮套 -> 记忆。
弹幕、主动导演、模拟游戏和游戏代理均为显式可选功能。
webui.enabled=true 时同时起一个 Web 测试台（见 brain/webui.py）。
"""
from __future__ import annotations

import asyncio
import itertools
import inspect
import json
import sqlite3
import sys
import time
from typing import TYPE_CHECKING

from . import config, danmaku, game
from .arbiter import Arbiter
from .autobiographical_memory import AutobiographicalMemory
from .avatar import MockAvatar
from .cognition import CognitiveBridge
from .conversation import ConversationState, VoiceRevisionWindow, is_backchannel, memory_change_owner
from .context_preparation import context_budget, prepare_context
from .context_budget import ContextBudgetError, ContextSection, assemble_context, context_sections
from .director import LiveDirector
from .events import Event
from .learning import LearningEngine
from .live_polls import LivePolls
from .tarot import Tarot
from .memory import Memory
from .metrics import RuntimeMetrics
from .persona import Persona
from .personality_evolution import PersonalityEvolution
from .playback import SpeechController
from .speech_pipeline import SpeechPipeline, SynthesisResult
from .media_narration import MediaNarrator
from .search import WebSearch
from .tts import make_tts
from .webui import WebUI
from .viewer_identity import viewer_identity
from .runtime_supervisor import RuntimeSupervisor
from .runtime_services import RuntimeServices
from .audio_retention import AudioRetention
from .broadcast_recovery import BroadcastRecovery, StandbyVoice, TurnDeliveryTracker, STANDBY_TEXT
from .rehearsal import RehearsalService

if TYPE_CHECKING:
    from .game_agent import GameAgent


async def shutdown_step(name, operation) -> None:
    """A failed optional closer must not prevent the remaining state from flushing."""
    try:
        result = operation()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        print(f'[shutdown] {name}关闭失败：{type(exc).__name__}')


async def run() -> None:
    config.load_dotenv()
    settings = config.load_settings()
    from .upgrade_release import MainReleaseGate
    release_gate = MainReleaseGate(config.ROOT)
    persona_cfg = config.load_persona()
    cognition = await CognitiveBridge.create(settings.cognition, persona_cfg)
    autobiography = AutobiographicalMemory(
        settings.selective_memory, settings.llm, persona_cfg.name
    )

    memory = Memory(settings.memory.path, settings.memory.viewer_db)
    learning = LearningEngine(
        settings.learning.state_path,
        enabled=settings.learning.enabled,
        max_episodes=settings.learning.max_episodes,
        feedback_window_sec=settings.learning.feedback_window_sec,
        consolidation_interval=settings.learning.consolidation_interval,
    )
    personality = PersonalityEvolution(
        settings.personality_evolution, persona_cfg.name
    )
    persistence = {'观众档案': memory, '互动反馈': learning,
                   '人格关系': personality, '长期记忆': autobiography}
    for store in persistence.values():
        store.enable_background_persistence()
    arbiter = Arbiter(
        settings.arbiter.danmaku_cooldown_sec,
        settings.arbiter.idle_comment_sec,
        max_pending=settings.arbiter.max_pending,
        normal_max_age_sec=settings.arbiter.normal_max_age_sec,
        aggregate_limit=settings.arbiter.aggregate_limit,
    )
    director = LiveDirector(settings.director)
    conversation = ConversationState()
    web_search = WebSearch(
        enabled=settings.search.enabled,
        max_results=settings.search.max_results,
        timeout_sec=settings.search.timeout_sec,
        cache_ttl_sec=settings.search.cache_ttl_sec,
    )
    persona = Persona(
        persona_cfg, settings.llm, memory, learning, web_search, personality,
        persona_llm=settings.persona_llm,
        topics=conversation.topics,
    )
    voice = make_tts(settings.tts)
    playback = SpeechController(settings.playback)
    metrics = RuntimeMetrics()
    avatar = MockAvatar()

    queue: asyncio.Queue = asyncio.Queue()
    live_polls = (LivePolls(config.ROOT / 'data' / 'live-polls.sqlite3')
                  if not settings.webui.public_test else None)
    tarot = (Tarot(config.ROOT / 'data' / 'tarot.sqlite3')
             if not settings.webui.public_test else None)
    tarot_events = {}
    from .task_client import TaskClient
    agent_tasks = (TaskClient(config.ROOT, settings.agent_tasks, queue)
                   if settings.agent_tasks.enabled and not settings.webui.public_test else None)
    supervisor = RuntimeSupervisor()
    finalizers: set[asyncio.Task] = set()
    mock_danmaku_enabled: asyncio.Event | None = None
    mock_game_enabled: asyncio.Event | None = None
    manual_mode = asyncio.Event()
    game_agent: GameAgent | None = None

    if settings.sources.bilibili_enabled:
        if settings.room_id <= 0:
            raise ValueError("sources.bilibili_enabled=true 时必须配置有效的 room_id")
        supervisor.start('bilibili', lambda: danmaku.blivedm_danmaku(queue, settings.room_id))
    mock_danmaku_enabled = asyncio.Event()
    if settings.sources.mock_danmaku_enabled:
        mock_danmaku_enabled.set()
    supervisor.start('mock_danmaku', lambda: danmaku.mock_danmaku(queue, mock_danmaku_enabled))

    mock_game_enabled = asyncio.Event()
    if settings.sources.mock_game_enabled:
        mock_game_enabled.set()
    supervisor.start('mock_game', lambda: game.mock_game(queue, mock_game_enabled))

    if settings.game_agent.enabled:
        # 视觉依赖只在明确启用游戏代理时加载，不影响纯聊天/皮套模式。
        from .game_agent import GameAgent

        game_agent = GameAgent(settings.game_agent, queue)
        supervisor.start('game_agent', game_agent.run, restart=False)

    services = RuntimeServices(config.ROOT, queue, public_test=settings.webui.public_test)
    if not settings.webui.public_test:
        services.configure_persona(persona, autobiography, settings.tts,
            synthesis_lock=getattr(voice, '_speak_lock', None),
            codex_command=settings.agent_tasks.codex_command,
            resource_available=lambda: not agent_tasks or not agent_tasks.resource_in_use('local_gpu'))
    webui = None
    qq_channel = services.qq_channel
    if services.trends is not None:
        director.topic_provider = services.trends.topic_seed
    if settings.webui.enabled:
        webui = WebUI(
            settings.webui,
            queue,
            settings.tts.out_dir,
            info={
                "focus": "一对一情感陪同",
                "mode": "companion",
                "bilibili": settings.sources.bilibili_enabled,
                "llm": persona.model_name if persona.model_configured else "未配置（占位回复）",
                "tts": settings.tts.backend,
                "learning": learning.summary(),
                "personality": personality.summary(),
                "search": settings.search.enabled,
                "cognition": cognition.summary(),
                "selective_memory": autobiography.summary(),
            },
            mock_danmaku_enabled=mock_danmaku_enabled,
            mock_game_enabled=mock_game_enabled,
            manual_mode=manual_mode,
            director=director,
            playback=playback,
            game_agent=game_agent,
            metrics=metrics,
            selective_memory=autobiography,
            personality=personality,
            services=services,
        )
        try:
            webui.agent_tasks = agent_tasks
            webui.release_gate = release_gate
            webui.live_polls = live_polls
            webui.tarot = tarot
            webui.runtime_supervisor = supervisor
            webui.persistence_status = lambda: {name: store.persistence_status()
                                                for name, store in persistence.items()}
            await webui.start()
        except OSError as exc:
            print(f"[webui] 启动失败（{exc}），继续无测试台运行")
            await shutdown_step('启动未完成的页面', webui.close)
            webui = None

    print(f"== {persona_cfg.name} 陪同模式 ==")
    if settings.sources.bilibili_enabled:
        print(f"   B站弹幕：已开启（房间 {settings.room_id}）")
    else:
        print("   B站弹幕：关闭")
    print(f"   模拟弹幕：{'开启' if mock_danmaku_enabled.is_set() else '关闭'}（网页可切换）")
    if game_agent:
        mode = "只观察" if settings.game_agent.observe_only else "可控（仍需测试台二次开启）"
        print(f"   游戏代理：{settings.game_agent.window_title or '未配置窗口'} · {mode}")
        print(f"   游戏急停：{settings.game_agent.emergency_stop_key.upper()}")
    else:
        print("   当前方向：带皮套与语音的一对一情感陪同")
    print(f"   游戏模拟：{'开启' if mock_game_enabled.is_set() else '关闭'}（网页可切换）")
    print(f"   主动导演：{'开启' if settings.director.enabled else '关闭'}")
    print(f"   LLM：{persona.model_name if persona.model_configured else '未配置（占位回复）'}")
    print(f"   TTS：{settings.tts.backend}")
    print(
        f"   可塑人格：{'已启用' if settings.personality_evolution.enabled else '关闭'}"
        f" · {personality.summary()['profile']} · {personality.summary()['snapshots']} 次固化"
    )
    print(f"   持久认知：{'已启用' if cognition.enabled else '关闭或降级'}")
    print(
        f"   时序图记忆：{'已启用' if settings.selective_memory.enabled and settings.selective_memory.temporal_graph else '关闭'}"
        f" · {autobiography.summary()['active']} 条"
    )

    ids = itertools.count(1)

    async def push_event(payload: dict) -> None:
        if webui:
            await webui.broadcast(payload)

    recovery = BroadcastRecovery(failure_limit=settings.broadcast.failure_limit,
                                 cooldown_sec=settings.broadcast.cooldown_sec)
    question_delivery = TurnDeliveryTracker()
    active_turns: set[int] = set()
    failed_turns: set[int] = set()
    cancelled_questions: set[int] = set()
    speech_pipeline = SpeechPipeline(voice, playback, push_event, metrics,
                                    synthesis_timeout_sec=settings.broadcast.synthesis_timeout_sec,
                                    wait_notice_sec=settings.broadcast.wait_notice_sec)
    speech_pipeline.expect_audio = settings.tts.backend != 'mock'

    async def interrupt_media(event):
        await register_event(event, turn_id=event.ui_id)

    def media_voice_resource_reason():
        if not services.voice_available():
            return '语音服务尚未就绪，文字聊天仍可使用，请稍后再播放。'
        if speech_pipeline.resource_available():
            return None
        if agent_tasks and settings.tts.backend != 'mock':
            return (agent_tasks.resource_block_reason('local_gpu')
                    or '语音尚未取得显卡授权，请在启动中心检查后台任务服务，等授权恢复后重试。')
        return '语音资源暂不可用，请在启动中心检查语音和后台任务服务后重试。'

    from .bilibili_review import make_video_review

    media_narrator = MediaNarrator(allocate_id=lambda: next(ids), pipeline=speech_pipeline,
        push=push_event, interrupt=interrupt_media,
        timeout_sec=max(1, settings.broadcast.synthesis_timeout_sec) + 35,
        resource_check=media_voice_resource_reason,
        reviewer=lambda evidence: make_video_review(persona, evidence))
    from pathlib import Path
    Path(settings.tts.out_dir).mkdir(parents=True, exist_ok=True)
    standby = StandbyVoice(settings.tts.out_dir, settings.tts)
    standby_path = standby.available()
    audio_retention = AudioRetention(
        settings.tts.out_dir, enabled=settings.tts.audio_retention_enabled,
        max_age_days=settings.tts.audio_retention_days,
        max_bytes=settings.tts.audio_retention_max_mib * 1024 * 1024,
        reference_paths=(settings.tts.ref_audio_path, settings.tts.indextts2_ref_audio_path,
                         settings.tts.indextts2_short_ref_audio_path, settings.tts.fish_speech_ref_audio_path))
    if webui:
        webui.audio_retention = audio_retention

    def protect_audio() -> None:
        items = list(playback.queue)
        if playback.current:
            items.append(playback.current)
        names = [item.url.rsplit('/', 1)[-1] for item in items]
        names.extend(speech_pipeline.pending_audio_paths())
        audio_retention.set_protected(names)

    async def maintain_audio() -> None:
        while True:
            await asyncio.sleep(max(10.0, settings.tts.audio_retention_interval_sec))
            protect_audio()
            await audio_retention.cleanup()
    if agent_tasks:
        playback.item_valid = agent_tasks.speech_valid
        playback.on_dispatch = agent_tasks.speech_dispatched
    interrupt_generation = asyncio.Event()
    responding = asyncio.Event()
    if webui:
        webui.conversation_status = lambda: {
            'generating': responding.is_set(), 'generating_turn_ids': sorted(active_turns),
            'synthesizing_turn_ids': sorted(speech_pipeline.pending_turn_ids()),
        }
        webui.cognition_delivery = cognition.delivery_snapshot
    if agent_tasks:
        # Reserve before game input is enabled. The separate executor owns grants.
        agent_tasks.resource_busy = lambda: {
            **({'local_gpu': '主服务语音已启用，本地显卡任务等待下播'}
               if settings.tts.backend != 'mock' else {}),
            **({'desktop': '主服务游戏代理已启用，桌面任务等待关闭主服务'}
               if game_agent and game_agent.cfg.enabled else {}),
        }
        await agent_tasks.start()
        if settings.tts.backend != 'mock':
            speech_pipeline.resource_available = lambda: not agent_tasks.resource_in_use('local_gpu')
        if game_agent:
            game_agent.input_blocked = lambda: agent_tasks.resource_in_use('desktop')
    voice_revisions = VoiceRevisionWindow()

    def track_finalizer(task: asyncio.Task) -> None:
        finalizers.add(task)

        def finished(done: asyncio.Task) -> None:
            finalizers.discard(done)
            if not done.cancelled():
                error = done.exception()  # retrieve failures so asyncio does not emit an orphan warning
                if error is not None:
                    print(f"[background] 后台收尾失败：{type(error).__name__}: {error}")

        task.add_done_callback(finished)

    def audit_delivery(item, completed, reason=''):
        if item.event.get('tarot_reading_id'):
            if not completed and tarot:
                tarot.mark_result(item.event, 'interrupted', error='解读播放未完成，可用原牌重试。')
            return
        if item.event.get('rehearsal_id') or item.event.get('is_fallback') or item.event.get('media_narration_id'):
            return
        turn_id = item.turn_id if item.turn_id is not None else item.id
        track_finalizer(asyncio.create_task(cognition.record_delivery(
            turn_id, item.segment_index, completed, reason)))

    def speech_state(item, state, reason=''):
        if item.event.get('tarot_reading_id') and state == 'playing' and tarot:
            tarot.note_playback_started(item.event, item.text, item.segment_index)
        if item.event.get('media_narration_id'):
            media_narrator.state(item, state, reason)
            return
        if agent_tasks:
            agent_tasks.speech_state(item, state, reason)
        if state in {'failed', 'cancelled', 'expired', 'dropped'}:
            audit_delivery(item, False, reason or state)
            companion = getattr(services, 'companion', None)
            if companion is not None:
                track_finalizer(asyncio.create_task(companion.played(item.turn_id, item.segment_index, False)))
        elif state == 'played' and not (playback.state == 'playing'
                or playback.paused and playback._resume_state == 'playing'):
            audit_delivery(item, False, 'missing_playback_start')

    playback.on_state = speech_state

    async def broadcast_notice(text, *, turn_id=None, duration_sec=12):
        await push_event({'type': 'broadcast_notice', 'text': text,
                          'turn_id': turn_id, 'duration_sec': duration_sec})

    async def reply_failed(event, stage):
        if getattr(event, 'tarot_reading_id', '') and tarot:
            tarot.mark_result(event, 'failed', error='解读未完成，可用原牌重试。')
        if (getattr(event, 'rehearsal_id', None) or getattr(event, 'is_fallback', False)
                or getattr(event, 'media_narration_id', None)):
            return
        recovery.failure(stage, event.ui_id)
        companion = getattr(services, 'companion', None)
        if companion is not None:
            await companion.abandon(event, outcome='failed')
        failed_turns.add(event.ui_id)
        question_delivery.cancel(event.ui_id, '回应失败或超时，请稍后重新回答')
        await speech_pipeline.cancel_turn(event.ui_id)
        if not recovery.claim_notice():
            return
        await broadcast_notice('回应暂时没有完成，请稍等片刻。', turn_id=event.ui_id, duration_sec=20)
        # Explicitly prepared with this voice; failure never switches voice backends.
        clip = await asyncio.to_thread(standby.available) if standby_path else None
        if clip and not playback.paused:
            notice = Event('system', 'ambient', STANDBY_TEXT)
            notice.ui_id = next(ids)
            notice.is_fallback = True
            speech_pipeline.enqueue(turn_id=notice.ui_id, segment_index=0,
                                    text=STANDBY_TEXT, emotion='平静', actions=[], event=notice,
                                    prepared_audio_path=clip)

    def synthesis_failed(event, reason):
        if getattr(event, 'media_narration_id', None):
            return  # The media workflow reports failure; never queue a standby reply.
        if agent_tasks:
            agent_tasks.synthesis_failed(event, reason)
        question_delivery.cancel(event.ui_id, '语音未完成，请稍后重新回答')
        if not getattr(event, 'rehearsal_id', None) and not getattr(event, 'is_fallback', False):
            failed_turns.add(event.ui_id)
            recovery.failure('tts', event.ui_id)
            track_finalizer(asyncio.create_task(reply_failed(event, 'tts')))

    def synthesized(event, path):
        if path and not getattr(event, 'is_fallback', False) and not getattr(event, 'media_narration_id', None):
            recovery.success('tts')

    async def synthesis_wait(event):
        if not getattr(event, 'rehearsal_id', None) and not getattr(event, 'media_narration_id', None):
            await broadcast_notice('声音还在准备，请稍等一下。', turn_id=event.ui_id)

    speech_pipeline.on_failure = synthesis_failed
    speech_pipeline.on_synthesized = synthesized
    speech_pipeline.on_wait = synthesis_wait

    def companion_event_current(event):
        field = event.get if isinstance(event, dict) else lambda name, default=None: getattr(event, name, default)
        if field('tarot_reading_id'):
            return bool(tarot and tarot.valid_event(event))
        if not field('companion_followup_token') and field('kind') != 'followup':
            return True
        companion = getattr(services, 'companion', None)
        allowed = getattr(services, 'private_followups_available', None)
        return bool(companion is not None and callable(allowed) and allowed()
                    and companion.followup_current(event))

    async def cancel_invalid_private_followups():
        companion = getattr(services, 'companion', None)
        if companion is None:
            return
        for event in companion.pending_followup_events():
            if companion_event_current(event):
                continue
            turn = getattr(event, 'ui_id', None)
            if type(turn) is int:
                cancelled_questions.add(turn)
                arbiter.remove_turn(turn)
                await speech_pipeline.cancel_turn(turn)
            await companion.abandon(event)

    if webui:
        webui.on_private_followups_blocked = cancel_invalid_private_followups

    async def cancel_invalid_tarot():
        for turn, event in list(tarot_events.items()):
            if companion_event_current(event):
                continue
            cancelled_questions.add(turn)
            arbiter.remove_turn(turn)
            await speech_pipeline.cancel_turn(turn)
            tarot_events.pop(turn, None)

    if webui:
        webui.on_tarot_cancel = cancel_invalid_tarot

    speech_pipeline.event_valid = lambda event: (
        event.ui_id not in failed_turns and event.ui_id not in cancelled_questions
        and companion_event_current(event)
        and director.program_event_current(event)
        and (not agent_tasks or agent_tasks.receipt_valid(event)))
    playback.item_valid = lambda item: (
        item.turn_id not in failed_turns and item.turn_id not in cancelled_questions
        and companion_event_current(item.event)
        and director.program_event_current(item.event)
        and (not agent_tasks or agent_tasks.speech_valid(item)))

    def upgrade_idle():
        return not (media_narrator.active or active_turns or responding.is_set()
                    or (qq_channel and qq_channel.busy)
                    or speech_pipeline.busy or playback.current or playback.queue
                    or not queue.empty() or arbiter.pending_turn_ids() or recovery.preparing
                    or (webui and (webui._bilibili_flows or webui._song_flows
                                   or (webui.rehearsal and webui.rehearsal.running)))
                    or (game_agent and (game_agent.control_enabled or game_agent.input_busy)))

    services.idle = upgrade_idle
    if webui:
        webui.upgrade_idle = upgrade_idle

    async def wait_for_upgrade():
        while not release_gate.stop_requested(idle=upgrade_idle()):
            await asyncio.sleep(.25)
        if webui:
            webui.runtime_started = False

    def voice_available():
        if not services.voice_available():
            raise ValueError('语音服务尚未就绪，文字聊天仍可使用')
        if media_narrator.active or active_turns or responding.is_set() or speech_pipeline.busy or playback.current or playback.queue:
            raise ValueError('当前有正式内容正在生成或播放，请等本轮结束')
        if playback.paused:
            raise ValueError('播放已暂停，请先恢复播放')
        if not webui or not webui._sockets:
            raise ValueError('请先打开并连接主控或 OBS 播放页')
        if recovery.preparing or (webui.rehearsal and webui.rehearsal.running):
            raise ValueError('正在演练或准备提示音，请等本轮结束')
        if not speech_pipeline.resource_available():
            raise ValueError('语音资源正在被占用，请稍后再试')
        return True

    async def prepare_standby():
        nonlocal standby_path
        if settings.tts.backend == 'mock':
            raise ValueError('请先配置真实语音后端')
        voice_available()
        recovery.preparing, recovery.prepare_error = True, ''
        async def prepare():
            nonlocal standby_path
            try:
                await asyncio.wait_for(standby.prepare(voice), max(1, settings.broadcast.synthesis_timeout_sec))
                standby_path = standby.available()
            except Exception as error:
                recovery.prepare_error = type(error).__name__ + '：提示音未准备成功，请检查语音服务后重试'
            finally:
                recovery.preparing = False
        track_finalizer(asyncio.create_task(prepare()))

    async def rehearse(report):
        probe = Persona(persona_cfg, settings.llm, memory=None,
                        persona_llm=settings.persona_llm)
        event = Event('system', 'ambient', '这是开播前的语音测试，请用一句简短的话向主播打招呼。', forced=True)
        event.ui_id = next(ids)
        speech_pipeline.begin_turn(event, event.ui_id)
        event.rehearsal_id = report.run_id
        report.bind_turn(event.ui_id)
        try:
            report('llm', 'running', '正在请求当前配置的语言模型')
            async def generate_probe():
                return [segment async for segment in probe.stream_reply(event)]
            try:
                segments = await asyncio.wait_for(generate_probe(), max(1, settings.broadcast.reply_timeout_sec))
            except asyncio.TimeoutError:
                report('llm', 'failed', '当前模型请求超时')
                recovery.failure('llm', event.ui_id)
                raise
            if probe.last_mode != 'llm' or not segments:
                report('llm', 'failed', '未获得真实模型的完整回复')
                recovery.failure('llm', event.ui_id)
                raise ValueError('模型测试未通过')
            recovery.success('llm')
            report('llm', 'passed', '当前模型已返回测试回复')
            report('tts', 'running', '正在使用当前音色生成测试语音')
            result = await speech_pipeline.enqueue(
                turn_id=event.ui_id, segment_index=0, text=''.join(s.text for s in segments),
                emotion=probe.last_emotion, actions=[], event=event)
            if result.cancelled or not result.audio_path:
                report('tts', 'failed', '未得到可播放的测试语音')
                recovery.failure('tts', event.ui_id)
                raise ValueError('语音测试未通过')
            report('tts', 'passed', '当前音色已生成测试语音')
            await report.wait_for_playback()
        finally:
            await speech_pipeline.cancel_turn(event.ui_id)
            await probe.close()

    if webui and not settings.webui.public_test:
        webui.rehearsal = RehearsalService(
            rehearse, can_start=voice_available,
            danmaku_status=lambda: {**danmaku.BILIBILI_STATUS, 'enabled': settings.sources.bilibili_enabled},
            timeout_sec=max(1, settings.broadcast.reply_timeout_sec) + max(1, settings.broadcast.synthesis_timeout_sec) + 35)
        webui.broadcast_status = lambda: {**recovery.summary(), 'standby_ready': bool(standby_path)}
        webui.on_prepare_standby = prepare_standby

    async def register_event(event, *, turn_id=None) -> None:
        """Normalize one incoming event and put it into the attention buffer."""
        tarot_turn = bool(getattr(event, 'tarot_reading_id', ''))
        if tarot_turn and not companion_event_current(event):
            return
        if manual_mode.is_set() and not event.forced and not event.task_id:
            arbiter.discard_unforced()
            return
        event.ui_id = next(ids) if turn_id is None else turn_id
        if tarot_turn:
            await cancel_invalid_tarot()
            tarot_events[event.ui_id] = event
        if (event.source in {'companion', 'voice'} and event.kind in {'chat', 'interrupt'}
                and not event.agent_reply and not event.task_id
                and not getattr(event, 'media_narration_id', None)):
            services.foreground('voice' if event.source == 'voice' else 'web')
        host_question = bool(getattr(event, 'host_question_id', None))
        if (event.source in {'companion', 'voice'} and event.kind in {'chat', 'interrupt'}
                and not getattr(event, 'media_narration_id', None)):
            await media_narrator.cancel('interrupted')
        if event.corrects is not None:
            if not voice_revisions.accepts(event):
                await push_event({"type": "correction_rejected", "text": "只能修改当前会话中最近一次语音；请作为新消息发送。"})
                return
        if event.confirm_memory:
            if responding.is_set() or playback.current or speech_pipeline.busy:
                await push_event({'type':'memory_confirmation', 'ok':False,
                                  'text':'请等这一轮回答结束，再确认保存。'})
                return
            original = voice_revisions.latest
            if original is None:
                return
            pending = list(voice_revisions.pending) or [(original, '', '平静')]
            for old_event, text, emotion in pending:
                await persist_delivery(old_event, text, emotion)
            personality.observe_event(original)
            voice_revisions.pending.clear()
            voice_revisions.latest = None
            companion = getattr(services, 'companion', None)
            companion_receipt = None
            if companion is not None:
                try:
                    companion_receipt = await companion.confirm_voice(original)
                except (ValueError, OSError):
                    companion.last_error = '已确认语音的陪伴记录未能保存，请检查陪伴能力页。'
                    companion_receipt = {'status': 'failed'}
            companion_notice = ''
            if companion_receipt:
                companion_notice = ('日程已安排。' if companion_receipt.get('status') == 'created' else
                                    '日程操作未完成，请到陪伴能力页检查。' if companion_receipt.get('status') == 'failed' else
                                    '时间尚不明确，日程未安排。' if companion_receipt.get('status') == 'needs_time' else
                                    '日程操作已处理。')
            await push_event({'type':'memory_confirmation', 'ok':True, 'id':event.corrects,
                              'text':'已按记忆筛选规则处理并保存；这条语音的纠错窗口已关闭。' + companion_notice})
            return
        if (not host_question and event.source in {"voice", "companion"} and event.kind == "chat"
                and event.corrects is None
                and is_backchannel(event.text)
                and not (agent_tasks and agent_tasks.intent_prompts.get(agent_tasks.requester(event)))
                and (responding.is_set() or playback.current or speech_pipeline.busy)):
            if playback.paused and (event.resume_playback or event.text.strip() in {"继续", "你继续", "接着说"}):
                for message in playback.set_paused(False):
                    await push_event(message)
            await push_event({"type": "backchannel", "text": event.text})
            return
        if event.source in {"companion", "voice"} and event.kind in {"chat", "interrupt"} and not host_question:
            if tarot and tarot.pause_current():
                await cancel_invalid_tarot()
            if webui and webui.rehearsal and webui.rehearsal.running:
                await webui.rehearsal.cancel(webui.rehearsal.snapshot()['run_id'])
            event.forced = True
            interrupt_generation.set()
            unfinished = playback.current or next(iter(playback.queue), None)
            if (unfinished is not None and not unfinished.event.get('media_narration_id')
                    and not unfinished.event.get('tarot_reading_id')):
                interrupted = Event(unfinished.event['source'], unfinished.event['kind'],
                                    unfinished.event['text'], user=unfinished.event.get('user'),
                                    viewer_key=unfinished.event.get('viewer_key', ''),
                                    ts=unfinished.event.get('ts', unfinished.created_at))
                interrupted.ui_id = unfinished.turn_id
                conversation.mark_interrupted(interrupted)
            for superseded in arbiter.discard_before(event.ui_id):
                metrics.record_cancelled(superseded.ui_id)
                if tarot and getattr(superseded, 'tarot_reading_id', ''):
                    tarot.mark_result(superseded, 'interrupted', error='解读被新的主控对话打断，可用原牌重试。')
            await speech_pipeline.interrupt_before(event.ui_id)
            if event.kind == "interrupt":
                return
            if event.resume_playback and playback.paused:
                for message in playback.set_paused(False):
                    await push_event(message)
        if event.corrects is not None:
            companion = getattr(services, 'companion', None)
            if companion is not None:
                await companion.discard_voice(event.corrects)
            conversation.retract_turn(event.corrects)
            persona.retract_turn(event.corrects)
            director.clear_conversation_topic(viewer_identity(event))
            if event.retract:
                voice_revisions.advance(event)
                await push_event({"type": "voice_retracted", "id": event.corrects})
                return
        if event.source in {"voice", "companion"} and event.kind == "chat" and not host_question:
            previous_voice = voice_revisions.latest
            companion = getattr(services, 'companion', None)
            accepted_voice = voice_revisions.advance(event)
            confirmed_voice_ids = set()
            for old_event, text, emotion in accepted_voice:
                if old_event.ui_id not in confirmed_voice_ids:
                    personality.observe_event(old_event)
                    confirmed_voice_ids.add(old_event.ui_id)
                track_finalizer(asyncio.create_task(persist_delivery(old_event, text, emotion)))
            if previous_voice is not None and event.corrects is None and companion is not None:
                try:
                    receipt = await companion.confirm_voice(previous_voice)
                except (ValueError, OSError):
                    companion.last_error = '已确认语音的陪伴记录未能保存，请检查陪伴能力页。'
                    receipt = {'status': 'failed', 'reply': '上一轮语音的陪伴操作未完成，不能声称日程已安排。'}
                if receipt:
                    event.companion_prior_receipt = ('上一轮语音现已确认；以下是实际本机日程结果：'
                                                     + json.dumps(receipt, ensure_ascii=False)[:2400])
        metrics.record_event(event.ui_id)
        companion = getattr(services, 'companion', None)
        if companion is not None and not tarot_turn:
            await companion.registered(event)
        private_followup = bool(getattr(event, 'companion_followup_token', ''))
        if agent_tasks and not host_question and not private_followup and not tarot_turn:
            await agent_tasks.handle_event(event)
        if not private_followup and not tarot_turn:
            if event.user:
                memory.touch_viewer(event)
            director.observe_event(event)
            conversation.observe_event(event)
            director.sync_conversation_topic(event, conversation.topics.inspect(event))
            learning.observe_event(event)
            if event.source != "voice":
                personality.observe_event(event)
            event.priority += learning.priority_bonus(event)
        arbiter.push(event)
        await push_event({
            "type": "event", "id": event.ui_id, "source": event.source,
            "kind": event.kind, "user": event.user, "text": event.text,
            "priority": event.priority,
            "corrects": event.corrects,
            "task_id": event.task_id,
            **(webui.question_event_payload(event) if webui else {}),
        })
        await push_event({"type": "arbiter", **arbiter.summary()})

    async def ask_question(card):
        if recovery.preparing or (webui.rehearsal and webui.rehearsal.running):
            raise ValueError('请先结束开播演练或提示音准备')
        origin = card['origin']
        event = Event(origin['source'], 'chat', card['text'], user=origin.get('user'),
                      viewer_key=origin.get('viewer_key', ''), forced=True)
        event.host_question_id, event.host_question_attempt = card['id'], card['attempt']
        turn_id = next(ids)
        webui._question_store().bind_turn(card['id'], card['attempt'], turn_id)
        question_delivery.begin(turn_id, lambda complete, reason: webui._question_store().finish_answer(
            card['id'], card['attempt'], completed=complete, reason=reason))
        try:
            await register_event(event, turn_id=turn_id)
        except BaseException:
            arbiter.remove_turn(turn_id)
            question_delivery.cancel(turn_id, '问题调度未完成')
            raise
        return turn_id

    async def cancel_question(card):
        turn_id = card.get('turn_id')
        if type(turn_id) is int:
            cancelled_questions.add(turn_id)
            arbiter.remove_turn(turn_id)
            question_delivery.cancel(turn_id, '主播停止了本次回答')
            await speech_pipeline.cancel_turn(turn_id)

    if webui and not settings.webui.public_test:
        webui.on_bilibili_narrate = media_narrator.narrate
        webui.bilibili_review_enabled = True
        webui.on_question_ask = ask_question
        webui.on_question_cancel = cancel_question

    async def ingest_events() -> None:
        from brain.tarot_runtime import holds_audience_event
        last_song_request = float('-inf')
        while True:
            event = await queue.get()
            try:
                vote = None
                if live_polls:
                    try:
                        vote = live_polls.handle_event(event)
                    except (OSError, sqlite3.Error):
                        print('[polls] 投票存储暂不可用，本条投票未确认计入')
                        vote = {'ok': False}
                if event.source == 'danmaku' and webui:
                    await webui.broadcast_danmaku({'user':event.user or '观众', 'text':event.text,
                        'kind':event.kind, 'ts':event.ts})
                if vote is not None:
                    director.observe_poll_vote(event, vote)
                    continue  # Count all votes before attention filtering; no per-vote TTS.
                if tarot:
                    try:
                        tarot_receipt = tarot.handle_event(event)
                    except (OSError, sqlite3.Error):
                        tarot_receipt = {'ok': False}
                        print('[tarot] 塔罗报名或抽牌未能保存，请在栏目页核对。')
                    # Ending a round may deliberately return None to route an
                    # urgent support message onward. Stop any stale voice too.
                    await cancel_invalid_tarot()
                    if tarot_receipt is not None:
                        continue  # The tea-party scheduler owns any next reply.
                    if holds_audience_event(tarot, event):
                        continue  # Keep this guest's reply window and spoken turn together.
                if event.kind == 'chat' and event.source in {'danmaku', 'companion', 'voice'}:
                    if event.retract or event.confirm_memory:
                        # Voice controls resend the original text. They must
                        # reach the control handler without replaying its song.
                        await register_event(event)
                        continue
                    from .music_requests import parse_music_request
                    from .songs import catalog, match_songs, parse_song_command
                    music_request = parse_music_request(event.text)
                    song_command = parse_song_command(event.text)
                    if (music_request is not None or song_command is not None) and settings.webui.public_test:
                        await push_event({'type': 'music_status', 'reason': 'public_test',
                                          'text': '公开测试模式不操作正式曲库、B站播放器或音乐制作任务，请在本机控制台使用。'})
                        continue
                    if (music_request is not None or song_command is not None) and not webui:
                        print('[music] 播放和音乐制作需要启用 WebUI 控制台。')
                        await push_event({'type': 'music_status', 'reason': 'unavailable',
                                          'text': '播放和音乐制作需要启用 WebUI 控制台。'})
                        continue
                    if music_request is not None:
                        mode, title = music_request
                        cached = next((j for j in reversed(webui.music_requests.items)
                            if j['mode'] == mode and j['query'].casefold() == title.casefold()
                            and j['state'] == 'ready' and j.get('song')), None)
                        if cached and time.monotonic() - last_song_request >= 10:
                            last_song_request = time.monotonic()
                            await push_event({'type':'song_request', 'song':cached['song']})
                            await push_event({'type':'music_status', 'text':'已将缓存翻唱加入播放队列：' + title})
                            continue
                        if cached:
                            await push_event({'type':'music_status', 'reason': 'cooldown', 'text':'点歌太快了，请稍后再试。'})
                            continue
                        try:
                            notice = webui.music_requests.submit(*music_request,
                                                                 event.viewer_key or event.user or event.source)
                        except OSError:
                            notice = '音乐任务未能保存，请稍后检查任务面板并重试。'
                        await push_event({'type': 'music_status', 'text': notice})
                        continue  # A production request must never become an invented LLM action.
                    if song_command is not None:
                        mode, query = song_command['mode'], song_command['query']
                        if mode in {'stop', 'next', 'open'}:
                            await push_event({'type': 'song_control', 'action': mode})
                            continue
                        if mode == 'bilibili' and not query:
                            await push_event({'type': 'music_status', 'reason': 'missing_query',
                                              'text': '请发送“B站点歌 歌名”、BV号或B站视频链接。'})
                            continue
                        if mode == 'local':
                            from .capabilities import unsupported_media_reply
                            unsupported = unsupported_media_reply(event.text)
                            if unsupported:
                                await push_event({'type': 'music_status', 'reason': 'unsupported', 'text': unsupported})
                                continue
                            try:
                                songs = await asyncio.to_thread(catalog, config.ROOT / 'data' / 'songs')
                            except OSError:
                                await push_event({'type': 'music_status', 'reason': 'catalog_unavailable',
                                                  'text': '本地曲库暂时无法读取，本次没有播放，请稍后重试。'})
                                continue
                            matches = match_songs(songs, query)
                            if not matches:
                                await push_event({'type': 'music_status', 'reason': 'not_found',
                                                  'text': '已查询本地曲库，没有匹配的歌曲：' + query[:120]
                                                          + '。可以发送“B站点歌 ' + query[:120] + '”查询B站。'})
                                continue
                            if len(matches) > 1:
                                titles = '、'.join(song['title'][:80] for song in matches[:5])
                                await push_event({'type': 'music_status', 'reason': 'ambiguous',
                                                  'text': '本地曲库匹配到多首，请指定完整歌名：' + titles
                                                          + ('等' if len(matches) > 5 else '')})
                                continue
                        now = time.monotonic()
                        if now - last_song_request < 10:
                            await push_event({'type': 'music_status', 'reason': 'cooldown',
                                              'text': '点歌太快了，请稍后再试。'})
                            continue
                        last_song_request = now
                        if mode == 'bilibili':
                            await push_event({'type': 'bilibili_request', 'query': query})
                        else:
                            await push_event({'type': 'song_request', 'song': matches[0]})
                            await push_event({'type': 'music_status', 'reason': 'queued',
                                              'text': '已加入本地播放队列：' + matches[0]['title']})
                        continue  # Recognized commands are consumed even when no song can play.
                await register_event(event)
            finally:
                queue.task_done()

    async def maintenance_loop() -> None:
        learning_tick = 0
        while True:
            due = time.perf_counter() + .25
            await asyncio.sleep(0.25)
            metrics.record_stage('event_loop_lag', time.perf_counter() - due)
            protect_audio()
            await cancel_invalid_private_followups()
            for message in playback.tick():
                await push_event(message)
            if agent_tasks:
                agent_tasks.notification_tick()
            learning_tick += 1
            if learning_tick >= 4:
                if webui and not settings.webui.public_test:
                    await webui.refresh_program_context()
                alive = arbiter.pending_turn_ids() | active_turns | speech_pipeline.pending_turn_ids()
                alive.update(item.turn_id for item in playback.queue)
                if playback.current:
                    alive.add(playback.current.turn_id)
                for turn_id in tuple(question_delivery.turns):
                    if turn_id not in alive:
                        question_delivery.cancel(turn_id, '回答已过期或未完整播放，请重新回答')
                question_delivery.retry()
                failed_turns.intersection_update(alive)
                cancelled_questions.intersection_update(alive)
                if live_polls:
                    try:
                        live_polls.tick()
                    except (OSError, sqlite3.Error):
                        print('[polls] 投票存储暂不可用，等待下一次检查')
                learning.tick()
                learning_tick = 0
                await push_event({"type": "metrics", **metrics.summary()})

    async def push_game_agent_status() -> None:
        """低频推送识别、规划和安全状态；游戏帧本身由独立 HTTP 端点读取。"""
        while True:
            await asyncio.sleep(1.0)
            if webui and game_agent:
                await webui.broadcast({"type": "game_agent", **game_agent.summary()})

    if game_agent:
        supervisor.start('game_status', push_game_agent_status, critical=False)

    async def finalize_cognition(
        cognitive_turn,
        event,
        *,
        emotion: str,
        text: str,
        actions: list[str],
        response_mode: str,
        response_latency: float,
        synthesis: list[asyncio.Future[SynthesisResult]],
        learning_episode_id: int | None,
    ) -> None:
        results = await asyncio.gather(*synthesis, return_exceptions=True)
        audio_paths: list[str] = []
        playback_messages: list[dict] = []
        synthesis_failed = False
        for result in results:
            if not isinstance(result, SynthesisResult) or result.cancelled:
                synthesis_failed = True
                question_delivery.cancel(event.ui_id, '回答语音未全部生成或已取消')
                continue
            if result.audio_path:
                audio_paths.append(result.audio_path)
            else:
                synthesis_failed = synthesis_failed or settings.tts.backend != 'mock'
                question_delivery.cancel(event.ui_id, '回答没有可播放的语音')
            playback_messages.extend(result.playback_messages)
        try:
            await cognition.complete_turn(
                cognitive_turn,
                event,
                emotion=emotion,
                text=text,
                avatar_actions=actions,
                response_mode=response_mode,
                response_latency=response_latency,
                tts_backend=settings.tts.backend,
                audio_path="; ".join(audio_paths) or None,
                playback_messages=playback_messages,
                learning_episode_id=learning_episode_id,
                expected_segments=len(synthesis),
                synthesis_failed=synthesis_failed,
            )
        finally:
            if not audio_paths:
                metrics.record_no_audio(event.ui_id)

    async def consolidate_autobiography(event, text: str, emotion: str) -> None:
        stored = await autobiography.consolidate(event, text, emotion)
        personality.absorb_memories(event, stored)
        await push_event({
            "type": "selective_memory",
            "stored_this_turn": len(stored),
            **autobiography.summary(),
        })
        await push_event({"type": "personality", **personality.summary()})

    async def process_event(event) -> None:
        # Clearing speech invalidates this whole stream, including its first
        # sentence if the model has not produced one yet.
        speech_pipeline.begin_turn(event, event.ui_id)
        if not director.program_event_current(event):
            active_turns.discard(event.ui_id)
            return
        if agent_tasks and event.task_id:
            try:
                await agent_tasks.rpc('status')
            except RuntimeError:
                active_turns.discard(event.ui_id)
                return
        if agent_tasks and not agent_tasks.refresh_receipt(event):
            active_turns.discard(event.ui_id)
            return
        responding.set()
        async def wait_notice():
            await asyncio.sleep(max(.1, settings.broadcast.wait_notice_sec))
            if not playback.current or playback.current.turn_id != event.ui_id:
                await broadcast_notice('这条回复还在准备，请稍等一下。', turn_id=event.ui_id)
        notice_task = asyncio.create_task(wait_notice())
        async def respond():
            if agent_tasks and not getattr(event, 'host_question_id', None):
                await agent_tasks.resolve_task_intent(event)
            await generate_event(event)
        try:
            await asyncio.wait_for(respond(), max(1, settings.broadcast.reply_timeout_sec))
        except asyncio.TimeoutError:
            metrics.record_error(event.ui_id)
            await reply_failed(event, 'llm')
            await push_event({'type': 'reply_error', 'id': event.ui_id, 'error': '回复等待超时，请稍后重试'})
        except asyncio.CancelledError:
            metrics.record_cancelled(event.ui_id)
            if getattr(event, 'tarot_reading_id', '') and tarot:
                tarot.mark_result(event, 'interrupted', error='本次解读已中断，可用原牌重试。')
            companion = getattr(services, 'companion', None)
            if companion is not None:
                await companion.abandon(event)
            if not getattr(event, 'tarot_reading_id', ''):
                conversation.mark_interrupted(event)
            question_delivery.cancel(event.ui_id, '回答被中断，请重新回答')
            await speech_pipeline.cancel_turn(event.ui_id)
            await push_event({"type": "reply_cancelled", "id": event.ui_id})
            raise
        except ContextBudgetError as exc:
            metrics.record_error(event.ui_id)
            await reply_failed(event, 'llm')
            await push_event({'type': 'reply_error', 'id': event.ui_id, 'error': str(exc)})
        except Exception:
            metrics.record_error(event.ui_id)
            await reply_failed(event, 'llm')
            await push_event({'type': 'reply_error', 'id': event.ui_id, 'error': '本次回复未完成，请稍后重试'})
        finally:
            notice_task.cancel()
            await asyncio.gather(notice_task, return_exceptions=True)
            active_turns.discard(event.ui_id)
            responding.clear()

    async def generate_event(event) -> None:
        companion = getattr(services, 'companion', None)
        private_followup = bool(getattr(event, 'companion_followup_token', ''))
        tarot_turn = bool(getattr(event, 'tarot_reading_id', ''))
        if not companion_event_current(event):
            if companion is not None:
                await companion.abandon(event)
            return
        if companion is not None and not await companion.valid_followup(event):
            return
        started = time.perf_counter()
        event.text_only_delivery = settings.tts.backend != 'mock' and not services.voice_available()
        if not private_followup and not tarot_turn:
            director.prepare(event)
        director_turn = director.summary()
        await push_event({"type": "director", **director_turn})
        await push_event({"type": "thinking", "id": event.ui_id})
        if event.text_only_delivery:
            await broadcast_notice('语音服务尚未就绪，本轮先用文字回复。', turn_id=event.ui_id)

        short_context = '' if tarot_turn else conversation.context_for(event)
        timeout = context_budget(settings.selective_memory.context_timeout_sec)
        memory_context, preparation = await prepare_context(
            event, autobiography, cognition, timeout=timeout)
        for stage, detail in preparation.items():
            metrics.record_stage(stage, detail['seconds'])
        extra_context = assemble_context(
            ContextSection('memory_correction', '' if tarot_turn else conversation.correction_context_for(event),
                           priority=110, required=True),
            ContextSection('recent_conversation', short_context, priority=90),
            *context_sections(memory_context),
            ContextSection('host_task_receipts', agent_tasks.context_for(event) if agent_tasks and not tarot_turn else '',
                           priority=100, required=True),
            ContextSection('bilibili_experience', services.bilibili_activity.context_for(event)
                           if services.bilibili_activity and not tarot_turn else '', priority=30),
            ContextSection('companion_features', companion.context_for(event) if companion and not tarot_turn else '',
                           priority=100, required=True),
            ContextSection('current_public_topics', services.topic_context(event) if not tarot_turn else '',
                           priority=85),
        )
        audit_started = time.perf_counter()
        try:
            cognitive_turn = (None if tarot_turn else
                              await asyncio.wait_for(cognition.begin_turn(event, director_turn), timeout))
            audit_status = 'ready' if cognitive_turn is not None else 'unavailable'
        except asyncio.TimeoutError:
            cognitive_turn, audit_status = None, 'timeout'
        metrics.record_stage('audit_prepare', time.perf_counter() - audit_started)
        metrics.record_stage('context_prepare', time.perf_counter() - started)
        if webui:
            # Private HTTP diagnostics only: never send memory text to OBS,
            # guest WebSockets or the text-free experience metrics export.
            webui.reply_diagnostics = {
                'turn_id': event.ui_id, 'prepared_at': time.time(),
                'sources': preparation, 'audit_status': audit_status,
                'plan': persona.plan_for(event),
            }
        if agent_tasks and not agent_tasks.refresh_receipt(event):
            await cognition.abort_turn(cognitive_turn, '任务通知已过期或撤销')
            return
        synthesis: list[asyncio.Future[SynthesisResult]] = []
        partial_text = ""
        streamed_actions: list[str] = []
        try:
            async for segment in persona.stream_reply(event, extra_context=extra_context):
                if (event.ui_id in failed_turns or event.ui_id in cancelled_questions
                        or not companion_event_current(event)):
                    raise asyncio.CancelledError()
                if not partial_text:
                    metrics.record_first_sentence(event.ui_id)
                partial_text += segment.text
                if segment.actions:
                    streamed_actions = list(segment.actions)
                await push_event({
                    "type": "reply_partial",
                    "id": event.ui_id,
                    "emotion": segment.emotion,
                    "text": partial_text,
                    "actions": streamed_actions,
                    "segment": segment.index,
                    "elapsed": round(time.perf_counter() - started, 2),
                })
                if not companion_event_current(event):
                    raise asyncio.CancelledError()
                if not event.text_only_delivery:
                    question_delivery.expect(event.ui_id, segment.index)
                    synthesis.append(speech_pipeline.enqueue(
                        turn_id=event.ui_id,
                        segment_index=segment.index,
                        text=segment.text,
                        emotion=segment.emotion,
                        actions=segment.actions,
                        event=event,
                    ))

            if not companion_event_current(event):
                raise asyncio.CancelledError()
            latency = round(time.perf_counter() - started, 2)
            emotion = persona.last_emotion
            text = persona.last_text or partial_text
            actions = persona.last_actions or streamed_actions
            if not text:
                raise RuntimeError("回复管线没有产生正文")
            if persona.last_mode in {'fallback', 'partial'}:
                await reply_failed(event, 'llm')
            elif persona.last_mode == 'llm':
                recovery.success('llm')
            question_delivery.generated(event.ui_id, persona.last_mode not in {'fallback', 'partial', 'cancelled'})
            if persona.last_search_seconds > 0:
                metrics.record_stage("search", persona.last_search_seconds)
            metrics.record_reply(
                event.ui_id, latency=latency, llm_ttft=persona.last_ttft
            )
            await avatar.show(emotion)
            if not companion_event_current(event):
                raise asyncio.CancelledError()
            if not private_followup and not tarot_turn:
                memory.record(event, emotion, text)
            learning_episode_id = None
            if persona.last_mode == "llm" and not private_followup and not tarot_turn:
                learning_episode_id = learning.learn_reply(event, emotion, text)
            if not private_followup and not tarot_turn and (settings.tts.backend == "mock" or event.text_only_delivery):
                conversation.record_reply(event, text)
                remember_spoken(event, text, emotion)
            if not private_followup and not tarot_turn:
                director.after_reply(event, emotion, text)
            await push_event({
                "type": "reply", "id": event.ui_id, "emotion": emotion,
                "text": text, "actions": actions,
                "director_phase": director_turn["phase_label"],
                "latency": latency, "ttft": round(persona.last_ttft, 2),
                "segments": len(synthesis), "mode": persona.last_mode,
                "error": '回复未完整生成，请查看运行状态' if persona.last_error and persona.last_mode != 'llm' else '',
            })
            if tarot_turn:
                if persona.last_mode == 'llm':
                    tarot.mark_generated(event, text, segments=len(synthesis),
                                         text_only=event.text_only_delivery or settings.tts.backend == 'mock')
                else:
                    tarot.mark_result(event, 'failed', error='解读未完整生成，可用原牌重试。')
            if companion is not None and not tarot_turn:
                await companion.generated(event, text, emotion,
                                          segments=0 if settings.tts.backend == 'mock' else len(synthesis),
                                          successful=persona.last_mode == 'llm')
                if persona.last_mode == 'llm' and event.kind == 'chat':
                    meme = companion.pick_meme(event.text, emotion, text)
                    if meme:
                        await push_event({'type': 'companion_meme', 'turn_id': event.ui_id,
                                          'id': meme['id'], 'caption': meme['caption'],
                                          'url': '/api/companion/memes/' + meme['id']})
            await push_event({"type": "director", **director.summary()})
            await push_event({"type": "learning", **learning.summary()})
            await push_event({"type": "personality", **personality.summary()})
            finalizer = asyncio.create_task(finalize_cognition(
                cognitive_turn,
                event,
                emotion=emotion,
                text=text,
                actions=actions,
                response_mode=persona.last_mode,
                response_latency=latency,
                synthesis=synthesis,
                learning_episode_id=learning_episode_id,
            ))
            track_finalizer(finalizer)
        except asyncio.CancelledError:
            await cognition.abort_turn(cognitive_turn, "被更高优先级直播事件中断")
            raise
        except Exception as exc:
            metrics.record_error(event.ui_id)
            await reply_failed(event, 'llm')
            await cognition.abort_turn(cognitive_turn, f"回复管线失败：{exc}")
            await push_event({
                "type": "reply_error", "id": event.ui_id,
                "error": f"{type(exc).__name__}：回复没有完成，请稍后重试",
            })
            print(f"[runtime] 回复管线失败：{type(exc).__name__}")

    async def scheduler_loop() -> None:
        from brain.tarot_runtime import holds_audience_event
        active_task: asyncio.Task | None = None
        active_event = None
        try:
            while True:
                if manual_mode.is_set():
                    arbiter.discard_unforced()
                drawing_active = bool(agent_tasks and any(
                    j['state'] == 'running' and j['kind'] == 'drawing' for j in agent_tasks.jobs))
                persona.behavior.set_working('drawing' if drawing_active else '')
                exclusive = (media_narrator.active or recovery.preparing
                             or bool(webui and getattr(webui, 'bilibili_active', False))
                             or bool(webui and webui.rehearsal and webui.rehearsal.running))
                automatic_allowed = (director.enabled and not manual_mode.is_set()
                                     and persona.behavior.allow_proactive and recovery.allow_automatic() and not exclusive)
                if automatic_allowed and tarot:
                    try:
                        automatic_allowed = not tarot.live_active()
                    except (OSError, sqlite3.Error):
                        automatic_allowed = False
                proactive = director.poll_proactive(allow=automatic_allowed)
                if proactive is not None:
                    await register_event(proactive)

                if active_task is not None:
                    if active_task.done():
                        try:
                            await active_task
                        except asyncio.CancelledError:
                            pass
                        active_task = None
                        active_event = None
                    elif (interrupt_generation.is_set() or active_event.ui_id in cancelled_questions
                          or active_event.ui_id in failed_turns
                          or not companion_event_current(active_event)
                          or not director.program_event_current(active_event)
                          or arbiter.has_urgent(above_priority=active_event.priority)):
                        active_task.cancel()
                        try:
                            await active_task
                        except asyncio.CancelledError:
                            pass
                        active_task = None
                        active_event = None
                    await asyncio.sleep(0.05)
                    continue

                interrupt_generation.clear()
                if exclusive:
                    await asyncio.sleep(.05)
                    continue
                # Wait for the previous voice to finish before spending another viewer's turn.
                if (playback.state in {'dispatched', 'playing', 'paused'} or speech_pipeline.busy) and not arbiter.has_urgent():
                    await asyncio.sleep(0.05)
                    continue
                event = arbiter.pick(allow_idle=False, allow_automatic=recovery.allow_automatic())
                if event is not None and holds_audience_event(tarot, event):
                    continue  # Also drain unrelated messages queued before this guest sat down.
                if event is not None:
                    active_turns.add(event.ui_id)
                    active_event = event
                    active_task = asyncio.create_task(process_event(event))
                else:
                    await asyncio.sleep(0.05)
        finally:
            if active_task is not None and not active_task.done():
                active_task.cancel()
                await asyncio.gather(active_task, return_exceptions=True)

    async def persist_delivery(event, text, emotion) -> None:
        personality.record_reply(event, text, emotion)
        await consolidate_autobiography(event, text, emotion)

    def remember_spoken(event, text, emotion) -> None:
        if not voice_revisions.stage(event, text, emotion):
            track_finalizer(asyncio.create_task(persist_delivery(event, text, emotion)))

    def delivered(item, completed: bool) -> None:
        if item.event.get('tarot_reading_id'):
            audit_delivery(item, completed, 'ended' if completed else 'interrupted')
            if tarot:
                tarot.played(item.event, item.segment_index, completed)
            return  # Card interpretations are not autobiographical facts or private diary entries.
        if item.event.get('media_narration_id'):
            media_narrator.delivered(item, completed)
            return
        if item.event.get('rehearsal_id'):
            if webui and webui.rehearsal:
                webui.rehearsal.audio_finished(item.turn_id, completed=completed)
            return
        if item.event.get('is_fallback'):
            return
        audit_delivery(item, completed, 'ended' if completed else 'interrupted')
        question_delivery.played(item.turn_id, item.segment_index, completed)
        companion = getattr(services, 'companion', None)
        if companion is not None:
            track_finalizer(asyncio.create_task(companion.played(item.turn_id, item.segment_index, completed)))
        if item.event.get('companion_followup_token'):
            return
        event = conversation.record_playback(item, completed)
        if event is not None:
            remember_spoken(event, item.text, item.emotion)

    playback.on_delivery = delivered

    async def memory_changed(memory, replacement) -> None:
        if qq_channel:
            await qq_channel.apply_memory_change(memory, replacement)
        source = memory.get('original_source') or memory.get('source', {})
        user = source.get('user')
        await register_event(Event('companion', 'interrupt', '记忆管理更新', user=user, forced=True))
        # Shared short-term state may serve other, unverified inputs. Clear old
        # context, but private corrections must be recalled through scoped memory.
        shared_replacement = None if memory.get('visibility') == 'private' else replacement
        conversation.apply_memory_change(memory, shared_replacement, time.time())
        persona.apply_memory_change(memory)
        director.clear_conversation_topic(memory_change_owner(memory))
        closed_id = voice_revisions.apply_memory_change(memory)
        if closed_id is not None:
            await push_event({'type':'memory_confirmation', 'ok':True, 'id':closed_id,
                              'text':'记忆管理已更新，旧语音的待确认记录已清理。'})

    if webui is not None:
        webui.on_memory_change = memory_changed

    try:
        await services.start()
        if not settings.webui.public_test:
            from .rvc_backend import RVCBackend
            rvc = RVCBackend(config.ROOT, services.music_requests, push_event)
            supervisor.start('rvc', rvc.run, restart=False, critical=False)
            supervisor.start('suno', lambda: services.suno_manual.run(settings.llm, push_event),
                             restart=False, critical=False)
        supervisor.start('event_ingest', ingest_events)
        if tarot:
            from .tarot_runtime import TarotHost
            tarot_host = TarotHost(tarot, register_event, available=lambda: (
                not manual_mode.is_set() and not playback.paused and services.idle()
                and recovery.allow_automatic()
                and not (webui and (getattr(webui, 'bilibili_active', False)
                                   or webui.rehearsal and webui.rehearsal.running))))
            supervisor.start('tarot_host', tarot_host.run, restart=False, critical=False)
        supervisor.start('maintenance', maintenance_loop)
        # A stopped pipeline has cancelled its pending futures and closed itself;
        # restarting the same object would silently reject new audio jobs.
        supervisor.start('speech', speech_pipeline.run, restart=False)
        if audio_retention.enabled:
            supervisor.start('audio_retention', maintain_audio, critical=False)
        if webui:
            webui.runtime_started = True
        scheduling = asyncio.create_task(scheduler_loop())
        upgrade_stop = asyncio.create_task(wait_for_upgrade())
        try:
            finished, _ = await asyncio.wait((scheduling, upgrade_stop),
                                             return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
        finally:
            scheduling.cancel()
            upgrade_stop.cancel()
            await asyncio.gather(scheduling, upgrade_stop, return_exceptions=True)
    finally:
        # Stop accepting actions before closing the services those actions use.
        if webui:
            await shutdown_step('主控页面', webui.close)
        if qq_channel:
            await shutdown_step('QQ 私聊', qq_channel.close)
        await shutdown_step('点歌旁白', media_narrator.close)
        if webui and webui.rehearsal:
            await shutdown_step('开播演练', webui.rehearsal.close)
        for turn_id in tuple(question_delivery.turns):
            question_delivery.cancel(turn_id, '服务已停止，上次回答未确认播完')
        if game_agent:
            await shutdown_step('游戏急停', lambda: game_agent.emergency_stop('程序退出'))
        await shutdown_step('后台工作任务', supervisor.close)
        await shutdown_step('应用服务', services.close)
        if game_agent:
            await shutdown_step('游戏输入', lambda: game_agent.emergency_stop('输入线程已退出'))
        for task in list(finalizers):
            task.cancel()
        await asyncio.gather(*finalizers, return_exceptions=True)
        await shutdown_step('人格关系', personality.close)
        await shutdown_step('长期记忆', autobiography.close)
        results = await asyncio.gather(*(store.close_persistence() for store in persistence.values()),
                                       return_exceptions=True)
        for name, result in zip(persistence, results):
            if isinstance(result, BaseException):
                print(f'[storage] {name}退出保存失败：{type(result).__name__}')
        closers = [('语言模型', persona.close), ('语音', speech_pipeline.close), ('认知记录', cognition.close)]
        if agent_tasks:
            closers.append(('后台任务服务', agent_tasks.close))
        await asyncio.gather(*(shutdown_step(name, operation) for name, operation in closers))


def main() -> None:
    if sys.platform == "win32":  # Windows 终端默认 GBK，避免 emoji / 中文报错
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n下播啦，晚安！")


if __name__ == "__main__":
    main()
