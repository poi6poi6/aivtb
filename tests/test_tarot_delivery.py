"""Real Tarot → SpeechPipeline → player boundaries with simulated TTS only."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from brain.config import PlaybackConfig
from brain.events import Event
from brain.playback import SpeechController
from brain.speech_pipeline import SpeechPipeline
from brain.tarot import Tarot
from tests.test_runtime_integration import main_function


class FakeVoice:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def speak(self, text, emotion):
        self.calls.append(text)
        self.started.set()
        await self.release.wait()
        return 'C:/simulated-tarot/voice.wav'


class TarotDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tarot = Tarot(Path(temporary.name) / 'tarot.sqlite3')
        self.session = self.tarot.create()
        receipt = self.tarot.test_join(self.session['id'], '测试观众', '整理书桌时可以从哪里开始？')
        self.reading = self.tarot.select(self.session['id'], receipt['entry']['id'])
        self.reading = self.tarot.draw(self.session['id'], self.reading['id'], [7])
        self.voice = FakeVoice()
        self.messages = []

        async def broadcast(message):
            self.messages.append(message)

        self.playback = SpeechController(PlaybackConfig(max_queue=3))
        self.pipeline = SpeechPipeline(self.voice, self.playback, broadcast)
        self.namespace = dict(tarot=self.tarot, services=None, tarot_events={},
                              arbiter=SimpleNamespace(remove_turn=Mock()),
                              cancelled_questions=set(), speech_pipeline=self.pipeline)
        current = main_function('companion_event_current', self.namespace)
        self.cancel_invalid = main_function('cancel_invalid_tarot', self.namespace)
        self.pipeline.event_valid = current
        self.playback.item_valid = lambda item: current(item.event)
        self.worker = asyncio.create_task(self.pipeline.run())
        self.addAsyncCleanup(self.stop_pipeline)

    async def stop_pipeline(self):
        await self.pipeline.close()
        await asyncio.gather(self.worker, return_exceptions=True)

    def claim(self, turn_id):
        event = self.tarot.interpretation_event(self.session['id'], self.reading['id'])
        event.ui_id = turn_id
        self.namespace['tarot_events'][turn_id] = event
        return event

    def enqueue(self, event, segment=0):
        return self.pipeline.enqueue(turn_id=event.ui_id, segment_index=segment,
            text=f'第 {event.ui_id} 轮第 {segment} 句牌面解读。', emotion='平静', actions=[], event=event)

    async def synthesized(self, event, segment=0):
        result = await asyncio.wait_for(self.enqueue(event, segment), 1)
        self.assertFalse(result.cancelled)
        self.assertTrue(result.audio_path)
        return result

    async def host_interrupt(self):
        conversation = SimpleNamespace(mark_interrupted=Mock())
        namespace = dict(self.namespace, Event=Event, manual_mode=asyncio.Event(),
            interrupt_generation=asyncio.Event(), playback=self.playback, webui=None,
            conversation=conversation, media_narrator=SimpleNamespace(cancel=AsyncMock()),
            services=SimpleNamespace(foreground=Mock()), metrics=Mock(),
            arbiter=SimpleNamespace(discard_before=Mock(return_value=[])))
        register = main_function('register_event', namespace)
        await register(Event('voice', 'interrupt', '先停一下。', user='主控'), turn_id=20)
        self.assertTrue(namespace['interrupt_generation'].is_set())
        self.assertIsNone(self.playback.current)
        self.assertFalse(self.playback.queue)
        namespace['services'].foreground.assert_called_once_with('voice')
        return conversation

    async def test_host_interrupt_does_not_put_tarot_into_recent_conversation(self):
        event = self.claim(10)
        await self.synthesized(event)
        await self.synthesized(event, 1)
        conversation = await self.host_interrupt()
        conversation.mark_interrupted.assert_not_called()

    async def test_host_interrupt_still_remembers_unfinished_ordinary_chat(self):
        event = Event('voice', 'chat', '帮我继续整理刚才的普通话题。', user='主控', viewer_key='local-owner')
        event.ui_id = 10
        await self.synthesized(event)
        conversation = await self.host_interrupt()
        conversation.mark_interrupted.assert_called_once()
        remembered = conversation.mark_interrupted.call_args.args[0]
        for field in ('source', 'kind', 'text', 'user', 'viewer_key', 'ts', 'ui_id'):
            self.assertEqual(getattr(remembered, field), getattr(event, field))

    async def test_ids_reach_audio_and_finish_cancels_current_queued_and_late_segments(self):
        event = self.claim(10)
        await self.synthesized(event)
        await self.synthesized(event, 1)
        items = [self.playback.current, *self.playback.queue]
        self.assertEqual(len(items), 2)
        for item in items:
            payload = item.message()['event']
            for field in ('tarot_session_id', 'tarot_reading_id', 'tarot_attempt_id'):
                self.assertEqual(payload[field], getattr(event, field))
            self.assertNotIn('tarot_context', payload)
            self.assertNotIn('deck', payload)
        audio = next(message for message in self.messages if message['type'] == 'audio')
        self.assertEqual(audio['event']['tarot_attempt_id'], event.tarot_attempt_id)
        current_id = self.playback.current.id
        self.playback.audio_started(current_id)
        self.tarot.finish(self.session['id'], self.reading['id'])
        before = len(self.messages)
        await self.cancel_invalid()
        self.assertIsNone(self.playback.current)
        self.assertFalse(self.playback.queue)
        self.assertTrue(any(message.get('action') == 'stop' and message.get('id') == current_id
                            for message in self.messages[before:]))
        self.assertFalse(any(message['type'] == 'audio' for message in self.messages[before:]))
        late = await asyncio.wait_for(self.enqueue(event, 2), 1)
        self.assertTrue(late.cancelled)
        self.assertEqual(len(self.voice.calls), 2)
        self.assertFalse(self.tarot.valid_event(audio['event']))

    async def test_retry_rejects_audio_that_finishes_synthesis_for_old_attempt(self):
        old = self.claim(10)
        self.voice.release.clear()
        pending = self.enqueue(old)
        await asyncio.wait_for(self.voice.started.wait(), 1)
        self.tarot.mark_result(old, 'failed', error='模拟失败，可原牌重试。')
        new = self.claim(11)
        self.assertNotEqual(old.tarot_attempt_id, new.tarot_attempt_id)
        self.voice.release.set()
        result = await asyncio.wait_for(pending, 1)
        self.assertTrue(result.cancelled)
        self.assertIsNone(result.audio_path)
        self.assertFalse(any(message['type'] == 'audio' for message in self.messages))
        await self.synthesized(new)
        audios = [message for message in self.messages if message['type'] == 'audio']
        self.assertEqual(len(audios), 1)
        self.assertEqual(audios[0]['event']['tarot_attempt_id'], new.tarot_attempt_id)
        self.assertEqual(self.tarot.status()['reading']['cards'], self.reading['cards'])

    async def test_retry_invalidates_already_prepared_audio_at_player_dispatch(self):
        old = self.claim(10)
        await self.synthesized(old)
        await self.synthesized(old, 1)
        self.tarot.mark_result(old, 'interrupted', error='模拟中断，可原牌重试。')
        new = self.claim(11)
        # A reconnect is a separate dispatch boundary, even before the host's
        # cancellation callback has cleared the old synthesized queue.
        messages = self.playback.redispatch_current()
        self.assertFalse(any(message['type'] == 'audio' for message in messages))
        self.assertIsNone(self.playback.current)
        self.assertFalse(self.playback.queue)
        await self.synthesized(new)
        self.assertEqual(self.playback.current.event['tarot_attempt_id'], new.tarot_attempt_id)
        self.assertTrue(self.tarot.valid_event(self.playback.current.event))
        late = await asyncio.wait_for(self.enqueue(old, 2), 1)
        self.assertTrue(late.cancelled)
        self.assertEqual(len(self.voice.calls), 3)


if __name__ == '__main__':
    unittest.main()
