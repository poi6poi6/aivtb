"""Topic transitions share delivered context without reviving closed threads."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from brain.conversation import ConversationState
from brain.events import Event
from brain.viewer_identity import viewer_identity


class ConversationTopicTests(unittest.TestCase):
    def setUp(self):
        self.state = ConversationState()
        self.serial = 0
        clock = patch('brain.conversation.time.time', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def event(self, text, *, owner='42', user='甲', observe=True):
        self.serial += 1
        event = Event('danmaku', 'chat', text, user=user, viewer_key=owner,
                      ts=1000 + self.serial)
        event.ui_id = self.serial
        if observe:
            self.state.observe_event(event)
        return event

    def plan(self, *, owner='42'):
        event = self.event('帮我挑一下裙子和外套', owner=owner)
        self.state.record_reply(event, '先挑裙子，之后再看外套。')
        return event

    def topical_context(self, event):
        return self.state.context_for(event).split('最近对话', 1)[0]

    @staticmethod
    def audio(event, text):
        return SimpleNamespace(event={key: getattr(event, key) for key in
            ('source', 'kind', 'text', 'user', 'viewer_key', 'ts')},
            turn_id=event.ui_id, text=text, played_seconds=.8, created_at=event.ts)

    def test_current_topic_and_question_are_per_owner_but_public_history_remains(self):
        first = self.event('帮我挑一下裙子')
        self.state.record_reply(first, '你想选黑裙子还是白裙子？')
        second = self.event('我想聊玫瑰怎么种', owner='99', user='乙')
        self.state.record_reply(second, '玫瑰准备种在阳台吗？')
        followup = self.event('黑色的吧')
        topical = self.topical_context(followup)
        self.assertIn('裙子', topical)
        self.assertNotIn('玫瑰准备种在阳台吗', topical)
        self.assertNotIn('我想聊玫瑰怎么种', topical)
        self.assertNotIn('当前话题：我想聊玫瑰', topical)
        public = self.state.context_for(followup)
        self.assertIn('乙：我想聊玫瑰怎么种', public)
        self.assertIn('已结束的话题不因此恢复', public)

    def test_explicit_end_cancels_arrangement_and_clears_interrupted_topic(self):
        original = self.plan()
        self.state.mark_interrupted(original)
        closing = self.event('先不聊这个了')
        self.assertEqual(self.state.topics.inspect(closing).action, 'close')
        self.assertFalse(self.state.unresolved_promises)
        self.assertEqual(self.state._progress[-1].state, 'cancelled')
        self.assertIsNone(self.state.interrupted_topic)
        context = self.topical_context(closing)
        self.assertIn('取消或结束这段共同安排', context)
        self.assertNotIn('被打断、尚未讲完', context)
        self.state.record_playback(self.audio(original, '之后再看鞋子。'), True)
        self.state.record_playback(self.audio(original, '鞋子还可以搭短靴。'), False)
        self.state.mark_interrupted(original)
        self.assertFalse(self.state.unresolved_promises)
        self.assertIsNone(self.state.interrupted_topic)
        resumed = self.event('继续')
        self.assertNotIn('共同对话的进展', self.topical_context(resumed))
        self.assertNotIn('被打断、尚未讲完', self.topical_context(resumed))

    def test_real_resolution_is_completed_instead_of_cancelled(self):
        self.plan()
        skirt = self.event('就这条黑裙子')
        self.state.record_reply(skirt, '外套选短一点。')
        done = self.event('好，就这么穿。')
        self.assertEqual(self.state.topics.inspect(done).reason, 'resolved')
        self.assertFalse(self.state.unresolved_promises)
        self.assertEqual(self.state._progress[-1].state, 'completed')
        self.assertIn('已完成这段共同安排', self.topical_context(done))

    def test_closed_delivery_stays_closed_after_history_pruning_and_correction(self):
        original = self.plan()
        self.event('先不聊这个了')
        for _ in range(20):
            self.event('玫瑰开花了', owner='99')
        latest = self.event('我想聊玫瑰')
        self.state.retract_turn(latest.ui_id)
        # All original audience turns have left the shared history, so restoring
        # a recent-event watermark alone cannot protect this delayed receipt.
        self.state.mark_interrupted(original)
        self.state.record_playback(self.audio(original, '裙子还没挑完。'), False)
        self.assertIsNone(self.state.interrupted_topic)
        self.assertFalse(self.state.unresolved_promises)

    def test_implicit_shift_pauses_and_explicit_resume_recalls_arrangement(self):
        original = self.plan()
        self.state.mark_interrupted(original)
        old_topic = self.state.topics.inspect(original).topic_id
        new_subject = self.event('玫瑰应该怎么浇水？')
        frame = self.state.topics.inspect(new_subject)
        self.assertEqual(frame.action, 'shift')
        self.assertFalse(frame.ended_topic_id)
        self.assertEqual(self.state._progress[-1].state, 'pending')
        self.assertNotIn('共同对话的进展', self.topical_context(new_subject))
        self.assertNotIn('被打断、尚未讲完', self.topical_context(new_subject))
        # Late delivery from the old turn can be kept in public history, but it
        # cannot replace the new topic or open a new promise in it.
        self.state.record_reply(original, '之后再看帽子。')
        self.state.record_playback(self.audio(original, '我还在挑裙子。'), False)
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertEqual(self.state.topics.inspect(new_subject).topic_id, frame.topic_id)
        resumed = self.event('回到刚才的话题')
        self.assertEqual(self.state.topics.inspect(resumed).topic_id, old_topic)
        self.assertIn('下一步：看外套', self.topical_context(resumed))

    def test_aside_does_not_close_main_arrangement(self):
        original = self.plan()
        main_topic = self.state.topics.inspect(original).topic_id
        aside = self.event('插一句，玫瑰应该放在哪里？')
        frame = self.state.topics.inspect(aside)
        self.assertEqual(frame.action, 'aside')
        self.assertEqual(frame.previous_topic_id, main_topic)
        self.assertEqual(self.state._progress[-1].state, 'pending')
        self.assertNotIn('共同对话的进展', self.topical_context(aside))
        resumed = self.event('继续刚才的')
        self.assertEqual(self.state.topics.inspect(resumed).topic_id, main_topic)
        self.assertIn('下一步：看外套', self.topical_context(resumed))

    def test_explicit_shift_closes_old_plan_but_allows_new_delivered_plan(self):
        original = self.plan()
        self.state.mark_interrupted(original)
        changed = self.event('换个话题，聊聊玫瑰')
        frame = self.state.topics.inspect(changed)
        self.assertEqual(frame.action, 'shift')
        self.assertEqual(frame.ended_topic_id, self.state.topics.inspect(original).topic_id)
        self.assertEqual(self.state._progress[-1].state, 'cancelled')
        self.assertIsNone(self.state.interrupted_topic)
        self.state.record_reply(changed, '先挑花盆，之后再看肥料。')
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertIn('肥料', self.state.unresolved_promises[0])
        self.assertEqual(self.state._progress[-1].topic_id, frame.topic_id)
        self.state.record_reply(original, '之后再看鞋子。')
        self.assertEqual(len(self.state.unresolved_promises), 1)

    def test_retract_end_restores_topic_and_blocks_receipts_from_retracted_turn(self):
        original = self.plan()
        topic_id = self.state.topics.inspect(original).topic_id
        closing = self.event('先不聊这个了')
        self.state.retract_turn(closing.ui_id)
        self.state.record_reply(closing, '之后再看帽子。')
        correction = self.event('外套还没挑好')
        self.assertEqual(self.state.topics.inspect(correction).topic_id, topic_id)
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertIn('下一步：看外套', self.topical_context(correction))

    def test_forget_clears_one_owner_topic_and_quarantines_old_delivery(self):
        first = self.plan()
        second = self.event('我想聊玫瑰怎么种', owner='99')
        self.state.record_reply(second, '先挑花盆，之后再看肥料。')
        memory = {'slot': 'fixture', 'subject': '观众甲',
                  'owner_key': viewer_identity(first)}
        self.state.apply_memory_change(memory, None, first.ts + .1)
        self.state.record_reply(first, '之后再看帽子。')
        fresh = self.event('继续')
        self.assertNotIn('裙子', self.topical_context(fresh))
        other = self.event('肥料怎么选', owner='99')
        self.assertIn('肥料', self.topical_context(other))
        self.assertEqual(len(self.state.unresolved_promises), 1)

    def test_completed_segments_only_and_duplicates_do_not_repeat_topic_delivery(self):
        original = self.event('帮我挑一下裙子和外套')
        first = self.audio(original, '先挑裙子。')
        second = self.audio(original, '之后再看外套。')
        self.state.record_playback(first, True)
        self.state.record_playback(first, True)
        self.state.record_playback(second, False)
        followup = self.event('就这条黑裙子')
        delivered = self.state.topics.inspect(followup).delivered
        self.assertEqual(delivered.count('先挑裙子。'), 1)
        self.assertNotIn('之后再看外套。', delivered)
        self.assertFalse(self.state.unresolved_promises)


if __name__ == '__main__':
    unittest.main()
