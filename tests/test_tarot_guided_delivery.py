"""Guided public dialogue through real playback receipts and simulated voice."""
import asyncio
import itertools
import json
import tempfile
import time
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
        self.fail = False

    async def speak(self, text, emotion):
        if self.fail:
            raise OSError('SIMULATED_VOICE_FAILURE')
        return 'C:/simulated-guided-tarot/voice.wav'


class GuidedTarotDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = time.time()
        self.tarot = Tarot(Path(folder.name) / 'tarot.sqlite3', clock=lambda: self.now)
        self.session = self.tarot.create(mode='test', hosting='guided', spread='three')
        entry = self.tarot.test_join(self.session['id'], '灯下来客', '最近画不出来，该从哪里开始？')
        self.reading = self.tarot.select(self.session['id'], entry['entry']['id'])
        self.voice, self.messages, self.finalizers = FakeVoice(), [], []
        self.ids = itertools.count(10)

        async def broadcast(message):
            self.messages.append(message)

        self.playback = SpeechController(PlaybackConfig(max_queue=5), clock=lambda: self.now)
        self.pipeline = SpeechPipeline(self.voice, self.playback, broadcast)
        self.namespace = dict(asyncio=asyncio, Event=Event, tarot=self.tarot,
            services=None, agent_tasks=None, tarot_events={}, playback=self.playback,
            speech_pipeline=self.pipeline, cancelled_questions=set(), failed_turns=set(),
            arbiter=SimpleNamespace(remove_turn=Mock()), track_finalizer=self.finalizers.append,
            cognition=Mock(), question_delivery=Mock(), recovery=Mock())
        current = main_function('companion_event_current', self.namespace)
        self.cancel_invalid = main_function('cancel_invalid_tarot', self.namespace)
        main_function('audit_delivery', self.namespace)
        self.playback.on_state = main_function('speech_state', self.namespace)
        self.playback.on_delivery = main_function('delivered', self.namespace)
        self.pipeline.event_valid = current
        self.playback.item_valid = lambda item: current(item.event)

        async def reply_failed(event, stage):
            self.tarot.mark_result(event, 'failed', error='模拟语音失败，可从原阶段重试。')

        self.namespace['reply_failed'] = reply_failed
        self.pipeline.on_failure = main_function('synthesis_failed', self.namespace)
        self.worker = asyncio.create_task(self.pipeline.run())
        self.addAsyncCleanup(self.close_pipeline)

    async def close_pipeline(self):
        await self.pipeline.close()
        await asyncio.gather(self.worker, *self.finalizers, return_exceptions=True)

    def state(self):
        return self.tarot.status()['reading']

    def claim(self, stage):
        event = self.tarot.next_event()
        self.assertIsNotNone(event)
        self.assertEqual(event.tarot_stage, stage)
        event.ui_id = next(self.ids)
        self.namespace['tarot_events'][event.ui_id] = event
        return event

    async def synthesize(self, event, text, index=0):
        result = await asyncio.wait_for(self.pipeline.enqueue(
            turn_id=event.ui_id, segment_index=index, text=text, emotion='平静', actions=[], event=event), 1)
        return result

    def play_current(self):
        self.assertIsNotNone(self.playback.current)
        item = self.playback.current
        self.playback.audio_started(item.id)
        self.playback.audio_finished(item.id)
        return item

    async def say(self, event, texts):
        for index, text in enumerate(texts):
            result = await self.synthesize(event, text, index)
            self.assertTrue(result.audio_path)
        self.tarot.mark_generated(event, ''.join(texts), segments=len(texts), text_only=False)
        for _ in texts:
            self.play_current()

    async def host_interrupt(self):
        conversation = SimpleNamespace(mark_interrupted=Mock())
        self.namespace.update(manual_mode=asyncio.Event(), interrupt_generation=asyncio.Event(),
            webui=None, conversation=conversation, media_narrator=SimpleNamespace(cancel=AsyncMock()),
            services=SimpleNamespace(foreground=Mock()), metrics=Mock(),
            arbiter=SimpleNamespace(discard_before=Mock(return_value=[]), remove_turn=Mock()),
            is_backchannel=lambda text: False, responding=asyncio.Event())
        register = main_function('register_event', self.namespace)
        await register(Event('voice', 'interrupt', '先停一下。', user='主控'), turn_id=100)
        self.assertTrue(self.namespace['interrupt_generation'].is_set())
        conversation.mark_interrupted.assert_not_called()

    async def confirm_and_draw(self):
        welcome = self.claim('welcome')
        await self.say(welcome, ['你想聊的是最近画不出来，对吗？'])
        self.assertEqual(self.state()['state'], 'awaiting_confirmation')
        self.tarot.test_reply(self.session['id'], '灯下来客', '对')
        self.assertEqual(self.state()['state'], 'selecting')
        self.tarot.test_choose(self.session['id'], '灯下来客', [3, 12, 18])
        return self.claim('reading')

    async def test_complete_public_dialogue_waits_for_each_real_ending(self):
        reading = await self.confirm_and_draw()
        cards = self.state()['cards']
        await self.say(reading, [f'第一张是{cards[0]["name"]}。',
                                 f'接着是{cards[1]["name"]}。',
                                 f'最后是{cards[2]["name"]}。你更在意没有想法，还是不够好？'])
        self.assertEqual(self.state()['state'], 'awaiting_reply')
        self.assertGreater(self.state()['wait_deadline'], self.now)
        self.assertIsNone(self.tarot.next_event())
        self.tarot.test_reply(self.session['id'], '另一位观众', '替他回答')
        self.assertEqual(self.state()['state'], 'awaiting_reply')
        self.now += 1
        self.tarot.test_reply(self.session['id'], '灯下来客', '总觉得自己画得不够好')
        closing = self.claim('closing')
        context = json.loads(closing.tarot_context)
        self.assertIn('不够好', context['response'])
        self.assertEqual(context['cards'], cards)
        self.assertIn('第一张', context['prior_reading'])
        await self.say(closing, ['今晚先留下一张不必给别人看的草稿。我们这一轮先收到这里。'])
        self.assertEqual(self.state()['state'], 'finished')
        self.assertIsNone(self.tarot.next_event())
        public = json.dumps(self.tarot.status(public=True), ensure_ascii=False)
        self.assertNotIn('总觉得自己画得不够好', public)
        self.assertNotIn('第一张', public)
        self.namespace['cognition'].record_delivery.assert_not_called()

    async def test_playback_can_finish_before_generation_and_duplicate_receipts_are_inert(self):
        event = self.claim('welcome')
        await self.synthesize(event, '我听见你的问题了。', 0)
        await self.synthesize(event, '你想聊的是画画，对吗？', 1)
        first = self.play_current()
        self.assertNotEqual(self.state()['state'], 'awaiting_confirmation')
        self.tarot.played(first.event, first.segment_index, True)
        self.assertNotEqual(self.state()['state'], 'awaiting_confirmation')
        second = self.play_current()
        self.assertNotEqual(self.state()['state'], 'awaiting_confirmation')
        self.now += 2
        self.tarot.mark_generated(event, '你想聊的是画画，对吗？', segments=2, text_only=False)
        deadline = self.state()['wait_deadline']
        self.assertEqual(self.state()['state'], 'awaiting_confirmation')
        self.assertGreater(deadline, self.now)
        self.now += 10
        self.tarot.played(second.event, second.segment_index, True)
        self.assertEqual(self.state()['wait_deadline'], deadline)

    async def test_end_without_start_does_not_confirm_delivery(self):
        event = self.claim('welcome')
        await self.synthesize(event, '你的问题是画画，对吗？')
        self.tarot.mark_generated(event, '你的问题是画画，对吗？', segments=1, text_only=False)
        self.playback.audio_finished(self.playback.current.id)
        self.assertIn(self.state()['state'], {'failed', 'interrupted'})
        self.assertFalse(self.state().get('wait_deadline'))
        self.assertIsNone(self.tarot.next_event())

    async def test_actual_tts_failure_stops_flow_and_preserves_stage_for_retry(self):
        event = self.claim('welcome')
        self.voice.fail = True
        result = await self.synthesize(event, '你的问题是画画，对吗？')
        await asyncio.gather(*self.finalizers)
        self.assertIsNone(result.audio_path)
        self.assertEqual(self.state()['state'], 'failed')
        self.assertEqual(self.state()['stage'], 'welcome')
        self.assertIsNone(self.tarot.next_event())
        self.tarot.mark_generated(event, '迟到的生成结束', segments=1, text_only=False)
        self.assertEqual(self.state()['state'], 'failed')

    async def test_text_only_needs_host_confirmation_without_fake_playback(self):
        event = self.claim('welcome')
        self.tarot.mark_generated(event, '你想聊画画，对吗？', segments=0, text_only=True)
        self.assertEqual(self.state()['state'], 'awaiting_host')
        self.assertFalse(self.state().get('wait_deadline'))
        self.assertIsNone(self.tarot.next_event())
        self.assertFalse(self.messages)
        self.now += 300
        self.assertIsNone(self.tarot.next_event())
        self.tarot.confirm_delivered(self.session['id'], self.reading['id'])
        self.assertEqual(self.state()['state'], 'awaiting_confirmation')
        self.assertGreater(self.state()['wait_deadline'], self.now)

    async def test_pause_revokes_old_started_and_ended_receipts(self):
        event = await self.confirm_and_draw()
        card = self.state()['cards'][0]
        await self.synthesize(event, f'第一张是{card["name"]}。')
        self.tarot.mark_generated(event, f'第一张是{card["name"]}。', segments=1, text_only=False)
        old = self.playback.current
        self.tarot.pause(self.session['id'], self.reading['id'])
        self.playback.audio_started(old.id)
        self.playback.audio_finished(old.id)
        await self.cancel_invalid()
        state = self.state()
        self.assertTrue(state['paused'])
        self.assertEqual(state['revealed_count'], 0)
        self.assertFalse(state.get('wait_deadline'))
        self.assertIsNone(self.tarot.next_event())
        self.assertFalse(self.tarot.valid_event(event))

    async def test_host_interrupt_freezes_an_active_reply_window(self):
        event = self.claim('welcome')
        await self.say(event, ['你想聊的是画画，对吗？'])
        self.assertEqual(self.state()['state'], 'awaiting_confirmation')
        self.now += 10
        await self.host_interrupt()
        self.assertTrue(self.state()['paused'])
        self.assertFalse(self.state().get('wait_deadline'))
        self.now += 300
        self.assertIsNone(self.tarot.next_event())
        self.tarot.resume(self.session['id'], self.reading['id'])
        self.assertEqual(self.state()['state'], 'awaiting_confirmation')
        self.assertAlmostEqual(self.state()['wait_deadline'] - self.now, 50)

    async def test_host_interrupt_cancels_active_audio_without_generic_chat_memory(self):
        event = self.claim('welcome')
        await self.synthesize(event, '我听见你的问题了。', 0)
        await self.synthesize(event, '你想聊的是画画，对吗？', 1)
        self.tarot.mark_generated(event, '我听见你的问题了。你想聊的是画画，对吗？', segments=2, text_only=False)
        old = self.playback.current
        self.playback.audio_started(old.id)
        await self.host_interrupt()
        self.assertTrue(self.state()['paused'])
        self.assertIsNone(self.playback.current)
        self.assertFalse(self.playback.queue)
        self.playback.audio_finished(old.id)
        self.assertTrue(self.state()['paused'])
        self.assertIsNone(self.tarot.next_event())

    async def test_card_faces_advance_only_with_spoken_card_segments(self):
        event = await self.confirm_and_draw()
        cards = self.state()['cards']
        self.assertEqual(self.state()['revealed_count'], 0)
        for index, card in enumerate(cards):
            await self.synthesize(event, f'这张牌是{card["name"]}。', index)
        self.tarot.mark_generated(event, '三张牌的完整解读', segments=3, text_only=False)
        self.assertEqual(self.state()['revealed_count'], 0)
        for index in range(3):
            self.playback.audio_started(self.playback.current.id)
            self.assertEqual(self.state()['revealed_count'], index + 1)
            self.playback.audio_finished(self.playback.current.id)
        self.assertEqual(self.state()['state'], 'awaiting_reply')


if __name__ == '__main__':
    unittest.main()
