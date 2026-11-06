"""Web 测试台：浏览器里实时看事件流 / 仲裁结果 / 回复，手动注入测试事件，试听 TTS。

页面在 brain/static/index.html；与主循环同进程同事件循环运行（aiohttp）。
"""
from __future__ import annotations

import asyncio
import json
import time
import os
import secrets
import re
import sqlite3
from collections import deque
from urllib.parse import urlsplit
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .config import ROOT, WebUIConfig
from .events import PRIO_AMBIENT, PRIO_CHAT, PRIO_DEATH, PRIO_FIND, PRIO_GIFT, PRIO_IDLE, Event
from .socket_outbox import SocketOutbox

if TYPE_CHECKING:
    from .autobiographical_memory import AutobiographicalMemory
    from .director import LiveDirector
    from .game_agent import GameAgent
    from .metrics import RuntimeMetrics
    from .personality_evolution import PersonalityEvolution
    from .playback import SpeechController

STATIC_DIR = Path(__file__).parent / "static"

_KIND_PRIOS = {
    "chat": PRIO_CHAT,
    "gift": PRIO_GIFT,
    "death": PRIO_DEATH,
    "find": PRIO_FIND,
    "ambient": PRIO_AMBIENT,
    "idle": PRIO_IDLE,
}

_GUEST_WS_ACTIONS = frozenset({
    'inject', 'conversation_interrupt', 'audio_started', 'audio_finished',
    'audio_progress', 'playback_skip', 'playback_pause', 'playback_clear',
})


class WebUI:
    def __init__(
        self,
        cfg: WebUIConfig,
        event_queue: asyncio.Queue,
        audio_dir: str,
        info: dict,
        mock_danmaku_enabled: asyncio.Event | None = None,
        mock_game_enabled: asyncio.Event | None = None,
        manual_mode: asyncio.Event | None = None,
        director: LiveDirector | None = None,
        playback: SpeechController | None = None,
        game_agent: GameAgent | None = None,
        metrics: RuntimeMetrics | None = None,
        selective_memory: AutobiographicalMemory | None = None,
        personality: PersonalityEvolution | None = None,
        *,
        workspace_root: Path | None = None,
        services=None,
    ):
        self.cfg = cfg
        self.event_queue = event_queue
        self.audio_dir = Path(audio_dir)
        self.info = info  # {"llm": ..., "tts": ...} 显示在页头
        self.mock_danmaku_enabled = mock_danmaku_enabled
        self.mock_game_enabled = mock_game_enabled
        self.manual_mode = manual_mode
        self.director = director
        self.playback = playback
        self.game_agent = game_agent
        self.metrics = metrics
        self.selective_memory = selective_memory
        self.personality = personality
        self.allowed_ws_actions = None
        self.on_memory_change = None
        self.conversation_status = None
        self.reply_diagnostics = {}
        self.cognition_delivery = None
        self.agent_tasks = None
        self.bilibili_activity = None
        self.qq_channel = None
        self.live_polls = None
        self.tarot = None
        self.on_tarot_cancel = None
        self.show_sessions = None
        self._program_armed_show = None
        self._program_context = {}
        self._program_context_at = -float('inf')
        self.question_cards = None
        self.on_question_ask = None
        self.on_question_cancel = None
        self._question_actions = asyncio.Lock()
        self._poll_announcement_at = -float('inf')
        self.tool_registry = None
        # dict 保留连接顺序；第一个页面是唯一音频播放器，避免多标签页重复朗读。
        self._sockets: dict[web.WebSocketResponse, None] = {}
        self._socket_priority = {}
        self._local_control_sockets = set()
        self._private_output_transitions = 0
        self.on_private_followups_blocked = None
        self._danmaku_sockets = set()
        self._danmaku_history = deque(maxlen=50)
        self._outboxes: dict[web.WebSocketResponse, SocketOutbox] = {}
        self._slow_connections = 0
        self.runtime_supervisor = None
        self.runtime_started = False
        self.release_gate = None
        self.upgrade_idle = lambda: False
        self.audio_retention = None
        self.rehearsal = None
        self.broadcast_status = None
        self.on_prepare_standby = None
        self.on_bilibili_narrate = None
        self.bilibili_review_enabled = False
        from .bilibili_video_evidence import BilibiliVideoEvidence
        self._bilibili_video_evidence = BilibiliVideoEvidence()
        self._bilibili_narrations = {}
        self._bilibili_flows = set()
        self._song_flows = set()
        self._media_operations = asyncio.Lock()
        self.persistence_status = None
        self._runner = None
        service_root = Path(workspace_root) if workspace_root is not None else ROOT
        self._songs_folder = service_root / 'data' / 'songs'
        from .runtime_services import RuntimeServices
        self._owns_services = services is None
        self.services = services or RuntimeServices(service_root, event_queue, public_test=cfg.public_test)
        self.services.bind(self)

    async def start(self) -> None:
        from .tool_registry import build_registry
        self.tool_registry = build_registry(self, live=True)
        if self.agent_tasks:
            self.agent_tasks.tool_registry = self.tool_registry
        from .code_review import sources, digest
        self.runtime_health = {'scope':'main','ready':True,'boot_id':secrets.token_hex(8),
                               'code':{name:digest(data) for name,data in sources(ROOT).items()}}
        if self.release_gate:
            self.runtime_health.update(self.release_gate.health_fields())
        @web.middleware
        async def access(request, handler):
            return await self._public_access(request, handler)
        if self.cfg.public_test and len(os.environ.get(self.cfg.public_token_env, '')) < 24:
            raise RuntimeError('Public test requires a strong access token')
        app = web.Application(middlewares=[access], client_max_size=32768)
        from .companion_web import install_companion_routes
        install_companion_routes(self, app)
        from .trending_web import install_trending_routes
        install_trending_routes(self, app)
        from .tarot_web import install_tarot_routes
        install_tarot_routes(self, app)
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/audio/{name}", self._audio)
        app.router.add_get('/songs', self._songs)
        app.router.add_get('/music-requests', self._music_requests)
        app.router.add_get('/minecraft', self._minecraft_page)
        app.router.add_get('/tasks', self._tasks_page)
        app.router.add_get('/polls', self._polls_page)
        app.router.add_get('/polls-status', self._polls_status)
        app.router.add_post('/polls-action', self._polls_action)
        app.router.add_get('/shows', self._shows_page)
        app.router.add_get('/shows-status', self._shows_status)
        app.router.add_get('/shows-poll/{poll_id}', self._shows_poll)
        app.router.add_get('/shows-drawing', self._shows_drawing_page)
        app.router.add_get('/shows-drawing-data/{show_id}/{card_id}', self._shows_drawing_data)
        app.router.add_post('/shows-action', self._shows_action)
        app.router.add_get('/show-program-status', self._show_program_status)
        app.router.add_get('/show-stage', self._show_stage_page)
        app.router.add_get('/show-stage-status', self._show_stage_status)
        app.router.add_get('/questions', self._questions_page)
        app.router.add_get('/questions-status', self._questions_status)
        app.router.add_post('/questions-action', self._questions_action)
        app.router.add_get('/drawing', self._drawing_page)
        app.router.add_get('/drawing-data/{job_id}', self._drawing_data)
        app.router.add_get('/drawing-state/{job_id}', self._drawing_state)
        app.router.add_get('/tasks-status', self._tasks_status)
        app.router.add_get('/runtime-health', self._runtime_health)
        app.router.add_get('/runtime', self._runtime_page)
        app.router.add_get('/runtime-status', self._runtime_status)
        app.router.add_get('/rehearsal', self._rehearsal_page)
        app.router.add_get('/rehearsal-status', self._rehearsal_status)
        app.router.add_post('/rehearsal-action', self._rehearsal_action)
        app.router.add_post('/broadcast-action', self._broadcast_action)
        app.router.add_get('/audio-library', self._audio_library)
        app.router.add_post('/audio-library-action', self._audio_library_action)
        app.router.add_get('/experience-report', self._experience_report)
        app.router.add_post('/tasks-action', self._tasks_action)
        app.router.add_get('/task-artifact/{job_id}/{name:.*}', self._task_artifact)
        app.router.add_get('/minecraft-status', self._minecraft_status)
        app.router.add_post('/minecraft-action', self._minecraft_action)
        app.router.add_get('/suno-inbox', self._suno_inbox)
        app.router.add_post('/suno-manual', self._suno_action)
        app.router.add_get('/bilibili', self._media_page)
        app.router.add_get('/bilibili-activity', self._activity_page)
        app.router.add_get('/api/bilibili-activity', self._activity_status)
        app.router.add_post('/api/bilibili-activity/action', self._activity_action)
        app.router.add_post('/api/bilibili-activity/browser', self._activity_browser)
        app.router.add_get('/qq', self._qq_page)
        app.router.add_get('/api/qq', self._qq_status)
        app.router.add_post('/api/qq/action', self._qq_action)
        app.router.add_get('/bilibili-search', self._search_media)
        app.router.add_post('/bilibili-browser/request', self._browser_media_request)
        app.router.add_post('/bilibili-browser/result', self._browser_media_result)
        app.router.add_post('/bilibili-browser/cancel', self._browser_media_cancel)
        app.router.add_post('/bilibili-video/request', self._browser_video_request)
        app.router.add_post('/bilibili-video/result', self._browser_video_result)
        app.router.add_post('/bilibili-video/cancel', self._browser_video_cancel)
        app.router.add_get('/danmaku', self._danmaku_page)
        app.router.add_get('/danmaku-ws', self._danmaku_ws)
        app.router.add_get('/bilibili-status', self._bilibili_status)
        app.router.add_get('/songs/{song_id}', self._song)
        app.router.add_get("/game-frame.jpg", self._game_frame)
        if (ROOT / "skin").is_dir():
            app.router.add_static("/skin/", ROOT / "skin", show_index=False)
        app.router.add_static("/static/", STATIC_DIR, show_index=False)   # 前端本地依赖(vendor)
        runner = web.AppRunner(app)
        self._runner = runner
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.host, self.cfg.port)
        await site.start()
        if self._owns_services:
            self.services.bilibili_activity = self.bilibili_activity
            await self.services.start()
        print(f"   测试台：http://{self.cfg.host}:{self.cfg.port}")

    async def _public_access(self, request, handler):
        if not self.cfg.public_test:
            self._local_access(request, write=request.method not in {'GET', 'HEAD', 'OPTIONS'})
            return await handler(request)
        token = os.environ.get(self.cfg.public_token_env, '')
        supplied = request.query.get('key', '') if request.path == '/' else ''
        if token and supplied and secrets.compare_digest(supplied, token):
            response = web.HTTPFound('/')
            response.set_cookie('vtuber_test', token, secure=True, httponly=True, samesite='Strict', max_age=28800)
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['Cache-Control'] = 'no-store'
            return response
        if not token or not secrets.compare_digest(request.cookies.get('vtuber_test', ''), token):
            raise web.HTTPForbidden(text='请使用完整的朋友测试邀请链接打开。')
        if request.path == '/game-frame.jpg':
            raise web.HTTPForbidden()
        if request.path in {'/ws', '/danmaku-ws'}:
            self._websocket_access(request)
        response = await handler(request)
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def _public_message(self, payload):
        if not self.cfg.public_test:
            return payload
        if payload.get('type') == 'hello':
            return {key:payload[key] for key in ('type','focus','llm','tts','playback','conversation_state','audio_owner','audio_target','audio_clients') if key in payload} | {'public_test':True}
        if payload.get('type') in {'reply_error','tts_error'}:
            return {**payload, 'error':'服务暂时无法完成，请稍后重试。'}
        return payload

    def _websocket_access(self, request) -> bool:
        """Authenticate before upgrading; return whether this is the local host UI."""
        try:
            origins = request.headers.getall('Origin', [])
            if len(origins) != 1:
                raise ValueError('missing or repeated Origin')
            origin = urlsplit(origins[0])
            host = urlsplit('//' + request.host)
            if (origin.scheme not in {'http', 'https'} or origin.netloc != request.host
                    or origin.path or origin.query or origin.fragment or origin.username is not None
                    or host.username is not None
                    or not self.cfg.public_test and origin.scheme != request.scheme):
                raise ValueError('foreign Origin')
            # Force validation of malformed/out-of-range ports, including IPv6 authorities.
            origin.port
            host.port
        except (ValueError, TypeError):
            raise web.HTTPForbidden(text='WebSocket 仅接受主控页面的同源连接') from None
        if self.cfg.public_test:
            token = os.environ.get(self.cfg.public_token_env, '')
            if not token or not secrets.compare_digest(request.cookies.get('vtuber_test', ''), token):
                raise web.HTTPForbidden(text='请先使用朋友测试邀请链接登录')
            return False
        if request.remote not in {'127.0.0.1', '::1'} or host.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise web.HTTPForbidden(text='主控 WebSocket 仅供本机使用')
        return True

    async def _index(self, request):
        return web.FileResponse(STATIC_DIR / "index.html", headers={'Cache-Control':'no-store'})

    def _local_access(self, request, *, write=False):
        """Apply the same loopback boundary to HTTP reads, writes and controls."""
        if self.cfg.public_test or request.remote not in {'127.0.0.1', '::1'}:
            raise web.HTTPForbidden(text='主控接口仅供本机使用')
        try:
            hosts = request.headers.getall('Host', [])
            if len(hosts) != 1:
                raise ValueError('missing or repeated Host')
            host = urlsplit('//' + hosts[0])
            if (host.hostname not in {'127.0.0.1', 'localhost', '::1'}
                    or host.username is not None or host.path or host.query or host.fragment):
                raise ValueError('nonlocal Host')
            host.port
        except (ValueError, TypeError):
            raise web.HTTPForbidden(text='主控接口仅接受本机地址') from None
        if write:
            self._websocket_access(request)

    def _minecraft_access(self, request, *, write=False):
        self._local_access(request, write=write)

    async def _minecraft_page(self, request):
        self._minecraft_access(request)
        return web.FileResponse(STATIC_DIR / 'minecraft.html', headers={'Cache-Control': 'no-store'})

    async def _tasks_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'tasks.html', headers={'Cache-Control': 'no-store'})

    async def _polls_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'polls.html', headers={'Cache-Control': 'no-store'})

    async def _polls_status(self, request):
        self._tasks_access(request)
        if self.live_polls is None:
            return web.json_response({'error': '投票服务未启动'}, status=503)
        import sqlite3
        try:
            result = self.live_polls.status(include_history=request.query.get('clean') != '1')
            from .danmaku import BILIBILI_STATUS
            result['source'] = {key: BILIBILI_STATUS[key] for key in ('state', 'room_id', 'last_heartbeat')}
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (OSError, sqlite3.Error):
            return web.json_response({'error': '投票存储暂不可用，请稍后重试'}, status=503)

    async def _polls_action(self, request):
        self._tasks_access(request, write=True)
        if self.live_polls is None:
            return web.json_response({'error': '投票服务未启动'}, status=503)
        import sqlite3
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            action = data.get('action')
            if action == 'create':
                result = self.live_polls.create(program=data.get('program'), question=data.get('question'),
                    options=data.get('options'), duration=data.get('duration', 180), mode=data.get('mode', 'live'))
            else:
                poll_id = data.get('id')
                if not isinstance(poll_id, str) or len(poll_id) != 16:
                    raise ValueError('请选择一轮投票')
                if action in {'close', 'cancel'}:
                    result = self.live_polls.finish(poll_id, cancel=action == 'cancel')
                elif action == 'test_vote':
                    result = self.live_polls.test_vote(poll_id, data.get('user'), data.get('choice'))
                elif action == 'announce':
                    if time.monotonic() - self._poll_announcement_at < 10:
                        raise ValueError('请间隔 10 秒后再播报')
                    event = self.live_polls.announcement_event(poll_id)
                    text = event.agent_reply
                    await self.event_queue.put(event)
                    self._poll_announcement_at = time.monotonic()
                    result = {'ok': True, 'message': '已加入语音队列，内容按点击时的统计生成', 'text': text}
                else:
                    raise ValueError('未知投票操作')
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)[:200]}, status=400)
        except (OSError, sqlite3.Error):
            return web.json_response({'error': '投票存储暂不可用，操作未确认，请刷新后检查'}, status=503)

    def _question_store(self):
        if self.question_cards is None:
            from .question_cards import QuestionCards
            self.question_cards = QuestionCards(ROOT / 'data/question-cards.sqlite3')
        return self.question_cards

    def question_event_payload(self, event):
        if self.cfg.public_test:
            return {}
        import sqlite3
        try:
            return self._question_store().remember_event(event)
        except (OSError, sqlite3.Error):
            # Optional host curation must not drop the incoming conversation.
            # The question page reports storage availability independently.
            return {}

    async def _questions_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'questions.html', headers={'Cache-Control': 'no-store'})

    async def _questions_status(self, request):
        self._tasks_access(request)
        import sqlite3
        try:
            result = self._question_store().status()
            result['can_ask'] = self.on_question_ask is not None
            result['can_cancel'] = self.on_question_cancel is not None
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (OSError, sqlite3.Error):
            return web.json_response({'error': '待答卡存储暂不可用'}, status=503)

    async def _questions_action(self, request):
        self._tasks_access(request, write=True)
        import sqlite3
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            async with self._question_actions:
                store = self._question_store()
                action = data.get('action')
                if action == 'pin':
                    card = store.pin(data.get('token'))
                elif action == 'manual':
                    card = store.manual(data.get('text'), data.get('request_key'))
                elif action == 'ask':
                    if self.on_question_ask is None:
                        raise RuntimeError('主控调度未连接')
                    card = store.begin_answer(data.get('id'), data.get('version'))
                    try:
                        turn_id = await self.on_question_ask(card)
                        card = store.bind_turn(card['id'], card['attempt'], turn_id)
                    except Exception:
                        store.dispatch_failed(card['id'], card['attempt'])
                        raise
                elif action == 'move':
                    state = data.get('state')
                    if state not in {'waiting', 'parked', 'withdrawn'}:
                        raise ValueError('不支持该问题状态')
                    card = store.checked(data.get('id'), data.get('version'))
                    if card['state'] == 'answering':
                        if self.on_question_cancel is None:
                            raise RuntimeError('主控取消调度未连接')
                        await self.on_question_cancel(card)
                        card = store.finish_answer(card['id'], card['attempt'], reason='主播停止了本次回答')
                    card = store.move(card['id'], card['version'], state)
                elif action == 'delete':
                    return web.json_response(store.delete(data.get('id'), data.get('version')),
                                             headers={'Cache-Control': 'no-store'})
                else:
                    raise ValueError('未知待答卡操作')
                return web.json_response(store.public(card), headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)[:200]}, status=400)
        except (RuntimeError, OSError, sqlite3.Error):
            return web.json_response({'error': '待答卡操作未确认，请刷新状态后重试'}, status=503)

    def _show_store(self):
        if self.show_sessions is None:
            from .show_sessions import ShowSessions
            self.show_sessions = ShowSessions(ROOT / 'data/show-sessions.sqlite3')
        return self.show_sessions

    async def _shows_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'shows.html', headers={'Cache-Control': 'no-store'})

    def program_context(self):
        return dict(self._program_context)

    def _program_drawing(self, root):
        manager = self.agent_tasks
        if manager is None or not root or getattr(manager, 'closing', False):
            return None
        if getattr(manager, 'remote', False) and (not manager.connected or time.monotonic() - manager.last_sync > 3):
            return None
        try:
            job, ready, seen = manager.get(root), None, set()
            while True:
                if job['id'] in seen or len(seen) >= 80 or job['kind'] != 'drawing':
                    return None
                seen.add(job['id'])
                if job['state'] == 'succeeded' and 'drawing.json' in job.get('artifacts', []):
                    ready = job.get('drawing_stage', 'final')
                if not job.get('superseded_by'):
                    return {'state': job['state'], 'stage': job.get('drawing_stage', 'sketch'), 'ready_stage': ready}
                job = manager.get(job['superseded_by'])
        except (StopIteration, KeyError, ValueError):
            return None

    async def refresh_program_context(self, *, force=False):
        """Use current local facts and the task client's fresh cache; never RPC."""
        if not force and time.monotonic() - self._program_context_at < 2:
            return self.program_context()
        import sqlite3
        from .show_sessions import program_context
        try:
            show = (self._show_store().current() if self.show_sessions is not None or
                    (ROOT / 'data/show-sessions.sqlite3').exists() else None)
            program = (show or {}).get('program') or {}
            card = next((c for c in (show or {}).get('cards', []) if c['id'] == program.get('card_id')), None)
            poll = None
            if program.get('poll_id') and self.live_polls:
                try:
                    poll = self.live_polls.get(program['poll_id'])
                except (ValueError, OSError, sqlite3.Error):
                    pass
            roots = {c['drawing_root'] for c in (show or {}).get('cards', []) if c.get('drawing_root')}
            drawings = {root: self._program_drawing(root) for root in roots}
            unresolved = any(c.get('submission', {}).get('state') == 'pending'
                             for c in (show or {}).get('cards', []) if c.get('submission'))
            pending = (None if unresolved or any(d is None for d in drawings.values()) else
                       sum(d['state'] in {'queued', 'running', 'waiting_approval'} for d in drawings.values()))
            self._program_context = program_context(show, armed=bool(show and self._program_armed_show == show['id']),
                poll=poll, drawing=drawings.get((card or {}).get('drawing_root')), pending_count=pending)
            if show is None:
                self._program_armed_show = None
        except (ValueError, OSError, sqlite3.Error):
            self._program_context = {**self._program_context, 'fresh': False, 'live_mode': False,
                                     'status': '栏目状态暂未确认', 'updated_at': time.time()}
        self._program_context_at = time.monotonic()
        if self.director:
            self.director.set_program_context(self._program_context)
        return self.program_context()

    async def _show_program_status(self, request):
        self._tasks_access(request)
        import sqlite3
        try:
            show = self._show_store().current()
            context = await self.refresh_program_context()
            poll = self.live_polls.status(include_history=False)['current'] if self.live_polls else None
            return web.json_response({'context': context, 'show_id': (show or {}).get('id'),
                'version': (show or {}).get('version'), 'program': (show or {}).get('program') or {},
                'cards': [{'id': c['id'], 'title': c['title']} for c in (show or {}).get('cards', [])],
                'available_poll': {k: poll[k] for k in ('id', 'question', 'state', 'mode')} if poll else None},
                headers={'Cache-Control': 'no-store'})
        except (OSError, sqlite3.Error):
            return web.json_response({'error': '节目单状态暂不可用'}, status=503)

    async def _show_stage_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'show-stage.html', headers={'Cache-Control': 'no-store'})

    async def _show_stage_status(self, request):
        self._tasks_access(request)
        from .show_sessions import public_program
        return web.json_response(public_program(await self.refresh_program_context()),
                                 headers={'Cache-Control': 'no-store'})

    async def _shows_status(self, request):
        self._tasks_access(request)
        import sqlite3
        try:
            return web.json_response(self._show_store().status(), headers={'Cache-Control': 'no-store'})
        except (OSError, sqlite3.Error):
            return web.json_response({'error': '栏目存储暂不可用'}, status=503)

    async def _shows_poll(self, request):
        self._tasks_access(request)
        if self.live_polls is None:
            return web.json_response({'error': '投票服务未启动'}, status=503)
        import sqlite3
        try:
            from .show_sessions import poll_commission
            poll = self.live_polls.get(request.match_info['poll_id'])
            if poll['state'] != 'closed':
                raise ValueError('请先结束投票；取消的投票不能生成委托')
            seed = poll_commission(poll) if len(poll['winners']) == 1 else {'prompt': '', 'choice': None}
            return web.json_response({'poll': poll, 'prompt': seed['prompt'], 'choice': seed['choice']},
                                     headers={'Cache-Control': 'no-store'})
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except (OSError, sqlite3.Error):
            return web.json_response({'error': '投票存储暂不可用'}, status=503)

    async def _shows_drawing_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'show-drawing.html', headers={'Cache-Control': 'no-store'})

    async def _shows_drawing_data(self, request):
        self._tasks_access(request)
        import hashlib
        import re
        import sqlite3
        from .agent_tasks import artifact_path
        from .drawing import load_drawing
        try:
            store = self._show_store()
            show = store.get(request.match_info['show_id'])
            card = store.card(show, request.match_info['card_id'])
            snapshot = (card.get('drawing') or {}).get('drawing')
            if show['state'] != 'closed' or not snapshot:
                raise ValueError('这张卡片没有收播时确认的作品版本')
            job_id = snapshot['job_id']
            if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{12}', job_id):
                raise ValueError('历史作品编号无效')
            folder = self.agent_tasks.folder if self.agent_tasks else ROOT / 'data/agent-tasks'
            document = load_drawing(artifact_path(folder / job_id, 'drawing.json'))
            version = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:24]
            if version != snapshot['version']:
                return web.json_response({'error': '历史作品文件已变化，无法展示收播时版本'}, status=409)
            return web.json_response({'document': document, 'snapshot': snapshot,
                'theme': show['theme'], 'closed': show['closed'], 'name': card['name']},
                headers={'Cache-Control': 'no-store'})
        except (ValueError, KeyError, OSError, sqlite3.Error):
            return web.json_response({'error': '历史作品不可用；不会自动替换为后续版本'}, status=404)

    async def _show_task_action(self, data):
        if self.agent_tasks is None:
            raise RuntimeError('后台任务服务未连接')
        if getattr(self.agent_tasks, 'remote', False):
            return await self.agent_tasks.action(data)
        from .task_actions import task_action
        return await task_action(self.agent_tasks, data)

    async def _shows_action(self, request):
        self._tasks_access(request, write=True)
        import sqlite3
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            store = self._show_store()
            action, key, version = data.get('action'), data.get('id'), data.get('version')
            if action == 'start':
                result = store.start(data.get('theme'), data.get('previous'))
            elif action == 'add':
                poll = None
                if data.get('poll_id'):
                    if self.live_polls is None:
                        raise ValueError('投票服务未启动')
                    poll = self.live_polls.get(data['poll_id'])
                result = store.add(key, version, data.get('title'), data.get('prompt', ''),
                                   poll=poll, choice=data.get('choice'))
            elif action == 'edit':
                result = store.edit(key, version, data.get('card_id'), data)
            elif action == 'program':
                if data.get('command') == 'attach_poll' and data.get('poll_id'):
                    if self.live_polls is None:
                        raise ValueError('投票服务未启动')
                    self.live_polls.get(data['poll_id'])
                result = store.program_command(key, version, data.get('command'),
                                               card_id=data.get('card_id'), poll_id=data.get('poll_id'))
                if data.get('command') == 'enable':
                    self._program_armed_show = key
                elif data.get('command') == 'disable':
                    self._program_armed_show = None
            elif action == 'submit':
                submission = store.begin_submission(key, version, data.get('card_id'))
                job = await self._show_task_action({'action': 'submit_commission', **submission})
                result = store.submitted(key, data.get('card_id'), submission['key'], job)
            elif action in {'sync', 'close'}:
                show = store.editable(key, version)
                roots = list(dict.fromkeys(c['drawing_root'] for c in show['cards'] if c['drawing_root']))
                observations = {}
                if roots:
                    try:
                        observations = await asyncio.wait_for(self._show_task_action(
                            {'action': 'drawing_states', 'ids': roots}), timeout=8)
                    except (ValueError, RuntimeError, OSError, StopIteration, asyncio.TimeoutError):
                        observations = {root: {'error': '绘画状态未确认'} for root in roots}
                result = store.record_drawings(key, version, observations, close=action == 'close',
                                               allow_unconfirmed=data.get('allow_unconfirmed') is True)
            else:
                raise ValueError('未知栏目操作')
            await self.refresh_program_context(force=True)
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)[:300]}, status=400)
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            return web.json_response({'error': str(exc)[:300] or '操作尚未确认，请刷新后核对'}, status=503)

    async def _drawing_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'drawing.html', headers={'Cache-Control': 'no-store'})

    async def _drawing_data(self, request):
        self._tasks_access(request)
        try:
            from .drawing import load_drawing
            from .agent_tasks import artifact_path
            job = (await self.agent_tasks.fetch_job(request.match_info['job_id'])
                   if getattr(self.agent_tasks, 'remote', False)
                   else self.agent_tasks.get(request.match_info['job_id']))
            if job['kind'] != 'drawing' or job['state'] != 'succeeded' or 'drawing.json' not in job['artifacts']:
                raise ValueError('笔画尚未准备好')
            return web.json_response(load_drawing(artifact_path(self.agent_tasks.folder / job['id'], 'drawing.json')),
                                     headers={'Cache-Control': 'no-store'})
        except (ValueError, RuntimeError, OSError, StopIteration, AttributeError):
            raise web.HTTPNotFound(text='笔画尚未准备好或数据无效')

    async def _drawing_state(self, request):
        self._tasks_access(request)
        try:
            data = {'action': 'drawing_state', 'id': request.match_info['job_id']}
            if getattr(self.agent_tasks, 'remote', False):
                result = await self.agent_tasks.action(data)
            else:
                from .task_actions import task_action
                if self.agent_tasks is None:
                    raise ValueError('后台任务服务未连接')
                result = await task_action(self.agent_tasks, data)
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, RuntimeError, OSError, StopIteration, AttributeError) as exc:
            return web.json_response({'error': str(exc) or '绘画状态暂不可用'}, status=503)

    async def _tasks_status(self, request):
        self._tasks_access(request)
        if getattr(self.agent_tasks, 'remote', False):
            try:
                await self.agent_tasks.rpc('status')
            except RuntimeError:
                return web.json_response({'error':'后台任务服务未连接'}, status=503)
        status = (self.task_daemon.status() if getattr(self, 'task_daemon', None)
                  else self.agent_tasks.status() if self.agent_tasks else {'enabled': False, 'jobs': []})
        return web.json_response(status,
                                 headers={'Cache-Control': 'no-store'})

    async def _runtime_health(self, request):
        self._tasks_access(request)
        return web.json_response({**self.runtime_health, **self.runtime_snapshot()},
                                 headers={'Cache-Control':'no-store'})

    def runtime_snapshot(self):
        supervision = self.runtime_supervisor.summary() if self.runtime_supervisor else None
        persistence = self.persistence_status() if self.persistence_status else {}
        task_service = (self.agent_tasks.status() if self.agent_tasks else {'enabled': False})
        ready = ((self.runtime_started and supervision['ready']) if supervision is not None
                 else getattr(self, 'runtime_health', {}).get('ready', False))
        ready = ready and not any(item.get('last_error') for item in persistence.values())
        core_ready = bool(ready)
        if task_service.get('enabled') and task_service.get('connected') is False:
            ready = False
        retention = self.audio_retention.status() if self.audio_retention else {}
        warnings = list(supervision.get('warnings', [])) if supervision else []
        recovery = self.broadcast_status() if getattr(self, 'broadcast_status', None) else {}
        if recovery.get('automatic_paused'):
            warnings.append('broadcast_recovery')
        if retention.get('last_error') and 'audio_retention' not in warnings:
            warnings.append('audio_retention')
        capabilities = self.capability_snapshot()
        if capabilities.get('voice', {}).get('ready') is False:
            warnings.append('voice_readiness')
        return {'ready': bool(ready), 'core_ready': core_ready, 'capabilities': capabilities,
                'workers': supervision['workers'] if supervision else {},
                'upgrade_idle': bool(self.runtime_started and self.upgrade_idle()),
                'warnings': warnings,
                'persistence': persistence,
                'tasks_connected': task_service.get('connected'),
                'audio_retention': retention,
                'broadcast_recovery': recovery,
                'connections': {'active': len(self._outboxes), 'slow_disconnected': self._slow_connections,
                                'queued': sum(len(box.pending) for box in self._outboxes.values()),
                                'coalesced': sum(box.coalesced for box in self._outboxes.values())},
                'metrics': self.metrics.summary() if self.metrics else {}}

    def capability_snapshot(self):
        """Explicit per-service readiness without exposing account identities or prompts."""
        capabilities = {}
        services = getattr(self, 'services', None)
        monitor = getattr(services, 'voice_readiness', None)
        if monitor is not None:
            capabilities['voice'] = monitor.snapshot()
        qq = getattr(self, 'qq_channel', None)
        if qq is not None:
            capabilities['qq'] = dict(ready=bool(qq.connected),
                state='ready' if qq.connected else 'unavailable',
                detail='QQ 通道已连接' if qq.connected else 'QQ 通道尚未连接，请在 QQ 页面连接')
        activity = getattr(self, 'bilibili_activity', None)
        if activity is not None:
            browser = activity.browser.snapshot()
            linked = bool(browser['connected'] and browser.get('account', {}).get('uid'))
            capabilities['bilibili'] = dict(ready=linked,
                state='ready' if linked else 'unavailable',
                detail='活动页与账号已确认，动作仍以助手回执为准' if linked
                else '请在已安装助手的浏览器打开活动页并读取账号')
        return capabilities

    async def _runtime_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'runtime.html', headers={'Cache-Control': 'no-store'})

    async def _runtime_status(self, request):
        self._tasks_access(request)
        diagnostics = dict(self.reply_diagnostics)
        if self.selective_memory and diagnostics:
            recall = self.selective_memory.recall_snapshot()
            if (recall.get('turn_id') == diagnostics.get('turn_id')
                    and diagnostics.get('sources', {}).get('memory_recall', {}).get('status') in {'ready', 'empty'}):
                diagnostics['recall'] = recall
        if self.cognition_delivery:
            diagnostics['delivery'] = self.cognition_delivery()
        return web.json_response({**self.runtime_snapshot(), 'reply_context': diagnostics},
                                 headers={'Cache-Control': 'no-store'})

    async def _rehearsal_page(self, request):
        self._tasks_access(request)
        return web.FileResponse(STATIC_DIR / 'rehearsal.html', headers={'Cache-Control': 'no-store'})

    async def _broadcast_action(self, request):
        self._tasks_access(request, write=True)
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.get('action') != 'prepare_standby':
                raise ValueError('未知直播恢复操作')
            if self.on_prepare_standby is None:
                raise ValueError('主控尚未就绪')
            await self.on_prepare_standby()
            return web.json_response({'ok': True}, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError) as error:
            return web.json_response({'error': str(error)[:160]}, status=400)
        except Exception:
            return web.json_response({'error': '提示音准备未确认，请刷新状态后重试'}, status=503)

    async def _rehearsal_status(self, request):
        self._tasks_access(request)
        result = self.rehearsal.snapshot() if self.rehearsal else {
            'available': False, 'running': False, 'state': 'idle', 'run_id': None,
            'automatic_passed': False, 'full_passed': False, 'steps': {}}
        return web.json_response(result, headers={'Cache-Control': 'no-store'})

    async def _rehearsal_action(self, request):
        self._tasks_access(request, write=True)
        if self.rehearsal is None:
            return web.json_response({'error': '开播演练尚未就绪'}, status=503)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('演练请求格式无效')
            action = data.get('action')
            if action == 'start' and set(data) == {'action'}:
                result = await self.rehearsal.start()
            elif action == 'cancel' and set(data) == {'action', 'run_id'}:
                result = await self.rehearsal.cancel(data['run_id'])
            elif action == 'confirm' and set(data) == {'action', 'run_id', 'field', 'passed'}:
                result = self.rehearsal.confirm(data['run_id'], data['field'], data['passed'])
            else:
                raise ValueError('演练操作格式无效')
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError) as error:
            return web.json_response({'error': str(error)[:240]}, status=400)

    async def _audio_library(self, request):
        self._tasks_access(request)
        if not self.audio_retention:
            return web.json_response({'items': [], 'status': {}, 'available': False})
        try:
            items = await self.audio_retention.list_files(limit=100)
            return web.json_response({'items': items, 'status': self.audio_retention.status(),
                                      'available': True}, headers={'Cache-Control': 'no-store'})
        except (OSError, ValueError):
            return web.json_response({'error': '音频目录暂不可用'}, status=503)

    async def _audio_library_action(self, request):
        self._tasks_access(request, write=True)
        if not self.audio_retention:
            return web.json_response({'error': '音频管理尚未启动'}, status=503)
        try:
            data = await request.json()
            if (not isinstance(data, dict) or data.get('action') != 'pin'
                    or not isinstance(data.get('name'), str) or type(data.get('pinned')) is not bool):
                raise ValueError('收藏请求格式无效')
            result = await self.audio_retention.pin(data['name'], data['pinned'])
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except ValueError:
            return web.json_response({'error': '音频不存在或收藏请求无效'}, status=400)
        except OSError:
            return web.json_response({'error': '收藏未保存，请重试'}, status=503)

    async def _experience_report(self, request):
        self._tasks_access(request)
        return web.json_response({
            'version': 1, 'recorded_at': time.time(),
            'boot_id': getattr(self, 'runtime_health', {}).get('boot_id'),
            'metrics': self.metrics.summary() if self.metrics else {},
            'measurement': '本次主服务启动后的最近样本；开口依据浏览器播放回执，未测量实际扬声器声音。',
            'voice_review': '浏览器最后一段转写完成至确认发送，包含人工修改等待。',
        }, headers={'Cache-Control': 'no-store',
                    'Content-Disposition': 'attachment; filename="companion-experience.json"'})

    async def _tasks_action(self, request):
        self._tasks_access(request, write=True)
        if not self.agent_tasks:
            return web.json_response({'error': '任务服务未启动'}, status=503)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            action = data.get('action')
            if action == 'tools':
                result = self.tool_registry.catalog() if self.tool_registry else []
            elif action == 'call_tool':
                if not self.tool_registry:
                    raise ValueError('工具目录未启动')
                result = await self.tool_registry.call(data.get('name'), data.get('arguments'), owner=True)
            elif action == 'release_stale' and getattr(self, 'task_daemon', None):
                result = await self.task_daemon.release_stale()
            elif getattr(self.agent_tasks, 'remote', False):
                result = await self.agent_tasks.action(data)
            else:
                from .task_actions import task_action
                result = await task_action(self.agent_tasks, data)
            return web.json_response(result)
        except (ValueError, RuntimeError, OSError, StopIteration) as exc:
            return web.json_response({'error': str(exc)[:1000]}, status=400)

    async def _task_artifact(self, request):
        self._tasks_access(request)
        try:
            from .agent_tasks import artifact_path
            job = (await self.agent_tasks.fetch_job(request.match_info['job_id'])
                   if getattr(self.agent_tasks, 'remote', False)
                   else self.agent_tasks.get(request.match_info['job_id']))
            name = request.match_info['name']
            if name not in job['artifacts']:
                raise ValueError('未登记的产物')
            path = artifact_path(self.agent_tasks.folder / job['id'], name)
            response = web.FileResponse(path, headers={'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'"})
            if path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
                response.headers['Content-Disposition'] = 'attachment'
            return response
        except (ValueError, RuntimeError, StopIteration, AttributeError):
            raise web.HTTPNotFound()

    def _tasks_access(self, request, *, write=False):
        self._local_access(request, write=write)

    async def _minecraft_status(self, request):
        self._minecraft_access(request)
        return web.json_response({**self.minecraft.state, 'history': self.minecraft.history},
                                 headers={'Cache-Control': 'no-store'})

    async def _minecraft_action(self, request):
        self._minecraft_access(request, write=True)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            await self.minecraft.command(str(data.get('action', '')), str(data.get('target', '')))
            return web.json_response({'ok': True, 'message': '指令已提交，请看实际回执'})
        except (ValueError, OSError, RuntimeError) as exc:
            return web.json_response({'error': str(exc)[:240]}, status=400)

    async def _danmaku_page(self, request):
        return web.FileResponse(STATIC_DIR / 'danmaku.html')

    async def _danmaku_ws(self, request):
        self._websocket_access(request)
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1024)
        await ws.prepare(request)
        self._danmaku_sockets.add(ws)
        try:
            self._send_socket(ws, {'type':'history', 'items':list(self._danmaku_history)})
            async for _ in ws:
                pass  # Display-only connection; never accepts chat/control commands.
        finally:
            await self._drop_socket(ws)
        return ws

    async def broadcast_danmaku(self, item):
        self._danmaku_history.append(item)
        for ws in tuple(self._danmaku_sockets):
            self._send_socket(ws, {'type':'danmaku', 'item':item})

    async def _songs(self, request):
        from .songs import catalog
        return web.json_response(await asyncio.to_thread(catalog, self._songs_folder))

    async def _music_requests(self, request):
        return web.json_response({'items': self.music_requests.items}, headers={'Cache-Control': 'no-store'})

    async def _suno_inbox(self, request):
        self._local_access(request)
        return web.json_response({'files':self.suno_manual.candidates()}, headers={'Cache-Control':'no-store'})

    async def _suno_action(self, request):
        self._local_access(request, write=True)
        try:
            data = await request.json()
            job = self.suno_manual.job(str(data.get('id','')))
            if data.get('action') == 'save':
                job.update(draft=self.suno_manual.validate_draft(data.get('draft')), state='waiting_suno', detail='文稿已保存，请在Suno网页生成。')
                self.music_requests.save()
            elif data.get('action') == 'retry':
                if job['state'] != 'draft_failed':
                    raise ValueError('当前状态不需要重试文稿。')
                job.update(state='awaiting_draft', detail='等待重新准备文稿。')
                self.music_requests.save()
            elif data.get('action') == 'import':
                song = await self.suno_manual.import_audio(job['id'], str(data.get('file','')))
                await self.broadcast({'type':'song_request','song':song})
            else:
                raise ValueError('未知操作。')
            await self.broadcast({'type':'music_status','text':job['detail']})
            return web.json_response({'ok':True})
        except (ValueError, TypeError, AttributeError) as exc:
            return web.json_response({'error':str(exc)}, status=400)
        except Exception:
            return web.json_response({'error':'操作未完成，请重试。'}, status=500)

    async def _media_page(self, request):
        return web.FileResponse(STATIC_DIR / 'bilibili.html', headers={'Cache-Control': 'no-store'})

    async def _qq_page(self, request):
        self._local_access(request)
        return web.FileResponse(STATIC_DIR / 'qq.html', headers={'Cache-Control': 'no-store',
                                                                'Referrer-Policy': 'no-referrer'})

    async def _qq_status(self, request):
        self._local_access(request)
        headers = {'Cache-Control': 'no-store'}
        if not self.qq_channel:
            return web.json_response({'error': 'QQ 聊天服务尚未启动'}, status=503, headers=headers)
        try:
            return web.json_response(self.qq_channel.snapshot(), headers=headers)
        except Exception:
            return web.json_response({'error': '暂时无法读取 QQ 状态，请稍后重试'}, status=503,
                                     headers=headers)

    async def _qq_action(self, request):
        from .qq_channel import QQError
        self._local_access(request, write=True)
        headers = {'Cache-Control': 'no-store'}
        if not self.qq_channel:
            return web.json_response({'error': 'QQ 聊天服务尚未启动'}, status=503, headers=headers)
        try:
            data = await request.json()
            if (not isinstance(data, dict) or not isinstance(data.get('action'), str)
                    or data['action'] not in {'connect', 'bind', 'disconnect', 'pair', 'unpair', 'reset', 'reply_mode'}):
                raise ValueError('invalid action')
            return web.json_response(await self.qq_channel.action(data), headers=headers)
        except QQError as exc:
            # QQError maps only fixed, user-facing codes; provider errors stay sanitized below.
            return web.json_response({'error': str(exc)}, status=400, headers=headers)
        except (ValueError, TypeError, KeyError):
            return web.json_response({'error': '请求无效，请检查操作和聊天称呼'}, status=400,
                                     headers=headers)
        except Exception:
            # Transport/provider exceptions can contain credentials; never echo them to the page.
            return web.json_response({'error': 'QQ 操作未完成，请刷新状态后重试'}, status=503,
                                     headers=headers)

    async def _activity_page(self, request):
        self._local_access(request)
        return web.FileResponse(STATIC_DIR / 'bilibili-activity.html', headers={'Cache-Control': 'no-store'})

    async def _activity_status(self, request):
        self._local_access(request)
        if not self.bilibili_activity:
            return web.json_response({'error': '自主活动服务尚未启动'}, status=503)
        return web.json_response(self.bilibili_activity.snapshot(), headers={'Cache-Control': 'no-store'})

    async def _activity_action(self, request):
        self._local_access(request, write=True)
        if not self.bilibili_activity:
            return web.json_response({'error': '自主活动服务尚未启动'}, status=503)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('活动请求格式无效')
            return web.json_response(await self.bilibili_activity.action(data), headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({'error': str(exc)[:300]}, status=400)
        except OSError:
            return web.json_response({'error': '活动记录写入失败，操作已停止'}, status=503)

    async def _activity_browser(self, request):
        self._local_access(request, write=True)
        if not self.bilibili_activity:
            return web.json_response({'error': '自主活动服务尚未启动'}, status=503)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('浏览器回执格式无效')
            broker = self.bilibili_activity.browser
            if data.get('action') == 'claim':
                result = broker.poll(data.get('active_id'))
            elif data.get('action') == 'result':
                result = {'ok': broker.result(data)}
            else:
                raise ValueError('未知浏览器操作')
            return web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError):
            return web.json_response({'error': '浏览器回执格式无效'}, status=400)

    async def _search_media(self, request):
        query = request.query.get('q', '').strip()
        if not query or len(query) > 100:
            return web.json_response({'error': '请输入1至100字的搜索词。'}, status=400)
        return web.json_response({
            'error': '请安装并连接本机浏览器点歌助手，再从官方B站页面读取结果。',
            'error_code': 'browser_required'}, status=409, headers={'Cache-Control': 'no-store'})

    async def _browser_media_action(self, request, action):
        from .bilibili_browser import BrowserResultError
        headers = {'Cache-Control': 'no-store'}
        try:
            if self.cfg.public_test:
                raise web.HTTPForbidden()
            self._websocket_access(request)  # Same local host and exactly one same-origin header.
        except web.HTTPForbidden:
            return web.json_response({'error': '浏览器点歌仅允许本机主控页面操作。',
                                      'error_code': 'forbidden'}, status=403, headers=headers)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise BrowserResultError('浏览器点歌请求格式无效。')
            allowed = {'request': {'query'}, 'result': {'request_id', 'page_url', 'results'},
                       'cancel': {'request_id', 'result_id'}}[action]
            if set(payload) - allowed:
                raise BrowserResultError('浏览器点歌请求字段无效。')
            if action == 'request':
                result = self._bilibili_browser.request(payload.get('query'))
            elif action == 'result':
                result = self._bilibili_browser.submit(payload.get('request_id'), payload.get('page_url'),
                                                       payload.get('results'))
            else:
                result = self._bilibili_browser.cancel(request_id=payload.get('request_id'),
                                                       result_id=payload.get('result_id'))
            return web.json_response(result, headers=headers)
        except BrowserResultError as exc:
            return web.json_response({'error': str(exc), 'error_code': exc.code}, status=exc.status, headers=headers)
        except (ValueError, TypeError, UnicodeError):
            return web.json_response({'error': '浏览器点歌请求格式无效。', 'error_code': 'invalid_result'},
                                     status=400, headers=headers)

    async def _browser_media_request(self, request):
        return await self._browser_media_action(request, 'request')

    async def _browser_media_result(self, request):
        return await self._browser_media_action(request, 'result')

    async def _browser_media_cancel(self, request):
        return await self._browser_media_action(request, 'cancel')

    async def _browser_video_action(self, request, action):
        from .bilibili_browser import BrowserResultError
        headers = {'Cache-Control': 'no-store'}
        try:
            if self.cfg.public_test:
                raise web.HTTPForbidden()
            self._websocket_access(request)
        except web.HTTPForbidden:
            return web.json_response({'error': '字幕读取仅允许本机主控页面操作。',
                                      'error_code': 'forbidden'}, status=403, headers=headers)

        async def read_body():
            # 12,000 Chinese subtitle characters can exceed the ordinary 32KB
            # form limit. Bound this route explicitly without enlarging others.
            body = bytearray()
            limit = 128000 if action == 'result' else 4096
            async for chunk in request.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > limit:
                    raise BrowserResultError('字幕回执超过长度上限。', status=413)
            return json.loads(body)

        try:
            payload = await asyncio.wait_for(read_body(), timeout=10)
            fields = {'request': {'query'}, 'result': {'request_id', 'evidence'},
                      'cancel': {'request_id', 'evidence_id'}}[action]
            if not isinstance(payload, dict) or set(payload) - fields:
                raise BrowserResultError('字幕读取请求字段无效。')
            store = self._bilibili_video_evidence
            if action == 'request':
                result = store.request(payload.get('query'))
            elif action == 'result':
                result = store.submit(payload.get('request_id'), payload.get('evidence'))
            else:
                result = store.cancel(request_id=payload.get('request_id'), evidence_id=payload.get('evidence_id'))
            return web.json_response(result, headers=headers)
        except BrowserResultError as exc:
            return web.json_response({'error': str(exc), 'error_code': exc.code}, status=exc.status, headers=headers)
        except (ValueError, TypeError, UnicodeError, RecursionError, asyncio.TimeoutError):
            return web.json_response({'error': '字幕读取请求无效或未完整收到，请重试。',
                                      'error_code': 'invalid_response'}, status=400, headers=headers)

    async def _browser_video_request(self, request):
        return await self._browser_video_action(request, 'request')

    async def _browser_video_result(self, request):
        return await self._browser_video_action(request, 'result')

    async def _browser_video_cancel(self, request):
        return await self._browser_video_action(request, 'cancel')

    async def _bilibili_status(self, request):
        from .danmaku import BILIBILI_STATUS
        status = dict(BILIBILI_STATUS)
        if status['state'] == 'connected' and time.time() - status['last_heartbeat'] > 65:
            status['state'] = 'reconnecting'
        return web.json_response(status)

    async def _cancel_bilibili_narration(self, ws, request_id=None):
        current = self._bilibili_narrations.get(ws)
        if current is None or (request_id is not None and current[0] != request_id):
            return
        self._bilibili_narrations.pop(ws, None)
        current[1].cancel()
        await asyncio.gather(current[1], return_exceptions=True)

    @property
    def bilibili_active(self):
        # Keep the existing broadcast/scheduler field while tracking the two
        # players independently. Stopping one must not release the other.
        return bool(self._bilibili_flows or self._song_flows)

    async def _set_bilibili_active(self, ws, active, *, media='bilibili'):
        before = self.bilibili_active
        previous_holders = self._bilibili_flows | self._song_flows
        holders = self._song_flows if media == 'local' else self._bilibili_flows
        acquiring = active and ws not in holders
        if active:
            holders.add(ws)
        else:
            holders.discard(ws)
        if acquiring:
            # A newly chosen player takes focus across pages. Keep the initiator
            # running; repeated active heartbeats must not stop another stage.
            for socket in previous_holders:
                if socket is not ws and socket in self._sockets:
                    self._send_socket(socket, {'type': 'media_focus'})
        if before != self.bilibili_active:
            await self.broadcast({'type': 'bilibili_playback_state', 'active': self.bilibili_active})

    async def _stop_media(self, requester=None):
        """Called under the media-operation lock: revoke first, then stop pages."""
        self._bilibili_flows.clear()
        self._song_flows.clear()
        for socket in tuple(self._bilibili_narrations):
            await self._cancel_bilibili_narration(socket)
        # The requesting page already stopped synchronously. Echoing this stop
        # can arrive after its next request and accidentally cancel the new song.
        for socket in tuple(self._sockets):
            if socket is not requester:
                self._send_socket(socket, {'type': 'song_control', 'action': 'stop'})
        await self.broadcast({'type': 'bilibili_playback_state', 'active': False})

    async def _local_song_narration_text(self, stage, data):
        if stage != 'found':
            return {
                'ack': '好呀，我找找这首。',
                'results': '你可以在歌单里选一下想听的版本，选好我再放。',
                'error': '这次还没确认要播放的歌曲，先不放了。可以换个歌名，或在歌单里重新选择。',
            }[stage]
        song = data.get('song')
        if not isinstance(song, dict):
            raise ValueError('没有可确认的歌曲，请重新选择。')
        if song.get('local_file') is True:
            # A browser object URL cannot be inspected here. Accept only the
            # selected filename metadata; never fetch a blob or accept audio.
            title = song.get('title')
            if (set(song) - {'local_file', 'title'} or not isinstance(title, str)
                    or not 1 <= len(title) <= 120 or not title.strip()
                    or any(ord(char) < 32 or ord(char) == 127 for char in title)):
                raise ValueError('本地文件名称无效，请重新选择音频文件。')
            return f'就是这首，《{title.strip()}》。来，一起听吧。'
        url = song.get('url')
        if not isinstance(url, str) or not re.fullmatch(r'/songs/[a-f0-9]{24}', url):
            raise ValueError('歌曲链接无效，请从本地歌单重新选择。')
        from .songs import catalog
        songs = await asyncio.to_thread(catalog, self._songs_folder)
        actual = next((item for item in songs if item['url'] == url), None)
        if actual is None:
            raise ValueError('这首歌已不在本地曲库中，请刷新歌单重新选择。')
        # The id and actual catalog title are authoritative, never the title in
        # the browser request. Bound spoken filenames independently of query.
        title = re.sub(r'\s+', ' ', actual['title']).strip()[:120]
        return f'找到了，是《{title}》。来，一起听吧。'

    async def _start_bilibili_narration(self, ws, data, *, media='bilibili'):
        """Keep the socket reader free to receive the real speech completion receipt."""
        request_id, stage, query = (data.get(key) for key in ('request_id', 'stage', 'query'))
        if (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id)
                or not isinstance(stage, str) or stage not in {'ack', 'found', 'direct', 'results', 'error', 'review'}
                or not isinstance(query, str) or not 1 <= len(query.strip()) <= 500):
            return
        current = self._bilibili_narrations.get(ws)
        if current is not None and current[0] == request_id:
            return
        await self._cancel_bilibili_narration(ws)

        async def run():
            result = {'completed': False, 'reason': '角色语音暂不可用，请重启主程序后重试。'}
            try:
                if self.on_bilibili_narrate is not None:
                    from .bilibili_media import extract_bvid
                    evidence = None
                    if stage == 'review':
                        if media != 'bilibili' or not self.bilibili_review_enabled:
                            raise ValueError('字幕评价尚未启用，请从主控制台重新点播。')
                        evidence = self._bilibili_video_evidence.snapshot(data.get('evidence_id'), query.strip())
                        if evidence.get('subtitle_status') != 'ready':
                            raise ValueError('没有可用字幕，不能据此评价视频内容。')
                    if media == 'local' and stage == 'direct':
                        raise ValueError('本地点歌不能使用视频直接播放请求。')
                    text = await self._local_song_narration_text(stage, data) if media == 'local' else {
                        'ack': ('好呀，收到这个视频了。' if extract_bvid(query.strip())
                                else '好呀，我去B站找找，挑播放量最高的版本给你听。'),
                        'results': '我找到几个结果了，你看看想听哪一个？选好我就放给你听。',
                        'error': {
                            'empty': '这次没有搜到匹配的视频。换个歌名或者 BV 号再试试吧。',
                            'network': '刚才连接B站时网络出了问题，还没查到结果。稍后再试试吧。',
                            'timeout': '这次B站响应超时了，我还没拿到结果。稍后再试试吧。',
                            'rate_limited': 'B站暂时限制了查询频率，我先停一下。等一会儿再查吧。',
                            'upstream_rejected': 'B站暂时拒绝了自动查询，我不能确认搜索结果。你可以先在官方搜索页看看。',
                            'invalid_response': 'B站这次返回的数据没法确认，我先不播放。稍后再试试吧。',
                            'browser_required': '请先安装并连接浏览器点歌助手，我再从B站页面帮你找。',
                            'browser_disconnected': '浏览器点歌助手还没连接上，连接好以后再试试吧。',
                            'verification_required': 'B站页面需要验证，请在正常的B站页面完成验证后再试。',
                            'login_required': 'B站页面需要登录，请你在浏览器里正常登录后再试。',
                            'page_unavailable': '这次没有读到可确认的B站页面结果，请检查页面后再试。',
                        }.get(data.get('error') if isinstance(data.get('error'), str) else '',
                              '这次B站查询没成功，还没拿到能确认的结果。稍后再试试，或者给我一个 BV 号吧。'),
                    }.get(stage)
                    if stage == 'review':
                        text = '根据这段视频已读取的字幕给出评价。'
                    elif media == 'bilibili' and stage == 'direct':
                        from .bilibili_browser import direct_video_reference
                        target = direct_video_reference(query)
                        if 'bvid' in data and data['bvid'] != target['bvid']:
                            raise ValueError('指定的视频与 BV 号不一致，请重新选择。')
                        # Syntax confirms the user's target only. No browser/API
                        # result, title, view count or availability is invented.
                        text = '就放你指定的这个视频，来一起听吧。'
                    elif media == 'bilibili' and stage == 'found':
                        bvid = data.get('bvid')
                        if not isinstance(bvid, str) or not re.fullmatch(r'BV[A-Za-z0-9]{10}', bvid):
                            raise ValueError('BV 号无效，未开始播放。')
                        # Use only the bounded official-page snapshot. Never
                        # trigger another upstream lookup after the user chose.
                        snapshot = self._bilibili_browser.snapshot(data.get('result_id'), query.strip())
                        results = snapshot['results']
                        item = next((item for item in results if item.get('bvid') == bvid), None)
                        if item is None:
                            raise ValueError('还没有确认这条视频，未开始播放，请重新查询。')
                        title = re.sub(r'<[^>]*>', '', str(item.get('title') or '')).strip()[:80]
                        text = f'找到了，是《{title}》。来，一起听吧。' if title else '找到了。来，一起听吧。'
                        if data.get('selection') == 'highest_play':
                            if snapshot['selection_reason'] != 'highest_play' or snapshot['auto_bvid'] != bvid:
                                raise ValueError('这次还不能确认合适的自动版本，请从结果里手动选择。')
                            text = f'这次搜到的结果里，播放量最高的是《{title or bvid}》。来，一起听吧。'
                    elif media == 'bilibili' and stage == 'results':
                        snapshot = self._bilibili_browser.snapshot(data.get('result_id'), query.strip())
                        results = snapshot['results']
                        if not results:
                            raise ValueError('没有可确认的搜索结果，请重新查询。')
                        if snapshot['selection_reason'] == 'missing_play_count':
                            text = '我搜到几个版本了，但这次没拿到可比较的播放量。你挑一个，我再放给你听。'
                        elif snapshot['selection_reason'] == 'below_play_threshold':
                            text = '这次还没找到合适的版本，你可以换个关键词，或者从结果里挑一个。'
                        elif snapshot['selection_reason'] == 'uncertain_play_count':
                            text = '这次页面上的播放量还不能确定哪个版本最高。你从结果里挑一个，我再放给你听。'
                    result = await self.on_bilibili_narrate({
                        'request_id': request_id, 'stage': stage, 'query': query.strip(),
                        'media': media,
                        'text': text, 'emotion': '开心' if stage in {'found', 'direct'} else '平静',
                        **({'evidence': evidence} if stage == 'review' else {}),
                    })
            except asyncio.CancelledError:
                raise
            except ValueError as error:
                result = {'completed': False, 'reason': str(error)}
            except Exception:
                result = {'completed': False, 'reason': '这次回应没有完成，可以重试或手动继续。'}
            finally:
                if stage == 'review' and not result.get('completed'):
                    result.setdefault('error_code', 'review_failed')
                current = self._bilibili_narrations.get(ws)
                if current is not None and current[1] is asyncio.current_task():
                    self._bilibili_narrations.pop(ws, None)
                    if ws in self._sockets:
                        self._send_socket(ws, {'type': 'bilibili_narration_done',
                                               'request_id': request_id, **result})

        self._bilibili_narrations[ws] = (request_id, asyncio.create_task(run()))

    async def _song(self, request):
        from .songs import resolve_song
        path = await asyncio.to_thread(resolve_song, self._songs_folder, request.match_info['song_id'])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def _audio(self, request):
        name = request.match_info["name"]
        # URL routes do not treat a backslash as a separator; Windows paths do.
        if not name or any(char in name for char in ('/', '\\', ':', '\x00')):
            raise web.HTTPNotFound()
        try:
            root = self.audio_dir.resolve(strict=True)
            path = (root / name).resolve(strict=True)
            path.relative_to(root)  # Also reject symlinks/junctions escaping the audio root.
            if path.suffix != '.wav' or not path.is_file():
                raise ValueError('not an audio file')
        except (OSError, ValueError, RuntimeError):
            raise web.HTTPNotFound() from None
        return web.FileResponse(path)

    async def _game_frame(self, request):
        """返回最近一帧目标游戏画面；禁用/未捕获时不暴露桌面内容。"""
        self._local_access(request)
        frame = self.game_agent.latest_jpeg if self.game_agent else None
        if not frame:
            raise web.HTTPNotFound
        return web.Response(
            body=frame,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    def _audio_role(self, ws):
        player = next(iter(self._sockets), None)
        priority = self._socket_priority.get(player, 0)
        return {'audio_owner': player is ws,
                'audio_target': 'obs' if priority == 2 else 'display' if priority == 1 else 'control',
                'audio_clients': len(self._sockets)}

    def private_followups_available(self):
        """Local authentication alone does not make an OBS output private."""
        tarot = getattr(self, 'tarot', None)
        try:
            if tarot is not None and tarot.live_active():
                return False
        except (OSError, sqlite3.Error):
            return False
        return bool(not self.cfg.public_test and not self._private_output_transitions
                    and not getattr(self, '_program_context', {}).get('live_mode')
                    and self._sockets
                    and all(ws in self._local_control_sockets
                            and self._socket_priority.get(ws) == 0 for ws in self._sockets))

    def _private_followup_payload(self, payload):
        if payload.get('type') in {'audio_control', 'audio_owner'}:
            return False  # Stop/clear must still reach the previous player.
        event = payload.get('event') or {}
        if isinstance(event, dict) and (event.get('companion_followup_token') or event.get('kind') == 'followup'):
            return True
        if payload.get('type') == 'event' and payload.get('kind') == 'followup':
            return True
        companion = getattr(getattr(self, 'services', None), 'companion', None)
        turn = payload.get('current_turn_id') if payload.get('type') == 'playback' else payload.get('turn_id', payload.get('id'))
        check = getattr(companion, 'is_followup_turn', None)
        return bool(callable(check) and check(turn))

    async def _block_private_followups(self):
        # Remove queued private audio before the current item, so cancellation
        # cannot dispatch another private segment to the incoming output.
        messages = []
        if self.playback:
            current = self.playback.current
            candidates = list(self.playback.queue) + ([current] if current else [])
            turns = dict.fromkeys(item.turn_id if item.turn_id is not None else item.id
                                 for item in candidates if self._private_followup_payload(item.message()))
            for turn in turns:
                messages.extend(self.playback.cancel_turn(turn, '私有提醒已停止，输出页面发生变化'))
        if self.on_private_followups_blocked:
            await self.on_private_followups_blocked()
        await self._broadcast_many(messages)

    def _notify_audio_roles(self, *, exclude=None):
        for socket in tuple(self._sockets):
            if socket is not exclude:
                self._send_socket(socket, {'type': 'audio_owner', **self._audio_role(socket)})

    async def _ws(self, request):
        owner = self._websocket_access(request)
        if self.cfg.public_test and self._sockets:
            busy = web.WebSocketResponse()
            await busy.prepare(request)
            await busy.send_json({'type': 'room_busy'})
            await busy.close(code=1013, message=b'Room occupied')
            return busy
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16384)
        await ws.prepare(request)
        priority = (2 if request.query.get('output') == 'live' else
                    1 if request.query.get('display') == '1' else 0) if not self.cfg.public_test else 0
        if priority:
            self._private_output_transitions += 1
            try:
                await self._block_private_followups()
            except BaseException:
                await ws.close()
                raise
            finally:
                self._private_output_transitions -= 1
        previous = next(iter(self._sockets), None)
        self._sockets[ws] = None
        self._socket_priority[ws] = priority
        if owner and priority == 0:
            self._local_control_sockets.add(ws)
        self._sockets = dict.fromkeys(sorted(self._sockets, key=lambda sock: -self._socket_priority.get(sock, 0)))
        if previous is not None and next(iter(self._sockets)) is not previous:
            self._send_socket(previous, {'type':'audio_control', 'action':'clear', 'id':None})
        self._send_socket(ws, self._public_message({
            "type": "hello",
            **self.info,
            **self._audio_role(ws),
            "conversation_state": self.conversation_status() if self.conversation_status else {},
            "mock_danmaku": self.mock_danmaku_enabled is not None,
            "mock_danmaku_enabled": (
                self.mock_danmaku_enabled.is_set()
                if self.mock_danmaku_enabled is not None else False
            ),
            "mock_game": self.mock_game_enabled is not None,
            "mock_game_enabled": (
                self.mock_game_enabled.is_set()
                if self.mock_game_enabled is not None else False
            ),
            "manual_mode": self.manual_mode.is_set() if self.manual_mode else False,
            "bilibili_narration": bool(self.on_bilibili_narrate and not self.cfg.public_test),
            "song_narration": bool(self.on_bilibili_narrate and not self.cfg.public_test),
            "music_protocol": 6,
            "bilibili_direct": bool(self.on_bilibili_narrate and not self.cfg.public_test),
            "bilibili_review": bool(self.bilibili_review_enabled and self.on_bilibili_narrate and not self.cfg.public_test),
            "bilibili_active": self.bilibili_active,
            "director": self.director.summary() if self.director else {"enabled": False},
            "playback": self.playback.summary() if self.playback else {"state": "idle", "queued": 0},
            "game_agent": self.game_agent.summary() if self.game_agent else {"enabled": False},
            "metrics": self.metrics.summary() if self.metrics else {},
            "selective_memory": (
                self.selective_memory.summary()
                if self.selective_memory else self.info.get("selective_memory", {})
            ),
            "personality": (
                self.personality.summary()
                if self.personality else self.info.get("personality", {})
            ),
        }))
        self._notify_audio_roles(exclude=ws)
        guest = '访客-' + secrets.token_hex(3)
        last_inject = 0.0
        if next(iter(self._sockets)) is ws and self.playback and self.playback.current:
            await self._broadcast_many(self.playback.redispatch_current())
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if not isinstance(data.get('type'), str):
                    continue
                allowed = getattr(self, 'allowed_ws_actions', None)
                if allowed is not None and data['type'] not in allowed:
                    continue
                if not owner and data.get('type') not in _GUEST_WS_ACTIONS:
                    continue  # Management commands require a local authenticated control page.
                if self.cfg.public_test:
                    if data.get('type') == 'inject':
                        if data.get('confirm_memory'):
                            continue
                        if data.get('source') not in {'voice','companion'} or data.get('kind','chat') != 'chat':
                            continue
                        if not isinstance(data.get('text'), str) or len(data['text']) > 500:
                            continue
                        if time.monotonic() - last_inject < .8:
                            continue
                        last_inject = time.monotonic()
                        data['user'] = guest
                if (data.get("type") in {"audio_started", "audio_finished", "audio_progress"}
                        and next(iter(self._sockets), None) is not ws):
                    continue
                if data.get("type") == "inject":
                    await self._inject(data, owner=owner)
                elif data.get('type') == 'bilibili_narrate' and not self.cfg.public_test:
                    if self._socket_priority.get(ws, 0) == 0:
                        async with self._media_operations:
                            await self._start_bilibili_narration(ws, data)
                elif data.get('type') == 'song_narrate' and not self.cfg.public_test:
                    async with self._media_operations:
                        if ws in self._sockets and (self._socket_priority.get(ws) == 0 or next(iter(self._sockets), None) is ws):
                            await self._start_bilibili_narration(ws, data, media='local')
                elif data.get('type') == 'bilibili_narration_cancel' and not self.cfg.public_test:
                    await self._cancel_bilibili_narration(ws, data.get('request_id'))
                elif data.get('type') == 'bilibili_playback_state' and not self.cfg.public_test:
                    if self._socket_priority.get(ws, 0) == 0 and isinstance(data.get('active'), bool):
                        async with self._media_operations:
                            await self._set_bilibili_active(ws, data['active'])
                elif data.get('type') == 'song_playback_state' and not self.cfg.public_test:
                    async with self._media_operations:
                        if ws in self._sockets and isinstance(data.get('active'), bool) and (
                                self._socket_priority.get(ws) == 0 or next(iter(self._sockets), None) is ws
                                or data['active'] is False and ws in self._song_flows):
                            await self._set_bilibili_active(ws, data['active'], media='local')
                elif data.get('type') == 'media_stop' and not self.cfg.public_test:
                    async with self._media_operations:
                        if ws in self._sockets and (self._socket_priority.get(ws) == 0 or next(iter(self._sockets), None) is ws):
                            await self._stop_media(requester=ws)
                elif data.get('type') == 'music_cancel' and not self.cfg.public_test:
                    self.music_requests.cancel(str(data.get('id', '')))
                    await self.broadcast({'type': 'music_status', 'text': '音乐待办已更新。'})
                elif data.get('type') == 'music_retry' and not self.cfg.public_test:
                    for job in self.music_requests.items:
                        if job['id'] == data.get('id') and job['mode'] == 'cover' and job['state'] in {'failed', 'cancelled'}:
                            job.update(state='needs_setup', detail='等待检查RVC配置和原曲。')
                            self.music_requests.save()
                    await self.broadcast({'type': 'music_status', 'text': '已重新检查翻唱任务。'})
                elif data.get('type') == 'music_play' and not self.cfg.public_test:
                    for job in self.music_requests.items:
                        if job['id'] == data.get('id') and job['state'] == 'ready' and job.get('song'):
                            await self.broadcast({'type': 'song_request', 'song': job['song']})
                elif data.get('type') == 'memory_manage':
                    await self._manage_memory(data, ws)
                elif data.get("type") == "conversation_interrupt":
                    await self._interrupt_conversation()
                elif data.get("type") == "set_mock_danmaku":
                    await self._set_mock_danmaku(bool(data.get("enabled")))
                elif data.get("type") == "set_mock_game":
                    await self._set_mock_game(bool(data.get("enabled")))
                elif data.get("type") == "set_manual_mode":
                    await self._set_manual_mode(bool(data.get("enabled")))
                elif data.get("type") == "set_director_phase":
                    await self._set_director_phase(str(data.get("phase", "auto")))
                elif data.get("type") == "audio_started":
                    await self._audio_started(data.get("id"))
                elif data.get("type") == "audio_progress":
                    self._audio_progress(data.get("id"), data.get("seconds"))
                elif data.get("type") == "audio_finished":
                    self._audio_progress(data.get("id"), data.get("seconds"))
                    await self._audio_finished(data.get("id"), str(data.get("reason", "ended")))
                elif data.get("type") == "playback_skip":
                    await self._playback_skip()
                elif data.get("type") == "playback_pause" and self.playback is not None:
                    await self._broadcast_many(self.playback.set_paused(bool(data.get("paused"))))
                elif data.get("type") == "playback_clear":
                    await self._playback_clear()
                elif data.get("type") == "set_game_observation":
                    await self._set_game_observation(bool(data.get("enabled")))
                elif data.get("type") == "set_game_control":
                    await self._set_game_control(bool(data.get("enabled")))
                elif data.get("type") == "game_emergency_stop":
                    await self._game_emergency_stop()
                elif data.get("type") == "game_reset_emergency":
                    await self._game_reset_emergency()
        finally:
            await self._drop_socket(ws)
        return ws

    async def _set_mock_danmaku(self, enabled: bool) -> None:
        """从测试台暂停或恢复模拟弹幕生成。"""
        if self.mock_danmaku_enabled is None:
            return
        if enabled and self.manual_mode is not None and self.manual_mode.is_set():
            await self.broadcast({"type": "mock_danmaku", "enabled": False})
            return
        if enabled:
            self.mock_danmaku_enabled.set()
        else:
            self.mock_danmaku_enabled.clear()
        await self.broadcast({"type": "mock_danmaku", "enabled": enabled})

    async def _set_mock_game(self, enabled: bool) -> None:
        """从测试台暂停或恢复模拟游戏事件生成。"""
        if self.mock_game_enabled is None:
            return
        if enabled and self.manual_mode is not None and self.manual_mode.is_set():
            await self.broadcast({"type": "mock_game", "enabled": False})
            return
        if enabled:
            self.mock_game_enabled.set()
        else:
            self.mock_game_enabled.clear()
        await self.broadcast({"type": "mock_game", "enabled": enabled})

    async def _set_manual_mode(self, enabled: bool) -> None:
        """手动模式暂停全部自动测试源和冷场事件。"""
        if self.manual_mode is None:
            return
        if enabled:
            self.manual_mode.set()
            if self.mock_danmaku_enabled is not None:
                self.mock_danmaku_enabled.clear()
            if self.mock_game_enabled is not None:
                self.mock_game_enabled.clear()
            if self.game_agent is not None:
                self.game_agent.set_control(False)
        else:
            self.manual_mode.clear()
        await self.broadcast({
            "type": "manual_mode", "enabled": enabled,
            "mock_danmaku_enabled": self.mock_danmaku_enabled.is_set() if self.mock_danmaku_enabled else False,
            "mock_game_enabled": self.mock_game_enabled.is_set() if self.mock_game_enabled else False,
        })
        if self.game_agent is not None:
            await self.broadcast({"type": "game_agent", **self.game_agent.summary()})

    async def _set_director_phase(self, phase: str) -> None:
        """在测试台锁定节目阶段，或恢复自动导演。"""
        if self.director is None or not self.director.set_phase(phase):
            return
        await self.broadcast({"type": "director", **self.director.summary()})

    async def _audio_started(self, event_id: object) -> None:
        try:
            parsed_id = int(event_id)
        except (TypeError, ValueError):
            return
        turn_id = parsed_id
        if self.playback is not None and (
            self.playback.current is None or self.playback.current.id != parsed_id
        ):
            return
        if self.playback is not None and self.playback.has_started:
            return
        if (
            self.playback is not None
            and self.playback.current is not None
            and self.playback.current.id == parsed_id
            and self.playback.current.turn_id is not None
        ):
            turn_id = self.playback.current.turn_id
        messages = []
        if self.playback is not None:
            item = self.playback.current
            messages = self.playback.audio_started(parsed_id)
            if self.playback.current is not item or not self.playback.has_started:
                await self._broadcast_many(messages)
                return
            if self.metrics is not None:
                self.metrics.record_stage("playback_wait", time.perf_counter() - item.ready_at)
        # Capture accepted receipt effects before broadcasting yields to controls
        # that can replace the current item.
        if getattr(self, 'rehearsal', None) and self.playback and self.playback.has_started:
            self.rehearsal.audio_started(turn_id)
        if self.metrics is not None:
            self.metrics.record_audio_started(turn_id)
            messages.append({"type": "metrics", **self.metrics.summary()})
        if self.director is not None and self.director.mark_audio_started(turn_id):
            messages.append({"type": "director", **self.director.summary()})
        await self._broadcast_many(messages)

    async def _interrupt_conversation(self) -> None:
        await self.event_queue.put(Event("voice", "interrupt", "", forced=True))

    def _audio_progress(self, audio_id: object, seconds: object) -> None:
        try:
            parsed_id = int(audio_id)
        except (TypeError, ValueError):
            return
        if self.playback:
            self.playback.audio_progress(parsed_id, seconds)

    async def _audio_finished(self, event_id: object, reason: str) -> None:
        if self.playback is None:
            return
        try:
            parsed_id = int(event_id)
        except (TypeError, ValueError):
            return
        await self._broadcast_many(self.playback.audio_finished(parsed_id, reason))

    async def _playback_skip(self) -> None:
        if self.playback is not None:
            await self._broadcast_many(self.playback.skip_current())

    async def _playback_clear(self) -> None:
        for socket in tuple(getattr(self, '_bilibili_narrations', {})):
            await self._cancel_bilibili_narration(socket)
        if self.playback is not None:
            await self._broadcast_many(self.playback.clear())

    async def _set_game_observation(self, enabled: bool) -> None:
        if self.game_agent is None:
            return
        self.game_agent.set_observation(enabled)
        await self.broadcast({"type": "game_agent", **self.game_agent.summary()})

    async def _set_game_control(self, enabled: bool) -> None:
        if self.game_agent is None:
            return
        self.game_agent.set_control(enabled)
        await self.broadcast({"type": "game_agent", **self.game_agent.summary()})

    async def _game_emergency_stop(self) -> None:
        if self.game_agent is None:
            return
        self.game_agent.emergency_stop("测试台急停")
        await self.broadcast({"type": "game_agent", **self.game_agent.summary()})

    async def _game_reset_emergency(self) -> None:
        if self.game_agent is None:
            return
        self.game_agent.reset_emergency()
        await self.broadcast({"type": "game_agent", **self.game_agent.summary()})

    async def _broadcast_many(self, messages: list[dict]) -> None:
        for message in messages:
            await self.broadcast(message)

    async def _inject(self, data: dict, *, owner=False) -> None:
        """从页面手动注入一个测试事件，和真实事件走同一条管线。"""
        kind = str(data.get("kind", "chat"))
        source = str(data.get("source", "companion"))
        if source not in ("companion", "danmaku", "game", "voice") or kind not in _KIND_PRIOS:
            return
        text = str(data.get("text", "")).strip()
        user = str(data.get("user", "你")).strip() or "你"
        if not text:
            return
        corrects = data.get("corrects")
        retract = data.get("retract") is True
        confirm_memory = data.get('confirm_memory') is True
        if (retract or confirm_memory) and corrects is None:
            return
        if corrects is not None:
            if source != "voice" or kind != "chat" or not isinstance(corrects, int) or isinstance(corrects, bool):
                return
        event = Event(source, kind, text, user=user, priority=_KIND_PRIOS[kind], forced=True,
                  corrects=corrects, resume_playback=data.get("resume_playback") is True,
                  retract=retract, confirm_memory=confirm_memory)
        event.owner_control = owner
        review_ms = data.get('voice_review_ms')
        if (self.metrics is not None and source == 'voice' and corrects is None
                and type(review_ms) in (int, float) and 0 <= review_ms <= 300000):
            self.metrics.record_stage('voice_review', review_ms / 1000)
        await self.event_queue.put(event)

    async def _manage_memory(self, data: dict, ws) -> None:
        if self.selective_memory is None:
            return
        action = data.get('action', 'list')
        ok = True
        if action in {'edit', 'forget'}:
            memory_id = data.get('id')
            if not isinstance(memory_id, int) or isinstance(memory_id, bool):
                return
            target = next((item for item in await self.selective_memory.list_memories()
                           if item['id'] == memory_id), None)
            if target is None or str(target.get('slot', '')).startswith('core:config:'):
                ok = False
            elif action == 'edit':
                ok = await self.selective_memory.edit_memory(memory_id, str(data.get('text', '')))
            else:
                ok = await self.selective_memory.forget(memory_id)
            if ok and getattr(self, 'companion', None) is not None:
                await self.companion.apply_memory_change(target)
            if ok and self.on_memory_change is not None:
                replacement = None
                if action == 'edit':
                    replacement = next((item['summary'] for item in await self.selective_memory.list_memories()
                                        if item.get('slot') == target['slot'] and item['status'] == 'active'), None)
                await self.on_memory_change(target, replacement)
        elif action != 'list':
            return
        self._send_socket(ws, {'type':'memory_list', 'ok':ok, 'action':action,
            'memories':await self.selective_memory.list_memories()})

    async def broadcast(self, payload: dict) -> None:
        private_followup = self._private_followup_payload(payload)
        if private_followup and not self.private_followups_available():
            if payload.get('type') == 'playback':
                payload = {**payload, 'current_text': ''}
            else:
                return
        if self.cfg.public_test:
            if payload.get('type') in {'learning','personality','selective_memory','metrics','game_agent','director'}:
                return
            payload = self._public_message(payload)
        if not self._sockets:
            return
        sockets = list(self._sockets)
        # 音频只能交给一个页面播放；其余 UI 消息仍广播给全部页面。
        if payload.get("type") in {"audio", "song_request"}:
            sockets = sockets[:1]
        elif payload.get('type') == 'bilibili_request':
            # The official video player needs visible controls. Keep it in one
            # console instead of a hidden OBS/display page or every open tab.
            controls = [ws for ws in sockets if self._socket_priority.get(ws, 0) == 0]
            if not controls:
                for ws in sockets:
                    self._send_socket(ws, {'type':'music_status', 'text':'请先打开控制台，再点播B站视频。'})
                return
            sockets = controls[:1]
        for ws in sockets:
            self._send_socket(ws, payload)

    def _send_socket(self, ws, payload) -> None:
        outbox = self._outboxes.get(ws)
        if outbox is None:
            async def failed():
                self._slow_connections += 1
                await self._drop_socket(ws)
                try:
                    await asyncio.wait_for(ws.close(code=1013, message=b'Slow connection'), 1.0)
                except Exception:
                    pass
            outbox = self._outboxes[ws] = SocketOutbox(ws.send_str, failed)
        kind = payload.get('type')
        state_key = kind if kind in {
            'metrics', 'arbiter', 'learning', 'personality', 'selective_memory',
            'director', 'game_agent', 'runtime_health',
            'audio_owner',
        } else None
        outbox.offer(json.dumps(payload, ensure_ascii=False), state_key=state_key)

    async def _drop_socket(self, ws) -> None:
        await self._cancel_bilibili_narration(ws)
        await self._set_bilibili_active(ws, False)
        await self._set_bilibili_active(ws, False, media='local')
        was_control_client = ws in self._sockets
        was_audio_client = next(iter(self._sockets), None) is ws
        self._sockets.pop(ws, None)
        self._socket_priority.pop(ws, None)
        self._local_control_sockets.discard(ws)
        self._danmaku_sockets.discard(ws)
        outbox = self._outboxes.pop(ws, None)
        try:
            if was_control_client:
                self._notify_audio_roles()
            if was_audio_client and getattr(self, 'rehearsal', None):
                self.rehearsal.playback_disconnected()
            if was_audio_client and self.playback and self.playback.current:
                await self._broadcast_many(self.playback.audio_finished(
                    self.playback.current.id, 'audio_client_disconnected'))
        finally:
            if outbox is not None:
                await outbox.close()

    async def close(self) -> None:
        for ws in tuple(self._outboxes):
            await self._drop_socket(ws)
            try:
                await asyncio.wait_for(ws.close(), 1.0)
            except Exception:
                pass
        try:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
        finally:
            self._bilibili_video_evidence.close()
            if self._owns_services:
                # Standalone test/task consoles retain their convenient local lifecycle.
                self.services.bilibili_activity = self.bilibili_activity
                self.services.qq_channel = self.qq_channel
                await self.services.close()
