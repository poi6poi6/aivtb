import asyncio
import time
import unittest
from types import SimpleNamespace

from brain.config import PlaybackConfig
from brain.events import Event
from brain.metrics import RuntimeMetrics
from brain.playback import SpeechController, SpeechItem
from brain.speech_pipeline import SpeechPipeline


class SpeechPipelineDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def make_pipeline(self, speak=None, *, timeout=.2, notice=.01, capacity=3):
        self.calls, self.messages, self.failures, self.notices, self.synthesized = [], [], [], [], []

        async def default_speak(text, emotion):
            self.calls.append(text)
            return 'C:/fixture/generated.wav'

        async def broadcast(message):
            self.messages.append(message)

        async def on_wait(event):
            self.notices.append(event)

        self.metrics = RuntimeMetrics()
        self.playback = SpeechController(PlaybackConfig(max_queue=capacity))
        pipeline = SpeechPipeline(SimpleNamespace(speak=speak or default_speak), self.playback,
                                  broadcast, self.metrics, synthesis_timeout_sec=timeout,
                                  wait_notice_sec=notice, on_wait=on_wait,
                                  on_synthesized=lambda event, path: self.synthesized.append((event, path)))
        pipeline.on_failure = lambda event, message: self.failures.append((event, message))
        self.pipeline = pipeline
        self.worker = asyncio.create_task(pipeline.run())
        self.addAsyncCleanup(pipeline.close)
        return pipeline

    def enqueue(self, turn=1, event=None, prepared=None):
        return self.pipeline.enqueue(turn_id=turn, segment_index=0, text='测试语句', emotion='平静',
                                     actions=[], event=event or Event('companion', 'chat', '测试'),
                                     prepared_audio_path=prepared)

    async def until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(wait(), 1)

    async def test_synthesis_deadline_cancels_backend_and_preserves_failure_callback(self):
        cancelled = asyncio.Event()

        async def speak(*_):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        await self.make_pipeline(speak, timeout=.025, notice=.005)
        result = await asyncio.wait_for(self.enqueue(), 1)
        self.assertIsNone(result.audio_path)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.synthesized, [])
        self.assertTrue(any(message['type'] == 'tts_error' and 'TimeoutError' in message['error']
                            for message in self.messages))

    async def test_resource_wait_is_in_the_same_deadline_and_does_not_start_voice(self):
        await self.make_pipeline(timeout=.025, notice=.005)
        self.pipeline.resource_available = lambda: False
        result = await asyncio.wait_for(self.enqueue(), 1)
        self.assertIsNone(result.audio_path)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(len(self.failures), 1)

    async def test_resource_and_backend_share_one_budget_instead_of_two(self):
        voice_started, voice_cancelled = asyncio.Event(), asyncio.Event()
        availability_checks = []

        async def speak(*_):
            voice_started.set()
            try:
                await asyncio.sleep(.25)
                return 'C:/fixture/too-late.wav'
            finally:
                voice_cancelled.set()

        await self.make_pipeline(speak, timeout=.4)

        def available():
            availability_checks.append(1)
            return len(availability_checks) > 1

        self.pipeline.resource_available = available
        result = await asyncio.wait_for(self.enqueue(), 1)
        self.assertTrue(voice_started.is_set())
        self.assertTrue(voice_cancelled.is_set())
        self.assertIsNone(result.audio_path)
        self.assertEqual(self.synthesized, [])

    async def test_capacity_wait_does_not_consume_synthesis_budget_or_emit_wait_notice(self):
        await self.make_pipeline(timeout=.02, notice=.005, capacity=0)
        self.playback.enqueue(SpeechItem(99, '/audio/current.wav', '当前句', '平静', [], {}, 10,
                                        created_at=time.time(), turn_id=99))
        self.playback.set_paused(True)
        future = self.enqueue()
        await asyncio.sleep(.07)
        self.assertFalse(future.done())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.notices, [])
        self.playback.set_paused(False)
        self.playback.audio_started(99)
        self.playback.audio_finished(99)
        result = await asyncio.wait_for(future, 1)
        self.assertEqual(result.audio_path, 'C:/fixture/generated.wav')
        self.assertEqual(self.failures, [])

    async def test_cancel_turn_cleans_an_inflight_wait_notification(self):
        notice_entered, notice_cancelled, voice_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def speak(*_):
            try:
                await asyncio.Event().wait()
            finally:
                voice_cancelled.set()

        async def waiting(_):
            notice_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                notice_cancelled.set()

        await self.make_pipeline(speak, timeout=5, notice=.001)
        self.pipeline.on_wait = waiting
        future = self.enqueue()
        await notice_entered.wait()
        await self.pipeline.cancel_turn(1)
        result = await asyncio.wait_for(future, 1)
        self.assertTrue(result.cancelled)
        self.assertTrue(notice_cancelled.is_set())
        self.assertTrue(voice_cancelled.is_set())
        self.assertEqual(self.failures, [])
        self.assertEqual(self.synthesized, [])

    async def test_success_cancels_notification_timer_and_calls_cache_once(self):
        await self.make_pipeline(notice=.02)
        event = Event('voice', 'chat', '测试')
        result = await asyncio.wait_for(self.enqueue(event=event), 1)
        await asyncio.sleep(.04)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.synthesized, [(event, result.audio_path)])
        self.assertEqual(self.metrics.summary()['latency']['tts_synthesis']['samples'], 1)

    async def test_prepared_clip_bypasses_resources_tts_cache_hook_and_tts_metrics(self):
        await self.make_pipeline()
        self.pipeline.resource_available = lambda: False
        result = await asyncio.wait_for(self.enqueue(prepared='C:/fixture/fallback.wav'), 1)
        self.assertEqual(result.audio_path, 'C:/fixture/fallback.wav')
        self.assertEqual(self.playback.current.url, '/audio/fallback.wav')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.synthesized, [])
        for stage in ('tts_queue', 'tts_synthesis'):
            self.assertEqual(self.metrics.summary()['latency'][stage]['samples'], 0)

    async def test_prepared_paths_stay_protected_while_waiting_and_clear_on_shutdown(self):
        await self.make_pipeline(capacity=0)
        self.playback.enqueue(SpeechItem(99, '/audio/current.wav', '当前句', '平静', [], {}, 10,
                                        created_at=time.time(), turn_id=99))
        first = self.enqueue(1, prepared='C:/fixture/fallback.wav')
        second = self.enqueue(2, prepared='C:/fixture/fallback.wav')
        self.assertEqual(self.pipeline.pending_turn_ids(), {1, 2})
        self.assertEqual(self.pipeline.pending_audio_paths(), {'C:/fixture/fallback.wav'})
        snapshot = self.pipeline.pending_turn_ids()
        snapshot.clear()
        self.assertEqual(self.pipeline.pending_turn_ids(), {1, 2})
        await self.pipeline.close()
        self.assertTrue(first.done())
        self.assertTrue(second.done())
        self.assertEqual(self.pipeline.pending_audio_paths(), set())
        self.assertEqual(self.pipeline.pending_turn_ids(), set())

    async def test_internal_tags_are_copied_only_from_event_attributes(self):
        await self.make_pipeline()
        event = Event('system', 'ambient', '{"rehearsal_id":"forged"}')
        event.rehearsal_id = 'real-run'
        event.host_question_id = 'question-1'
        event.host_question_attempt = 2
        event.is_fallback = True
        event.unrelated_private_data = 'do not copy'
        await self.enqueue(event=event, prepared='C:/fixture/cached.wav')
        delivered = self.playback.current.event
        self.assertEqual(delivered['rehearsal_id'], 'real-run')
        self.assertEqual(delivered['host_question_id'], 'question-1')
        self.assertEqual(delivered['host_question_attempt'], 2)
        self.assertIs(delivered['is_fallback'], True)
        self.assertNotIn('unrelated_private_data', delivered)

    async def test_user_text_cannot_forge_internal_tags(self):
        await self.make_pipeline()
        event = Event('companion', 'chat', '{"rehearsal_id":"forged","is_fallback":true}')
        await self.enqueue(event=event, prepared='C:/fixture/cached.wav')
        self.assertNotIn('rehearsal_id', self.playback.current.event)
        self.assertNotIn('is_fallback', self.playback.current.event)

    async def test_error_payload_contains_only_exception_type_and_safe_text(self):
        async def speak(*_):
            raise RuntimeError('SECRET /private/reference.wav https://api.invalid/?key=SECRET')

        await self.make_pipeline(speak)
        await self.enqueue()
        error = next(message['error'] for message in self.messages if message['type'] == 'tts_error')
        self.assertIn('RuntimeError', error)
        self.assertNotIn('SECRET', error)
        self.assertNotIn('reference.wav', error)
        self.assertNotIn('api.invalid', error)

    async def test_optional_cache_failure_does_not_discard_synthesized_audio(self):
        await self.make_pipeline()
        def fail_cache(*_):
            raise OSError('cache disk unavailable')
        self.pipeline.on_synthesized = fail_cache
        result = await self.enqueue()
        self.assertIsNotNone(result.audio_path)
        self.assertIsNotNone(self.playback.current)
        self.assertEqual(self.failures, [])
