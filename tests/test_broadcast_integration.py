"""Exercise actual main helpers with isolated stores, fake model and real playback."""
import asyncio
import itertools
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.arbiter import Arbiter
from brain.broadcast_recovery import BroadcastRecovery, TurnDeliveryTracker
from brain.config import PlaybackConfig
from brain.context_budget import ContextBudgetError
from brain.events import Event
from brain.metrics import RuntimeMetrics
from brain.playback import SpeechController
from brain.question_cards import QuestionCards
from brain.rehearsal import RehearsalService
from brain.speech_pipeline import SpeechPipeline
from tests.test_runtime_integration import main_function


class BroadcastIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cards = QuestionCards(Path(self.temporary.name) / 'questions.sqlite3')
        self.messages = []

        async def broadcast(message):
            self.messages.append(message)

        self.playback = SpeechController(PlaybackConfig(max_queue=4))
        self.voice = SimpleNamespace(speak=AsyncMock(return_value='C:/fixture/speech.wav'))
        self.pipeline = SpeechPipeline(self.voice, self.playback, broadcast)
        self.worker = asyncio.create_task(self.pipeline.run())
        self.addAsyncCleanup(self.pipeline.close)
        self.tasks = SimpleNamespace(handle_event=AsyncMock(), refresh_receipt=Mock(return_value=True),
                                     resolve_task_intent=AsyncMock(), rpc=AsyncMock(), jobs=[],
                                     intent_prompts={})
        self.webui = SimpleNamespace(rehearsal=None, _question_store=lambda: self.cards,
                                     question_event_payload=lambda _: {})
        self.context = dict(asyncio=asyncio, time=time, Event=Event, ids=itertools.count(100),
                            ContextBudgetError=ContextBudgetError,
                            services=SimpleNamespace(foreground=Mock(), voice_available=lambda: True),
                            speech_pipeline=self.pipeline, playback=self.playback,
                            webui=self.webui, recovery=BroadcastRecovery(failure_limit=1),
                            question_delivery=TurnDeliveryTracker(), cancelled_questions=set(),
                            active_turns=set(), failed_turns=set(), agent_tasks=self.tasks,
                            media_narrator=SimpleNamespace(active=False, cancel=AsyncMock()), tarot=None,
                            manual_mode=asyncio.Event(), responding=asyncio.Event(),
                            interrupt_generation=asyncio.Event(), arbiter=Arbiter(danmaku_cooldown_sec=0),
                            metrics=RuntimeMetrics(), memory=Mock(), personality=Mock(),
                            conversation=Mock(), remember_spoken=Mock(), audit_delivery=Mock(), learning=Mock(),
                            director=Mock(), persona=Mock(), push_event=broadcast,
                            voice_revisions=SimpleNamespace(latest=None, pending=[], advance=lambda _: []),
                            is_backchannel=lambda _: False, track_finalizer=Mock(),
                            settings=SimpleNamespace(broadcast=SimpleNamespace(reply_timeout_sec=1, wait_notice_sec=.1),
                                                     llm=SimpleNamespace(), persona_llm=SimpleNamespace()),
                            persona_cfg=SimpleNamespace(),
                            broadcast_notice=AsyncMock(), standby_path=None,
                            generate_event=AsyncMock())
        self.context['learning'].priority_bonus.return_value = 0
        self.context['director'].program_event_current.return_value = True
        self.context['conversation'].record_playback.return_value = None
        self.context['director'].poll_proactive.return_value = None
        self.context['director'].enabled = False
        for name in ('register_event', 'ask_question', 'cancel_question', 'delivered', 'reply_failed', 'process_event'):
            main_function(name, self.context)
        self.playback.on_delivery = self.context['delivered']

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 2)

    async def test_main_binds_speech_generation_before_waiting_for_model(self):
        started, release = asyncio.Event(), asyncio.Event()
        results = []
        async def generate(event):
            started.set()
            await release.wait()
            results.append(await self.pipeline.enqueue(
                turn_id=event.ui_id, segment_index=0, text='清空后生成的回答',
                emotion='平静', actions=[], event=event))
        self.context['generate_event'] = generate
        event = Event('companion', 'chat', '详细说明', user='你')
        event.ui_id = 200
        task = asyncio.create_task(self.context['process_event'](event))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.playback.clear()
            release.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].cancelled)
            self.voice.speak.assert_not_awaited()
            self.assertIsNone(self.playback.current)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_context_limit_reports_fixed_error_without_fabricated_reply(self):
        self.context['generate_event'] = AsyncMock(side_effect=ContextBudgetError('core_too_large'))
        event = Event('companion', 'chat', 'fixture question', user='你')
        event.ui_id = 220
        await self.context['process_event'](event)
        errors = [item for item in self.messages if item['type'] == 'reply_error']
        self.assertEqual(len(errors), 1)
        self.assertIn('上下文预算', errors[0]['error'])
        self.assertFalse(any(item['type'] in {'reply', 'reply_partial'} for item in self.messages))
        self.voice.speak.assert_not_awaited()

    def original_card(self, *, source='danmaku', text='请画一朵花并解释它的含义'):
        original = Event(source, 'chat', text, user='同名观众', viewer_key='bili:original')
        original.ui_id = 7
        card = self.cards.pin(self.cards.remember_event(original)['question_pin_token'])
        return original, self.cards.begin_answer(card['id'], card['version'])

    def bind_rehearsal(self, *, model_gate=None):
        instances = []
        configured_persona_llm = self.context['settings'].persona_llm

        class FakePersona:
            def __init__(self, persona_cfg, llm_cfg, memory, *, persona_llm):
                assert memory is None
                assert persona_llm is configured_persona_llm
                self.last_mode, self.last_emotion = 'llm', '平静'
                self.closed = False
                instances.append(self)

            async def stream_reply(self, event):
                if model_gate:
                    await model_gate.wait()
                yield SimpleNamespace(text='晚上好，声音测试已准备。', emotion='平静')

            async def close(self):
                self.closed = True

        self.context['Persona'] = FakePersona
        rehearse = main_function('rehearse', self.context)
        service = RehearsalService(rehearse, playback_timeout_sec=.5)
        self.webui.rehearsal = service
        self.addAsyncCleanup(service.close)
        return service, instances

    async def test_rehearsal_uses_real_pipeline_and_full_playback_without_memory_writes(self):
        service, instances = self.bind_rehearsal()
        await service.start()
        await self.until(lambda: self.playback.current is not None)
        item = self.playback.current
        self.assertEqual(item.event['rehearsal_id'], service.snapshot()['run_id'])
        self.assertFalse(service.snapshot()['automatic_passed'])
        self.playback.audio_started(item.id)
        service.audio_started(item.turn_id)
        self.playback.audio_finished(item.id)
        await service._task
        self.assertTrue(service.snapshot()['automatic_passed'])
        self.assertFalse(service.snapshot()['full_passed'])
        self.assertTrue(instances[0].closed)
        self.context['conversation'].record_playback.assert_not_called()
        self.context['remember_spoken'].assert_not_called()
        self.context['memory'].record.assert_not_called()
        self.context['personality'].record_reply.assert_not_called()
        self.assertEqual(self.context['metrics'].events, 0)
        self.assertEqual(self.context['question_delivery'].turns, {})

    async def test_clear_while_rehearsal_model_waits_rejects_its_late_voice(self):
        model_gate = asyncio.Event()
        service, instances = self.bind_rehearsal(model_gate=model_gate)
        await service.start()
        await self.until(lambda: service.snapshot()['steps']['llm']['state'] == 'running')
        self.playback.clear()
        model_gate.set()
        await asyncio.wait_for(service._task, 1)
        self.assertEqual(service.snapshot()['state'], 'failed')
        self.assertEqual(service.snapshot()['steps']['tts']['state'], 'failed')
        self.assertFalse(service.snapshot()['automatic_passed'])
        self.assertTrue(instances[0].closed)
        self.voice.speak.assert_not_awaited()
        self.assertIsNone(self.playback.current)
        self.assertFalse(any(message['type'] == 'audio' for message in self.messages))

    async def test_rehearsal_cancel_cleans_only_probe_turn_and_never_saves_playback(self):
        service, instances = self.bind_rehearsal()
        run = await service.start()
        await self.until(lambda: self.playback.current is not None)
        probe = self.playback.current
        other = Event('system', 'ambient', '后来排队的正式内容')
        other.ui_id = 999
        await self.pipeline.enqueue(turn_id=999, segment_index=0, text=other.text, emotion='平静',
                                    actions=[], event=other, prepared_audio_path='C:/fixture/other.wav')
        await service.cancel(run['run_id'])
        self.assertEqual(service.snapshot()['state'], 'cancelled')
        self.assertIsNotNone(self.playback.current)
        self.assertEqual(self.playback.current.turn_id, 999)
        self.assertFalse(service.audio_finished(probe.turn_id))
        self.assertTrue(instances[0].closed)
        self.context['conversation'].record_playback.assert_not_called()
        self.context['remember_spoken'].assert_not_called()

    async def test_rehearsal_cancel_during_model_request_closes_isolated_persona(self):
        service, instances = self.bind_rehearsal(model_gate=asyncio.Event())
        run = await service.start()
        await self.until(lambda: bool(instances))
        await service.cancel(run['run_id'])
        self.assertTrue(instances[0].closed)
        self.assertIsNone(self.playback.current)
        self.voice.speak.assert_not_awaited()
        self.context['memory'].record.assert_not_called()

    async def test_question_keeps_original_identity_and_bypasses_task_routing(self):
        original, card = self.original_card(source='voice')
        turn = await self.context['ask_question'](card)
        event = self.context['arbiter'].pick(allow_idle=False)
        self.assertEqual((event.source, event.user, event.viewer_key, event.text),
                         (original.source, original.user, original.viewer_key, original.text))
        self.assertEqual(event.host_question_id, card['id'])
        self.assertEqual(event.host_question_attempt, card['attempt'])
        self.assertEqual(event.ui_id, turn)
        self.assertTrue(event.forced)
        self.tasks.handle_event.assert_not_awaited()
        await self.context['process_event'](event)
        self.tasks.resolve_task_intent.assert_not_awaited()
        self.context['generate_event'].assert_awaited_once_with(event)

    async def test_saved_question_is_not_dropped_as_duplicate_of_pending_original(self):
        original, card = self.original_card()
        self.context['arbiter'].push(original)
        turn = await self.context['ask_question'](card)
        selected = self.context['arbiter'].pick(allow_idle=False)
        self.assertEqual(selected.ui_id, turn)
        self.assertEqual(selected.host_question_id, card['id'])
        self.assertIsNone(self.context['arbiter'].pick(allow_idle=False), 'do not immediately answer original twice')

    async def test_question_is_answered_only_after_generation_and_every_segment_plays(self):
        _, card = self.original_card()
        turn = await self.context['ask_question'](card)
        event = self.context['arbiter'].pick(allow_idle=False)
        tracker = self.context['question_delivery']
        for index in (0, 1):
            tracker.expect(turn, index)
            await self.pipeline.enqueue(turn_id=turn, segment_index=index, text=f'回答第{index}段', emotion='平静',
                                        actions=[], event=event, prepared_audio_path=f'C:/fixture/{index}.wav')
        first = self.playback.current
        self.playback.audio_started(first.id)
        self.playback.audio_finished(first.id)
        self.assertEqual(self.cards.get(card['id'])['state'], 'answering')
        tracker.generated(turn, True)
        self.assertEqual(self.cards.get(card['id'])['state'], 'answering')
        last = self.playback.current
        self.playback.audio_started(last.id)
        self.playback.audio_finished(last.id)
        self.assertEqual(self.cards.get(card['id'])['state'], 'answered')

    async def test_question_incomplete_playback_returns_to_waiting(self):
        _, card = self.original_card()
        turn = await self.context['ask_question'](card)
        event = self.context['arbiter'].pick(allow_idle=False)
        tracker = self.context['question_delivery']
        tracker.expect(turn, 0)
        tracker.generated(turn, True)
        await self.pipeline.enqueue(turn_id=turn, segment_index=0, text='未播完的回答', emotion='平静',
                                    actions=[], event=event, prepared_audio_path='C:/fixture/answer.wav')
        self.playback.audio_started(self.playback.current.id)
        self.playback.skip_current()
        result = self.cards.get(card['id'])
        self.assertEqual(result['state'], 'waiting')
        self.assertIsNone(result['answered_at'])

    async def test_unstarted_skipped_question_is_recovered_by_main_maintenance(self):
        _, card = self.original_card()
        turn = await self.context['ask_question'](card)
        event = self.context['arbiter'].pick(allow_idle=False)
        tracker = self.context['question_delivery']
        tracker.expect(turn, 0)
        tracker.generated(turn, True)
        await self.pipeline.enqueue(turn_id=turn, segment_index=0, text='尚未播放', emotion='平静',
                                    actions=[], event=event, prepared_audio_path='C:/fixture/answer.wav')
        self.playback.skip_current()
        self.assertNotEqual(self.cards.get(card['id'])['state'], 'answered')
        self.webui.refresh_program_context = AsyncMock()
        self.tasks.notification_tick = Mock()
        self.context['settings'].webui = SimpleNamespace(public_test=False)
        self.context.update(protect_audio=lambda: None, live_polls=None)
        maintenance = main_function('maintenance_loop', self.context)
        task = asyncio.create_task(maintenance())
        try:
            await self.until(lambda: self.cards.get(card['id'])['state'] == 'waiting')
            self.assertIsNone(self.cards.get(card['id'])['answered_at'])
            self.assertNotIn(turn, tracker.turns)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_fallback_delivery_never_saves_memory_or_marks_question_answered(self):
        item = SimpleNamespace(event={'is_fallback': True}, turn_id=123, segment_index=0)
        self.context['delivered'](item, True)
        self.context['conversation'].record_playback.assert_not_called()
        self.context['remember_spoken'].assert_not_called()

    async def test_reply_timeout_cleans_speech_question_and_active_state(self):
        _, card = self.original_card()
        turn = await self.context['ask_question'](card)
        event = self.context['arbiter'].pick(allow_idle=False)
        generation_cancelled = asyncio.Event()

        async def generate(_):
            try:
                await asyncio.Event().wait()
            finally:
                generation_cancelled.set()

        self.context['generate_event'] = generate
        self.context['active_turns'].add(turn)
        await asyncio.wait_for(self.context['process_event'](event), 2)
        self.assertTrue(generation_cancelled.is_set())
        self.assertFalse(self.context['responding'].is_set())
        self.assertNotIn(turn, self.context['active_turns'])
        self.assertIn(turn, self.context['failed_turns'])
        self.assertEqual(self.cards.get(card['id'])['state'], 'waiting')
        self.assertEqual(self.context['metrics'].errors, 1)
        self.assertFalse(self.context['recovery'].allow_automatic())
        notices = self.context['broadcast_notice'].await_count
        await asyncio.sleep(.12)
        self.assertEqual(self.context['broadcast_notice'].await_count, notices)

    async def test_explicit_cancel_cleans_speech_question_and_active_state(self):
        _, card = self.original_card()
        turn = await self.context['ask_question'](card)
        event = self.context['arbiter'].pick(allow_idle=False)
        entered = asyncio.Event()

        async def generate(_):
            entered.set()
            await asyncio.Event().wait()

        self.context['generate_event'] = generate
        self.context['active_turns'].add(turn)
        task = asyncio.create_task(self.context['process_event'](event))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.context['responding'].is_set())
        self.assertNotIn(turn, self.context['active_turns'])
        self.assertEqual(self.cards.get(card['id'])['state'], 'waiting')
        self.context['conversation'].mark_interrupted.assert_called_once_with(event)

    async def test_recovery_pauses_automatic_replies_but_allows_host_questions(self):
        now = [0.0]
        recovery = BroadcastRecovery(failure_limit=1, cooldown_sec=30, clock=lambda: now[0])
        recovery.failure('llm', 1)
        self.context['recovery'] = recovery
        self.context['persona'].behavior.allow_proactive = False
        automatic = Event('danmaku', 'chat', '自动弹幕', user='甲', viewer_key='bili:1')
        automatic.ui_id = 1
        manual = Event('danmaku', 'chat', '主播指定回答', user='乙', viewer_key='bili:2', forced=True)
        manual.ui_id = 2
        self.context['arbiter'].push(automatic)
        self.context['arbiter'].push(manual)
        seen = []

        async def process(event):
            seen.append(event.ui_id)
            self.context['active_turns'].discard(event.ui_id)

        self.context['process_event'] = process
        scheduler = main_function('scheduler_loop', self.context)
        task = asyncio.create_task(scheduler())
        try:
            await self.until(lambda: bool(seen))
            await asyncio.sleep(.12)
            self.assertEqual(seen, [2])
            self.assertEqual(self.context['arbiter'].pending_turn_ids(), {1})
            now[0] = 31
            await self.until(lambda: 1 in seen)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_media_narration_and_video_hold_scheduler_until_media_finishes(self):
        self.context['persona'].behavior.allow_proactive = True
        self.context['director'].enabled = True
        event = Event('danmaku', 'chat', '播放期间的弹幕', viewer_key='bili:1')
        event.ui_id = 10
        self.context['arbiter'].push(event)
        self.context['media_narrator'].active = True
        seen = []

        async def process(item):
            seen.append(item.ui_id)
            self.context['active_turns'].discard(item.ui_id)

        self.context['process_event'] = process
        task = asyncio.create_task(main_function('scheduler_loop', self.context)())
        try:
            await asyncio.sleep(.12)
            self.assertEqual(seen, [])
            self.assertFalse(self.context['director'].poll_proactive.call_args.kwargs['allow'])
            self.webui.bilibili_active = True
            self.context['media_narrator'].active = False
            await asyncio.sleep(.12)
            self.assertEqual(seen, [])
            self.assertEqual(self.context['arbiter'].pending_turn_ids(), {10})
            self.webui.bilibili_active = False
            await self.until(lambda: bool(seen))
            self.assertEqual(seen, [10])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_live_tarot_holds_proactive_topics_but_still_accepts_viewer_chat(self):
        self.context['persona'].behavior.allow_proactive = True
        self.context['director'].enabled = True
        self.context['tarot'] = SimpleNamespace(live_active=Mock(return_value=True))
        event = Event('danmaku', 'chat', '这张牌的图案真好看', viewer_key='bili:1')
        event.ui_id = 10
        self.context['arbiter'].push(event)
        seen = []

        async def process(item):
            seen.append(item.ui_id)
            self.context['active_turns'].discard(item.ui_id)

        self.context['process_event'] = process
        task = asyncio.create_task(main_function('scheduler_loop', self.context)())
        try:
            await self.until(lambda: bool(seen))
            self.assertEqual(seen, [10])
            self.assertFalse(self.context['director'].poll_proactive.call_args.kwargs['allow'])
            self.context['tarot'].live_active.return_value = False
            await self.until(lambda: self.context['director'].poll_proactive.call_args.kwargs['allow'])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_guided_tarot_also_filters_audience_messages_queued_before_selection(self):
        self.context['tarot'] = SimpleNamespace(live_active=lambda: True, holds_chat=lambda: True)
        for index, (kind, text, priority) in enumerate((('chat', '插个话题', 10), ('gift', '礼物', 80)), 10):
            event = Event('danmaku', kind, text, viewer_key=f'bili:{index}', priority=priority)
            event.ui_id = index
            self.context['arbiter'].push(event)
        seen = []
        async def process(item):
            seen.append(item.text)
            self.context['active_turns'].discard(item.ui_id)
        self.context['process_event'] = process
        task = asyncio.create_task(main_function('scheduler_loop', self.context)())
        try:
            await self.until(lambda: not self.context['arbiter'].pending_turn_ids())
            self.assertEqual(seen, [])
            urgent = Event('danmaku', 'chat', '我现在想伤害自己', viewer_key='bili:guest')
            urgent.ui_id = 12
            self.context['arbiter'].push(urgent)
            await self.until(lambda: bool(seen))
            self.assertEqual(seen, [urgent.text])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
