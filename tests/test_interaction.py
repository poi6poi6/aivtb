from __future__ import annotations

import unittest

from brain.config import PlaybackConfig
from brain.conversation import ConversationState, VoiceRevisionWindow
from brain.events import Event
from brain.playback import SpeechController, SpeechItem


class PauseTests(unittest.TestCase):
    def setup_controller(self):
        now = [100.0]
        controller = SpeechController(PlaybackConfig(max_age_sec=20, max_play_sec=30), clock=lambda: now[0])
        first = SpeechItem(1, '/audio/1.wav', '第一段', '平静', [], {}, 10, created_at=100, turn_id=1)
        second = SpeechItem(2, '/audio/2.wav', '第二段', '平静', [], {}, 10, created_at=100, turn_id=1)
        controller.enqueue(first)
        controller.enqueue(second)
        return controller, now, first, second

    def test_long_pause_preserves_current_queue_progress_and_watchdog_budget(self):
        controller, now, first, second = self.setup_controller()
        controller.audio_started(1)
        now[0] = 105
        controller.audio_progress(1, 5)
        messages = controller.set_paused(True)
        self.assertEqual(messages[0]['action'], 'pause')
        now[0] = 405
        self.assertEqual(controller.tick(), [])
        self.assertIs(controller.current, first)
        self.assertEqual(controller.queue, [second])
        controller.audio_progress(1, 5.1)
        self.assertEqual(first.played_seconds, 5.1)
        messages = controller.set_paused(False)
        self.assertEqual(messages[0]['action'], 'resume')
        self.assertEqual(controller.state, 'playing')
        self.assertEqual(controller.tick(), [])
        self.assertIs(controller.current, first)
        controller.audio_finished(1)
        self.assertIs(controller.current, second)

    def test_pause_before_audio_arrives_does_not_autoplay_on_dispatch_or_reconnect(self):
        controller = SpeechController(PlaybackConfig())
        controller.set_paused(True)
        item = SpeechItem(1, '/audio/1.wav', '第一段', '平静', [], {}, 10)
        self.assertTrue(controller.enqueue(item)[0]['paused'])
        self.assertTrue(controller.redispatch_current()[0]['paused'])
        messages = controller.audio_started(1)
        self.assertEqual(messages[0]['action'], 'pause')
        self.assertTrue(controller.paused)

    def test_interruption_of_paused_audio_is_partial_not_completed(self):
        controller, _, first, _ = self.setup_controller()
        deliveries = []
        controller.on_delivery = lambda item, completed: deliveries.append((item.id, completed))
        controller.audio_started(1)
        controller.set_paused(True)
        controller.cancel_turn(1)
        self.assertEqual(deliveries, [(first.id, False)])
        self.assertIsNone(controller.current)
        self.assertFalse(controller.queue)

    def test_start_receipt_during_pause_gets_full_budget_on_resume_once(self):
        controller, now, first, _ = self.setup_controller()
        states = []
        controller.on_state = lambda item, state, reason: states.append((item.id, state))
        now[0] = 101
        controller.set_paused(True)
        now[0] = 150
        controller.audio_started(first.id)
        self.assertTrue(controller.has_started)
        self.assertEqual(controller.state, 'paused')
        self.assertEqual(controller.started_at, 101)
        now[0] = 175
        controller.audio_started(first.id)
        self.assertEqual(states, [(first.id, 'playing')])
        self.assertEqual(controller.started_at, 101)
        now[0] = 201
        controller.set_paused(False)
        self.assertEqual(controller.started_at, 201)
        self.assertEqual(controller.tick(), [])
        now[0] = 230.9
        self.assertEqual(controller.tick(), [])
        self.assertIs(controller.current, first)
        now[0] = 231
        self.assertTrue(any(message.get('action') == 'stop' for message in controller.tick()))


class VoiceRevisionTests(unittest.TestCase):
    def event(self, number, text, *, corrects=None, source='voice', user='你'):
        event = Event(source, 'chat', text, user=user, corrects=corrects)
        event.ui_id = number
        return event

    def test_corrected_transcript_never_commits_old_voice_facts(self):
        revisions = VoiceRevisionWindow()
        original = self.event(1, '我喜欢上午跑步')
        revisions.advance(original)
        self.assertTrue(revisions.stage(original, '上午跑步挺好的。', '平静'))
        corrected = self.event(2, '我喜欢下午跑步', corrects=1)
        self.assertEqual(revisions.advance(corrected), [])
        self.assertTrue(revisions.stage(corrected, '那就下午去跑步。', '平静'))
        committed = revisions.advance(self.event(3, '聊点别的', source='companion'))
        self.assertEqual([text for _, text, _ in committed], ['那就下午去跑步。'])
        self.assertFalse(revisions.accepts(self.event(4, '改旧语音', corrects=2)))

    def test_stale_and_other_user_corrections_are_rejected_without_losing_pending_memory(self):
        revisions = VoiceRevisionWindow()
        original = self.event(1, '原始语音')
        revisions.advance(original)
        revisions.stage(original, '回答', '平静')
        for event in (self.event(2, '错误', corrects=9), self.event(2, '错误', corrects=1, user='别人')):
            with self.assertRaises(ValueError):
                revisions.advance(event)
        self.assertEqual(len(revisions.pending), 1)

    def test_retraction_removes_old_transcript_reply_topic_and_promise(self):
        state = ConversationState()
        original = self.event(1, '我喜欢上午跑步')
        state.observe_event(original)
        state.record_reply(original, '我待会安排上午跑步。')
        state.retract_turn(1)
        corrected = self.event(2, '下午', corrects=1)
        state.observe_event(corrected)
        context = state.context_for(corrected)
        self.assertNotIn('上午', context)
        self.assertFalse(state.unresolved_promises)
        self.assertIn('识别有误', context)


class ContinuationTests(unittest.TestCase):
    def test_side_question_preserves_topic_and_delivered_boundary(self):
        state = ConversationState()
        original = Event('voice', 'chat', '解释怎么种番茄', user='你')
        original.ui_id = 1
        state.observe_event(original)
        state.record_reply(original, '先准备花盆。')
        state.mark_interrupted(original)
        side = Event('voice', 'chat', '花盆多大？', user='你')
        state.observe_event(side)
        self.assertIn('先回答本次插问', state.context_for(side))
        resume = Event('voice', 'chat', '继续', user='你')
        context = state.context_for(resume)
        self.assertIn('解释怎么种番茄', context)
        self.assertIn('先准备花盆', context)
        self.assertIn('跳过已完整说出的段落', context)
        self.assertIsNone(state.interrupted_topic)

    def test_undo_discards_pending_memory_and_late_cancellation(self):
        state, revisions = ConversationState(), VoiceRevisionWindow()
        original = Event('voice', 'chat', '错误名字', user='你')
        original.ui_id = 1
        revisions.advance(original)
        revisions.stage(original, '错误回答', '平静')
        state.mark_interrupted(original)
        undo = Event('voice', 'chat', '错误名字', user='你', corrects=1, retract=True)
        undo.ui_id = 2
        self.assertEqual(revisions.advance(undo), [])
        state.retract_turn(1)
        state.mark_interrupted(original)
        self.assertIsNone(state.interrupted_topic)
        self.assertIsNone(revisions.latest)
        self.assertFalse(revisions.pending)
        self.assertNotIn('错误名字', state.context_for(undo))


if __name__ == '__main__':
    unittest.main()
