from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.config import LLMConfig, PersonaConfig, PlaybackConfig
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline


class _Voice:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def speak(self, text: str, emotion: str) -> str:
        self.calls.append(text)
        await asyncio.sleep(0)
        return f"C:/fake/{len(self.calls)}.wav"

    async def close(self) -> None:
        self.closed = True


class _SegmentVoice(_Voice):
    def __init__(self) -> None:
        super().__init__()
        self.segments = []

    async def speak(self, text: str, emotion: str) -> str:
        raise AssertionError('分句后端必须收到回合上下文')

    async def speak_segment(self, text: str, emotion: str = '平静', *, turn_id, segment_index) -> str:
        self.segments.append((text, emotion, turn_id, segment_index))
        await asyncio.sleep(0)
        return f'C:/fake/{len(self.segments)}.wav'


class SpeechPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 1)

    def enqueue_segments(self, pipeline, turn, count, *, event=None):
        event = event or Event('voice', 'chat', '详细说明', user='你')
        return [pipeline.enqueue(turn_id=turn, segment_index=i, text=f'第{i}段',
                                 emotion='平静', actions=[], event=event) for i in range(count)]

    async def test_optional_segment_backend_receives_turn_and_sentence_context(self):
        voice, messages = _SegmentVoice(), []
        playback = SpeechController(PlaybackConfig(max_queue=3))

        async def broadcast(message):
            messages.append(message)

        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        event = Event('voice', 'chat', '今天怎么样')
        segments = [('今天有一点累。', '平静', 9, 0),
                    ('不过终于完成了！', '开心', 9, 1),
                    ('真的呀？', '惊讶', 10, 0)]
        try:
            futures = [pipeline.enqueue(turn_id=turn, segment_index=index, text=text,
                                        emotion=emotion, actions=[], event=event)
                       for text, emotion, turn, index in segments]
            results = await asyncio.wait_for(asyncio.gather(*futures), 1)
            self.assertEqual(voice.segments, segments)
            self.assertTrue(all(result.audio_path and not result.cancelled for result in results))
            items = [playback.current, *playback.queue]
            self.assertEqual([(item.text, item.emotion, item.turn_id, item.segment_index)
                              for item in items], segments)
            self.assertFalse(any(message['type'] == 'tts_error' for message in messages))
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_legacy_backend_and_noncallable_segment_attribute_keep_speak_contract(self):
        for with_noncallable_attribute in (False, True):
            with self.subTest(noncallable_attribute=with_noncallable_attribute):
                voice, calls = _Voice(), []
                if with_noncallable_attribute:
                    voice.speak_segment = None

                async def speak(text, emotion):
                    calls.append((text, emotion))
                    return 'C:/fake/legacy.wav'

                voice.speak = speak

                async def broadcast(message):
                    pass

                pipeline = SpeechPipeline(voice, SpeechController(PlaybackConfig()), broadcast)
                worker = asyncio.create_task(pipeline.run())
                try:
                    result = await asyncio.wait_for(pipeline.enqueue(
                        turn_id=23, segment_index=2, text='你好呀。', emotion='开心',
                        actions=[], event=Event('voice', 'chat', '你好')), 1)
                    self.assertEqual(calls, [('你好呀。', '开心')])
                    self.assertEqual(result.audio_path, 'C:/fake/legacy.wav')
                    self.assertFalse(result.cancelled)
                finally:
                    await pipeline.close()
                    await asyncio.gather(worker, return_exceptions=True)

    async def test_cancelled_segment_backend_cannot_publish_late_audio(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        class SlowSegmentVoice(_SegmentVoice):
            async def speak_segment(self, text, emotion='平静', *, turn_id, segment_index):
                if turn_id != 1:
                    return await super().speak_segment(text, emotion, turn_id=turn_id,
                                                       segment_index=segment_index)
                self.segments.append((text, emotion, turn_id, segment_index))
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    # Even a backend that returns during cancellation must not
                    # resurrect an interrupted reply in the player.
                    return 'C:/fake/late.wav'

        voice, messages = SlowSegmentVoice(), []
        playback = SpeechController(PlaybackConfig())

        async def broadcast(message):
            messages.append(message)

        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            old = self.enqueue_segments(pipeline, 1, 2)
            await asyncio.wait_for(started.wait(), 1)
            await pipeline.cancel_turn(1)
            results = await asyncio.wait_for(asyncio.gather(*old), 1)
            self.assertTrue(cancelled.is_set())
            self.assertTrue(all(result.cancelled and not result.audio_path for result in results))
            self.assertIsNone(playback.current)
            self.assertFalse(playback.queue)
            self.assertFalse(any(message['type'] == 'audio' for message in messages))
            new = self.enqueue_segments(pipeline, 2, 1)
            result = (await asyncio.wait_for(asyncio.gather(*new), 1))[0]
            self.assertFalse(result.cancelled)
            self.assertEqual(playback.current.turn_id, 2)
            self.assertEqual([(turn, index) for _, _, turn, index in voice.segments], [(1, 0), (2, 0)])
            self.assertFalse(any('late.wav' in message.get('url', '') for message in messages))
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_five_segment_reply_survives_long_pause_and_slow_playback(self):
        now = [100.0]
        playback = SpeechController(PlaybackConfig(max_queue=3, max_age_sec=5), clock=lambda: now[0])
        playback.set_paused(True)
        voice, messages = _Voice(), []
        async def broadcast(message):
            messages.append(message)
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            event = Event('voice', 'chat', '请详细解释怎么安排一天', ts=100)
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'TEST_QUEUE_KEY': 'fixture'}):
                persona = Persona(PersonaConfig(), LLMConfig(api_key_env='TEST_QUEUE_KEY', first_clause_chars=24),
                                  Memory(str(Path(folder)/'log'), str(Path(folder)/'viewers')))
                async def stream(*args):
                    # Separate deltas exercise a real five-item transport queue;
                    # already-arrived short sentences may share one TTS request.
                    for delta in ('[平静]第一步先把今天要做的事情和明天要做的事情全部记下来，',
                                  '再按顺序慢慢整理。', '第二步把最需要处理的事情先做完。',
                                  '第三步给自己留出休息时间。', '第四步睡前检查一次完成的情况。'):
                        yield delta
                persona._stream_llm = stream
                segments = [s async for s in persona.stream_reply(event)]
            self.assertEqual([s.index for s in segments], list(range(5)))
            futures = [pipeline.enqueue(turn_id=1, segment_index=s.index, text=s.text,
                                        emotion=s.emotion, actions=s.actions, event=event) for s in segments]
            await self.wait_until(lambda: len(voice.calls) == 4 and futures[3].done())
            await asyncio.sleep(.06)
            self.assertFalse(futures[4].done())
            self.assertEqual(len(voice.calls), 4, 'full playback queue must stop synthesizing')
            self.assertEqual([s.segment_index for s in [playback.current, *playback.queue]], [0, 1, 2, 3])
            now[0] = 1000
            self.assertEqual(playback.tick(), [])
            playback.set_paused(False)
            seen = []
            for index in range(5):
                await self.wait_until(lambda: playback.current is not None)
                item = playback.current
                seen.append(item.segment_index)
                playback.audio_started(item.id)
                now[0] += 10  # Longer than max_age; the admitted reply stays whole.
                playback.audio_finished(item.id)
                self.assertLessEqual(len(playback.queue), 3)
            results = await asyncio.wait_for(asyncio.gather(*futures), 1)
            self.assertEqual(seen, list(range(5)))
            self.assertFalse(any(result.cancelled for result in results))
            self.assertEqual(playback.dropped, 0)
            await asyncio.wait_for(pipeline.queue.join(), 1)
            self.assertFalse(pipeline.busy)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await pipeline.close()

    async def test_interrupt_releases_capacity_wait_and_rejects_late_tail(self):
        playback = SpeechController(PlaybackConfig(max_queue=0))
        playback.set_paused(True)
        voice = _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            old = self.enqueue_segments(pipeline, 1, 3)
            await self.wait_until(lambda: old[0].done() and pipeline._active_job.segment_index == 1)
            await pipeline.interrupt_before(2)
            late = self.enqueue_segments(pipeline, 1, 1)
            new = self.enqueue_segments(pipeline, 2, 1)
            results = await asyncio.wait_for(asyncio.gather(*old, *late, *new), 1)
            self.assertEqual([r.cancelled for r in results], [False, True, True, True, False])
            self.assertEqual(playback.current.turn_id, 2)
            self.assertEqual(len(voice.calls), 2)
            await asyncio.wait_for(pipeline.queue.join(), 1)
            self.assertFalse(pipeline.busy)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_urgent_turn_can_preempt_a_paused_full_queue(self):
        playback = SpeechController(PlaybackConfig(max_queue=1))
        playback.set_paused(True)
        voice = _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            old = self.enqueue_segments(pipeline, 1, 4)
            await self.wait_until(lambda: old[1].done() and pipeline._active_job.segment_index == 2)
            urgent = self.enqueue_segments(pipeline, 2, 1, event=Event('danmaku', 'gift', '礼物', priority=100))
            results = await asyncio.wait_for(asyncio.gather(*old, *urgent), 1)
            self.assertEqual([r.cancelled for r in results], [False, False, True, True, False])
            self.assertEqual(playback.current.turn_id, 2)
            self.assertFalse(playback.queue)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_clear_discards_pending_tail_instead_of_refilling_playback(self):
        playback = SpeechController(PlaybackConfig(max_queue=0))
        playback.set_paused(True)
        voice = _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            futures = self.enqueue_segments(pipeline, 1, 3)
            await self.wait_until(lambda: futures[0].done())
            playback.clear()
            results = await asyncio.wait_for(asyncio.gather(*futures), 1)
            self.assertEqual([r.cancelled for r in results], [False, True, True])
            self.assertEqual(len(voice.calls), 1)
            self.assertIsNone(playback.current)
            self.assertFalse(playback.queue)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_clear_rejects_late_stream_segments_even_after_pipeline_drained(self):
        playback, voice, messages = SpeechController(PlaybackConfig()), _Voice(), []
        async def broadcast(message):
            messages.append(message)
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            event = Event('voice', 'chat', '详细说明', user='你')
            first = await pipeline.enqueue(turn_id=1, segment_index=0, text='已经生成的首句',
                emotion='平静', actions=[], event=event)
            self.assertFalse(first.cancelled)
            await pipeline.queue.join()
            self.assertFalse(pipeline.busy)
            playback.clear()
            late = await pipeline.enqueue(turn_id=1, segment_index=1, text='清空后才生成的尾句',
                emotion='平静', actions=[], event=event)
            self.assertTrue(late.cancelled)
            self.assertIsNone(playback.current)
            self.assertEqual(voice.calls, ['已经生成的首句'])
            # A genuinely new turn remains available after the clear.
            fresh_event = Event('voice', 'chat', '新问题', user='你')
            fresh = await pipeline.enqueue(turn_id=2, segment_index=0, text='新回答',
                emotion='平静', actions=[], event=fresh_event)
            self.assertFalse(fresh.cancelled)
            self.assertEqual(playback.current.turn_id, 2)
            self.assertEqual([m['text'] for m in messages if m['type'] == 'audio'],
                             ['已经生成的首句', '新回答'])
        finally:
            await pipeline.close()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_clear_before_first_sentence_invalidates_bound_generation(self):
        playback, voice = SpeechController(PlaybackConfig()), _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        try:
            event = Event('voice', 'chat', '模型还在思考', user='你')
            pipeline.begin_turn(event, 1)
            playback.clear()
            late = await pipeline.enqueue(turn_id=1, segment_index=0, text='迟到的第一句',
                emotion='平静', actions=[], event=event)
            self.assertTrue(late.cancelled)
            self.assertFalse(voice.calls)
            self.assertIsNone(playback.current)
        finally:
            await pipeline.close()

    async def test_invalidated_receipt_stops_waiting_without_synthesis(self):
        playback = SpeechController(PlaybackConfig(max_queue=0))
        playback.set_paused(True)
        voice = _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        valid = True
        pipeline.event_valid = lambda event: valid
        worker = asyncio.create_task(pipeline.run())
        try:
            futures = self.enqueue_segments(pipeline, 1, 3)
            await self.wait_until(lambda: futures[0].done())
            valid = False
            results = await asyncio.wait_for(asyncio.gather(*futures), 1)
            self.assertEqual([r.cancelled for r in results], [False, True, True])
            self.assertEqual(len(voice.calls), 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_shutdown_settles_active_and_queued_futures(self):
        playback = SpeechController(PlaybackConfig(max_queue=0))
        playback.set_paused(True)
        voice = _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        futures = self.enqueue_segments(pipeline, 1, 5)
        await self.wait_until(lambda: futures[0].done())
        await asyncio.wait_for(pipeline.close(), 1)
        self.assertTrue(worker.done())
        self.assertTrue(all(future.done() for future in futures))
        self.assertFalse(pipeline.busy)
        self.assertIsNone(pipeline._active_synthesis)
        self.assertTrue(voice.closed)
        await asyncio.wait_for(pipeline.queue.join(), 1)

    async def test_new_stale_turn_is_rejected_before_spending_tts(self):
        playback = SpeechController(PlaybackConfig(max_age_sec=5), clock=lambda: 100)
        voice = _Voice()
        async def broadcast(message):
            pass
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        try:
            stale = self.enqueue_segments(pipeline, 1, 1, event=Event('voice', 'chat', '旧问题', ts=0))
            result = (await asyncio.wait_for(asyncio.gather(*stale), 1))[0]
            self.assertTrue(result.cancelled)
            self.assertFalse(voice.calls)
            self.assertIsNone(playback.current)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_synthesizes_segments_and_cancels_whole_turn(self) -> None:
        voice = _Voice()
        messages: list[dict] = []

        async def broadcast(message: dict) -> None:
            messages.append(message)

        playback = SpeechController(PlaybackConfig(max_queue=3))
        pipeline = SpeechPipeline(voice, playback, broadcast)
        worker = asyncio.create_task(pipeline.run())
        event = Event("danmaku", "chat", "测试", user="观众", ts=time.time())
        try:
            first = pipeline.enqueue(
                turn_id=9, segment_index=0, text="第一句。", emotion="开心",
                actions=["挥手"], event=event,
            )
            second = pipeline.enqueue(
                turn_id=9, segment_index=1, text="第二句。", emotion="开心",
                actions=[], event=event,
            )
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

            self.assertEqual(voice.calls, ["第一句。", "第二句。"])
            self.assertEqual(playback.current.turn_id, 9)
            self.assertEqual([item.turn_id for item in playback.queue], [9])
            self.assertTrue(any(message.get("type") == "audio" for message in messages))

            await pipeline.cancel_turn(9)
            self.assertIsNone(playback.current)
            self.assertEqual(playback.queue, [])
            self.assertTrue(any(message.get("type") == "audio_control" for message in messages))
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await pipeline.close()
        self.assertTrue(voice.closed)


if __name__ == "__main__":
    unittest.main()
