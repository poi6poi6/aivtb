"""Real voice queue and playback receipts with a fake local TTS provider."""
import ast
import asyncio
import itertools
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.config import PlaybackConfig
from brain.events import Event
from brain.media_narration import MediaNarrator
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline
from brain.task_client import TaskClient


def main_helper(name, namespace):
    path = Path(__file__).resolve().parents[1] / 'brain' / 'main.py'
    run = next(node for node in ast.parse(path.read_text('utf-8')).body
               if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run')
    function = next(node for node in run.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace[name]


class MediaNarrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = []
        self.ids = itertools.count(20)
        self.clock = time.time()
        self.playback = SpeechController(PlaybackConfig(start_timeout_sec=3, max_play_sec=10),
                                         clock=lambda: self.clock)
        self.voice = SimpleNamespace(speak=AsyncMock(return_value='C:/fixture/media.wav'))
        self.pipeline = SpeechPipeline(self.voice, self.playback, self.push, synthesis_timeout_sec=.5)
        self.interrupts = []

        async def interrupt(event):
            self.interrupts.append(event)
            await self.pipeline.interrupt_before(event.ui_id)

        self.narrator = MediaNarrator(allocate_id=lambda: next(self.ids), pipeline=self.pipeline,
                                     push=self.push, interrupt=interrupt, timeout_sec=1)
        self.hooks = {'media_narrator': self.narrator, 'playback': self.playback,
                      'agent_tasks': Mock(), 'audit_delivery': Mock(), 'question_delivery': Mock(),
                      'conversation': Mock(), 'remember_spoken': Mock(), 'recovery': Mock(),
                      'track_finalizer': Mock()}
        self.playback.on_state = main_helper('speech_state', self.hooks)
        self.playback.on_delivery = main_helper('delivered', self.hooks)
        self.pipeline.on_failure = main_helper('synthesis_failed', self.hooks)
        self.worker = asyncio.create_task(self.pipeline.run())
        self.tasks = []

    async def asyncTearDown(self):
        await self.narrator.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.pipeline.close()

    async def push(self, message):
        self.messages.append(message)

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 1)

    def narrate(self, *, request='request-1', stage='ack', text='好呀，我来找找这首歌。', media='bilibili'):
        task = asyncio.create_task(self.narrator.narrate({
            'request_id': request, 'stage': stage, 'query': '青鸟', 'text': text, 'media': media,
            'emotion': '开心' if stage in {'found', 'direct'} else '平静'}))
        self.tasks.append(task)
        return task

    def finish(self):
        item = self.playback.current
        self.playback.audio_started(item.id)
        self.playback.audio_finished(item.id)

    def start_review(self, reviewer):
        self.narrator.reviewer = reviewer
        task = asyncio.create_task(self.narrator.narrate({
            'request_id': 'review-fixture', 'stage': 'review', 'query': 'BV1B7411m7LV',
            'text': '请评价已读取的字幕。', 'evidence': {'subtitles': ['PUBLIC_SOURCE_NOT_FOR_AUDIO']}}))
        self.tasks.append(task)
        return task

    async def test_review_uses_final_text_and_requires_actual_audio_receipts_without_memory(self):
        reviewer = AsyncMock(return_value={'text': '字幕里把整理分成小步骤，这个办法很实用。', 'emotion': '平静'})
        task = self.start_review(reviewer)
        await self.until(lambda: self.playback.current is not None)
        reviewer.assert_awaited_once()
        self.assertFalse(task.done())
        self.assertEqual(self.playback.current.event['media_narration_stage'], 'review')
        self.assertNotIn('PUBLIC_SOURCE_NOT_FOR_AUDIO', str(self.messages))
        self.voice.speak.assert_awaited_once_with('字幕里把整理分成小步骤，这个办法很实用。', '平静')
        self.finish()
        self.assertTrue((await task)['completed'])
        self.hooks['conversation'].record_playback.assert_not_called()
        self.hooks['remember_spoken'].assert_not_called()

    async def test_cancel_while_review_model_waits_rejects_late_text_and_audio(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def reviewer(_):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # Simulate a transport ignoring its first cancellation.
            finished.set()
            return {'text': '这段迟到的评价不应该播出。', 'emotion': '平静'}

        task = self.start_review(reviewer)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(self.narrator.active)
        await self.narrator.cancel('new_chat')
        self.assertFalse((await asyncio.wait_for(task, 1))['completed'])
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        self.voice.speak.assert_not_awaited()
        self.assertFalse(any(message['type'] in {'reply', 'audio'} for message in self.messages))
        self.assertIsNone(self.playback.current)

    async def test_failed_or_malformed_review_never_falls_back_to_reading_prompt(self):
        for value in (RuntimeError('SECRET_PROVIDER_RESPONSE'), {'text': '', 'emotion': '平静'},
                      {'text': 'x' * 351, 'emotion': '平静'}):
            reviewer = AsyncMock(side_effect=value) if isinstance(value, Exception) else AsyncMock(return_value=value)
            result = await self.start_review(reviewer)
            self.assertFalse(result['completed'])
            self.assertNotIn('SECRET_PROVIDER_RESPONSE', str(result))
        self.voice.speak.assert_not_awaited()
        self.assertFalse(any(message['type'] == 'reply' for message in self.messages))

    async def test_cleared_playback_generation_rejects_late_review_text_as_well_as_audio(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def reviewer(_):
            entered.set()
            await release.wait()
            return {'text': '清空后不得再显示或播出的旧评价。', 'emotion': '平静'}

        task = self.start_review(reviewer)
        await asyncio.wait_for(entered.wait(), 1)
        self.playback.clear()
        release.set()
        self.assertFalse((await task)['completed'])
        self.voice.speak.assert_not_awaited()
        self.assertFalse(any(message['type'] in {'reply', 'audio'} for message in self.messages))

    async def test_voice_resource_preflight_prevents_unnecessary_review_model_call(self):
        self.narrator.resource_check = lambda: '语音资源未就绪。'
        reviewer = AsyncMock()
        result = await self.start_review(reviewer)
        self.assertEqual(result['error_code'], 'voice_resource_unavailable')
        reviewer.assert_not_awaited()

    def resource_client(self):
        # Exercise the real grant gate and main wiring without constructing an
        # HTTP client, loading task data or accessing the running coordinator.
        client = TaskClient.__new__(TaskClient)
        client.resource_busy = lambda: {'local_gpu': '语音资源预留'}
        client.connected = True
        client.last_sync = time.monotonic()
        client.granted = {'local_gpu'}
        client.jobs = []
        self.pipeline.resource_available = lambda: not client.resource_in_use('local_gpu')
        self.narrator.resource_check = main_helper('media_voice_resource_reason', {
            'speech_pipeline': self.pipeline, 'agent_tasks': client,
            'services': SimpleNamespace(voice_available=lambda: True),
            'settings': SimpleNamespace(tts=SimpleNamespace(backend='indextts2'))})
        return client

    async def test_unavailable_resource_rejects_before_ack_or_synthesis_with_actionable_reason(self):
        for state, phrase in [('disconnected', '任务协调服务未连接'), ('stale', '授权已过期'),
                              ('occupied', '正被后台任务占用'), ('ungranted', '尚未取得显卡授权')]:
            client = self.resource_client()
            if state == 'disconnected':
                client.connected = False
            elif state == 'stale':
                client.last_sync -= 3
            elif state == 'occupied':
                client.granted.clear()
                client.jobs = [{'kind': 'image', 'state': 'running', 'resource_profile': 'local_gpu'}]
            else:
                client.granted.clear()
            with self.subTest(state=state):
                result = await self.narrate(request=state)
                self.assertFalse(result['completed'])
                self.assertEqual(result['error_code'], 'voice_resource_unavailable')
                self.assertIn(phrase, result['reason'])
                self.assertIn('启动中心', result['reason'])
                self.assertNotIn('turn_id', result)
                self.assertTrue(client.resource_in_use('local_gpu'), 'preflight must never grant the GPU')
        self.assertEqual(self.messages, [])
        self.assertEqual(self.interrupts, [])
        self.assertFalse(self.pipeline.busy)
        self.voice.speak.assert_not_awaited()

    async def test_fresh_grant_restores_narration_after_disconnected_rejection(self):
        client = self.resource_client()
        client.connected = False
        self.assertEqual((await self.narrate())['error_code'], 'voice_resource_unavailable')
        client.connected = True
        client.last_sync = time.monotonic()
        task = self.narrate(request='fresh-grant')
        await self.until(lambda: self.playback.current is not None)
        self.voice.speak.assert_awaited_once()
        self.assertIsNone(client.resource_block_reason('local_gpu'))
        self.assertFalse(task.done())
        self.finish()
        self.assertTrue((await task)['completed'])

    async def test_new_resource_rejection_still_cancels_previous_narration(self):
        client = self.resource_client()
        old = self.narrate(request='old')
        await self.until(lambda: self.playback.current is not None)
        before = [m for m in self.messages if m['type'] in {'event', 'reply'}]
        client.connected = False
        result = await self.narrate(request='blocked-new')
        self.assertEqual(result['error_code'], 'voice_resource_unavailable')
        self.assertEqual((await old)['reason'], 'superseded')
        self.assertIsNone(self.playback.current)
        self.assertEqual([m for m in self.messages if m['type'] in {'event', 'reply'}], before)
        self.voice.speak.assert_awaited_once()

    async def test_resource_lost_during_interrupt_does_not_emit_ack(self):
        client = self.resource_client()
        original = self.narrator.interrupt
        async def revoke(event):
            await original(event)
            client.granted.clear()
        self.narrator.interrupt = revoke
        result = await self.narrate()
        self.assertEqual(result['error_code'], 'voice_resource_unavailable')
        self.assertFalse(any(m['type'] in {'event', 'reply', 'audio'} for m in self.messages))
        self.voice.speak.assert_not_awaited()
        self.assertEqual(self.messages[-1]['type'], 'bilibili_narration_end')

    async def test_resource_check_failure_is_safe_and_does_not_leak_error_text(self):
        self.narrator.resource_check = Mock(side_effect=RuntimeError('private diagnostic payload'))
        result = await self.narrate()
        self.assertEqual(result['error_code'], 'voice_resource_unavailable')
        self.assertIn('启动中心', result['reason'])
        self.assertNotIn('private', result['reason'])
        self.assertEqual(self.messages, [])
        self.voice.speak.assert_not_awaited()

    async def test_clear_during_narration_setup_rejects_late_synthesis(self):
        original_interrupt, original_push = self.narrator.interrupt, self.narrator.push
        for stage in ('interrupt', 'reply'):
            started, release = asyncio.Event(), asyncio.Event()
            async def interrupt(event):
                await original_interrupt(event)
                if stage == 'interrupt':
                    started.set()
                    await release.wait()
            async def push(message):
                await original_push(message)
                if stage == 'reply' and message['type'] == 'reply':
                    started.set()
                    await release.wait()
            self.narrator.interrupt, self.narrator.push = interrupt, push
            with self.subTest(stage=stage):
                task = self.narrate(request='clear-during-' + stage)
                await asyncio.wait_for(started.wait(), 1)
                self.playback.clear()
                release.set()
                result = await asyncio.wait_for(task, 1)
                self.assertFalse(result['completed'])
                self.assertEqual(result['reason'], 'synthesis_cancelled')
                self.assertIsNone(self.playback.current)
                self.assertFalse(self.pipeline.busy)
                self.voice.speak.assert_not_awaited()
                self.assertFalse(any(message['type'] == 'audio' for message in self.messages))

    async def test_ack_uses_real_turn_and_waits_for_started_then_ended(self):
        task = self.narrate()
        await self.until(lambda: self.playback.current is not None)
        item = self.playback.current
        self.assertFalse(task.done(), 'synthesis and audio dispatch cannot authorize video playback')
        self.assertEqual(item.turn_id, self.interrupts[0].ui_id)
        self.assertEqual(item.event['media_narration_id'], 'request-1')
        user = next(m for m in self.messages if m['type'] == 'event')
        self.assertEqual((user['source'], user['user'], user['text']), ('companion', '你', 'B站点歌 青鸟'))
        reply = next(m for m in self.messages if m['type'] == 'reply')
        self.assertEqual((reply['mode'], reply['media_narration_id']), ('media', 'request-1'))
        self.assertFalse(any(m['type'] == 'bilibili_narration_end' for m in self.messages))
        self.playback.audio_started(item.id)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.playback.audio_finished(item.id)
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result, {'completed': True, 'turn_id': item.turn_id})
        self.assertEqual(self.messages[-1], {'type': 'bilibili_narration_end',
                                           'request_id': 'request-1', 'turn_id': item.turn_id})
        self.hooks['audit_delivery'].assert_not_called()
        self.hooks['conversation'].record_playback.assert_not_called()
        self.hooks['remember_spoken'].assert_not_called()
        self.hooks['agent_tasks'].speech_state.assert_not_called()

    async def test_found_uses_supported_expression_without_duplicate_user_row(self):
        task = self.narrate(stage='found', text='找到啦，接下来一起听吧。')
        await self.until(lambda: self.playback.current is not None)
        item = self.playback.current
        self.assertEqual((item.emotion, item.actions), ('开心', ['弯眼']))
        self.assertEqual(item.event['media_narration_stage'], 'found')
        self.assertFalse(any(m['type'] == 'event' for m in self.messages))
        self.finish()
        self.assertTrue((await task)['completed'])

    async def test_direct_resumes_paused_audio_and_waits_for_actual_delivery(self):
        self.playback.set_paused(True)
        task = self.narrate(stage='direct', text='就放你指定的这个视频，来一起听吧。')
        await self.until(lambda: self.playback.current is not None)
        self.assertFalse(self.playback.paused)
        self.assertFalse(task.done())
        self.assertEqual(self.playback.current.event['media_narration_stage'], 'direct')
        self.assertFalse(any(m['type'] == 'event' for m in self.messages))
        self.finish()
        self.assertTrue((await task)['completed'])

    async def test_direct_retains_resource_preflight_and_cancellation(self):
        client = self.resource_client()
        client.connected = False
        denied = await self.narrate(stage='direct')
        self.assertEqual(denied['error_code'], 'voice_resource_unavailable')
        self.assertEqual(self.messages, [])
        self.voice.speak.assert_not_called()
        client.connected = True
        client.last_sync = time.monotonic()
        task = self.narrate(stage='direct', request='direct-running')
        await self.until(lambda: self.playback.current is not None)
        await self.narrator.cancel('new_chat')
        self.assertFalse((await task)['completed'])
        self.assertIsNone(self.playback.current)

    async def test_local_ack_uses_local_command_caption_and_found_can_resume_independently(self):
        task = self.narrate(media='local')
        await self.until(lambda: self.playback.current is not None)
        user = next(message for message in self.messages if message['type'] == 'event')
        self.assertEqual(user['text'], '点歌 青鸟')
        self.finish()
        self.assertTrue((await task)['completed'])
        self.messages.clear()
        self.playback.set_paused(True)
        task = self.narrate(media='local', stage='found')
        await self.until(lambda: self.playback.current is not None)
        self.assertFalse(self.playback.paused)
        self.assertFalse(any(message['type'] == 'event' for message in self.messages))
        self.assertFalse(task.done())
        self.finish()
        self.assertTrue((await task)['completed'])

    async def test_ended_without_actual_started_receipt_fails_promptly(self):
        task = self.narrate()
        await self.until(lambda: self.playback.current is not None)
        self.playback.audio_finished(self.playback.current.id)
        result = await asyncio.wait_for(task, 1)
        self.assertFalse(result['completed'])
        self.assertEqual(result['reason'], 'missing_playback_start')

    async def test_ack_resumes_microphone_pause_and_still_waits_for_delivery(self):
        self.playback.set_paused(True)
        task = self.narrate()
        await self.until(lambda: self.playback.current is not None)
        self.assertFalse(self.playback.paused)
        self.assertFalse(task.done())
        self.finish()
        self.assertTrue((await task)['completed'])

    async def test_started_while_paused_is_still_a_real_delivery(self):
        task = self.narrate()
        await self.until(lambda: self.playback.current is not None)
        item = self.playback.current
        self.playback.set_paused(True)
        self.playback.audio_started(item.id)
        self.playback.audio_finished(item.id)
        self.assertTrue((await task)['completed'])

    async def test_skip_clear_and_start_timeout_resolve_failure(self):
        for action in ('skip', 'clear', 'start_timeout'):
            task = self.narrate(request=action)
            await self.until(lambda: self.playback.current is not None)
            if action == 'skip':
                self.playback.skip_current()
            elif action == 'clear':
                self.playback.clear()
            else:
                self.clock += 4
                self.playback.tick()
            self.assertFalse((await asyncio.wait_for(task, 1))['completed'], action)
            self.assertIsNone(self.playback.current)

    async def test_missing_or_failed_synthesis_never_queues_fallback(self):
        for error in (False, True):
            self.voice.speak.return_value = None
            self.voice.speak.side_effect = RuntimeError('fixture failure') if error else None
            result = await self.narrate(request=str(error))
            self.assertFalse(result['completed'])
            self.assertFalse(any(m['type'] == 'audio' for m in self.messages))
        self.hooks['recovery'].failure.assert_not_called()
        self.hooks['track_finalizer'].assert_not_called()
        self.hooks['question_delivery'].cancel.assert_not_called()
        self.hooks['agent_tasks'].synthesis_failed.assert_not_called()

    async def test_deadline_stops_unacknowledged_audio(self):
        self.narrator.timeout_sec = .05
        result = await self.narrate()
        self.assertEqual(result['reason'], 'timeout')
        self.assertFalse(result['completed'])
        self.assertIsNone(self.playback.current)
        self.assertTrue(any(m['type'] == 'audio_control' and m['action'] == 'stop' for m in self.messages))
        self.assertEqual(self.messages[-1]['type'], 'bilibili_narration_end')

    async def test_synthesis_timeout_finishes_without_fallback_or_audio(self):
        async def hung_voice(*args):
            await asyncio.Event().wait()
        self.voice.speak.side_effect = hung_voice
        self.pipeline.synthesis_timeout_sec = .03
        result = await asyncio.wait_for(self.narrate(), 1)
        self.assertFalse(result['completed'])
        self.assertEqual(result['reason'], 'no_audio')
        self.assertFalse(any(m['type'] == 'audio' for m in self.messages))
        self.hooks['track_finalizer'].assert_not_called()

    async def test_external_task_cancel_stops_current_audio_and_revokes_marker(self):
        task = self.narrate()
        await self.until(lambda: self.playback.current is not None)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.playback.current)
        self.assertEqual(self.messages[-1]['type'], 'bilibili_narration_end')

    async def test_late_noncooperative_synthesis_after_cancel_cannot_dispatch(self):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def stubborn_voice(*args):
            started.set()
            try:
                await gate.wait()
            except asyncio.CancelledError:
                await gate.wait()
            return 'C:/fixture/late.wav'

        self.voice.speak.side_effect = stubborn_voice
        task = self.narrate()
        try:
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            gate.set()
        await asyncio.wait_for(self.pipeline.queue.join(), 1)
        self.assertFalse(any(m['type'] == 'audio' for m in self.messages))
        self.assertIsNone(self.playback.current)

    async def test_cancel_during_reply_broadcast_prevents_late_enqueue(self):
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def push(message):
            self.messages.append(message)
            if message['type'] == 'reply':
                entered.set()
                await gate.wait()

        self.narrator.push = push
        task = self.narrate()
        await entered.wait()
        await self.narrator.cancel('new_chat')
        result = await asyncio.wait_for(task, 1)
        self.assertEqual(result['reason'], 'new_chat')
        gate.set()
        self.voice.speak.assert_not_awaited()
        self.assertFalse(any(m['type'] == 'audio' for m in self.messages))

    async def test_new_narration_supersedes_old_and_old_receipt_cannot_finish_new(self):
        first = self.narrate(request='old')
        await self.until(lambda: self.playback.current is not None)
        old = self.playback.current
        second = self.narrate(request='new', stage='found')
        self.assertEqual((await first)['reason'], 'superseded')
        await self.until(lambda: self.playback.current is not None and self.playback.current is not old)
        self.narrator.delivered(old, True)
        self.assertFalse(second.done())
        self.finish()
        self.assertTrue((await second)['completed'])

    async def test_main_interrupt_does_not_cancel_itself_but_new_chat_does(self):
        context = dict(manual_mode=asyncio.Event(), ids=self.ids, media_narrator=self.narrator,
                       webui=None, responding=asyncio.Event(), is_backchannel=lambda _: True,
                       services=SimpleNamespace(foreground=Mock()),
                       playback=self.playback, speech_pipeline=self.pipeline, agent_tasks=None,
                       interrupt_generation=asyncio.Event(), conversation=Mock(), metrics=Mock(),
                       arbiter=SimpleNamespace(discard_before=lambda _: []), push_event=self.push)
        register = main_helper('register_event', context)

        async def interrupt(event):
            await register(event, turn_id=event.ui_id)

        self.narrator.interrupt = interrupt
        task = self.narrate()
        await self.until(lambda: self.playback.current is not None)
        self.assertFalse(task.done())
        # A new ordinary voice/chat message cancels the waiting media callback
        # even if it is handled as a short acknowledgement by the chat pipeline.
        context['responding'].set()
        await register(Event('companion', 'chat', '好', forced=True))
        self.assertEqual((await task)['reason'], 'interrupted')
        self.assertIsNone(self.playback.current)

    async def test_main_failure_and_delivery_guards_never_touch_memory_or_standby(self):
        event = Event('system', 'ambient', 'trusted narration')
        event.media_narration_id = 'request'
        event.ui_id = 90
        reply_failed = main_helper('reply_failed', self.hooks)
        synthesized = main_helper('synthesized', self.hooks)
        synthesis_wait = main_helper('synthesis_wait', self.hooks)
        await reply_failed(event, 'tts')
        synthesized(event, 'C:/fixture/test.wav')
        await synthesis_wait(event)
        self.hooks['recovery'].failure.assert_not_called()
        self.hooks['recovery'].success.assert_not_called()
        self.hooks['track_finalizer'].assert_not_called()

    async def test_invalid_payload_and_shutdown_do_not_start_speech(self):
        for payload in ({}, {'stage': []}, None,
                        {'stage': 'ack', 'request_id': 'r', 'text': '', 'query': 'x'}):
            self.assertEqual((await self.narrator.narrate(payload))['reason'], 'invalid_payload')
        await self.narrator.close()
        self.assertEqual((await self.narrate())['reason'], 'shutdown')
        self.voice.speak.assert_not_awaited()
