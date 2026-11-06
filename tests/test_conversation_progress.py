"""Delivered shared arrangements advance and close without crossing accounts."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brain.conversation import ConversationState
from brain.events import Event
from brain.viewer_identity import viewer_identity


class ConversationProgressTests(unittest.TestCase):
    def setUp(self):
        self.state = ConversationState()
        self.serial = 0
        self.clock = patch('brain.conversation.time.time', return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def event(self, text, *, owner='42', user='甲', observe=True):
        self.serial += 1
        event = Event('danmaku', 'chat', text, user=user, viewer_key=owner,
                      ts=1000 + self.serial)
        event.ui_id = self.serial
        if observe:
            self.state.observe_event(event)
        return event

    def plan(self, *, owner='42', user='甲'):
        event = self.event('陪我挑一下裙子和外套', owner=owner, user=user)
        self.state.record_reply(event, '先挑裙子，之后再看外套。')
        return event

    def hints(self, event):
        return '\n'.join(line for line in self.state.context_for(event).splitlines()
                         if '共同对话的进展' in line or '这段共同安排' in line)

    @staticmethod
    def audio(event, text):
        return SimpleNamespace(event={name: getattr(event, name) for name in
            ('source', 'kind', 'text', 'user', 'viewer_key', 'ts')},
            turn_id=event.ui_id, text=text, played_seconds=.5, created_at=event.ts)

    def test_skirt_then_coat_progresses_and_explicitly_closes(self):
        self.plan()
        skirt = self.event('就这条黑裙子')
        self.assertIn('下一步：看外套', self.hints(skirt))
        self.state.record_reply(skirt, '那外套选短一点，别把腰线压住。')
        coat = self.event('外套还需要调整吗？')
        self.assertIn('已经谈到：那外套选短一点', self.hints(coat))
        self.assertIn('等待对方确认', self.hints(coat))
        self.event('好')
        self.assertEqual(len(self.state.unresolved_promises), 1, 'a generic acknowledgement is not completion')
        closing = self.event('好，就这么穿。')
        self.assertIn('对方已完成', self.hints(closing))
        self.assertFalse(self.state.unresolved_promises)
        self.state.record_reply(closing, '嗯，这样就够了。')
        self.assertEqual(self.hints(self.event('那外套先放这里')), '')

    def test_weather_hypotheses_history_questions_and_negated_plans_are_not_todos(self):
        for text in (
            '之后就下雨了。', '假如你想换，之后再看外套。',
            '如果有时间，我待会安排一下。', '以前我们先挑裙子，之后再看外套。',
            '我之前说过之后再看外套。', '我不打算之后再看外套。',
            '之后不看外套了。', '之后再看外套吗？',
            '“之后再看外套”只是一个例子。', '你说之后再看外套。',
        ):
            with self.subTest(text=text):
                self.state.record_reply(self.event('聊聊天'), text)
                self.assertFalse(self.state.unresolved_promises)

    def test_only_matching_owner_can_see_or_close_progress(self):
        original = self.plan()
        other = self.event('外套已经挑好了', owner='99')
        self.assertEqual(self.hints(other), '')
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertEqual(self.hints(self.event('继续', owner='', user=None)), '')
        anonymous = self.event('挑衣服', owner='', user=None)
        self.state.record_reply(anonymous, '之后再看帽子。')
        self.assertEqual(len(self.state.unresolved_promises), 1)
        renamed = self.event('外套已经挑好了', user='改名后的甲')
        self.assertEqual(viewer_identity(original), viewer_identity(renamed))
        self.assertIn('对方已完成', self.hints(renamed))
        self.assertFalse(self.state.unresolved_promises)

    def test_old_arrangements_are_only_prompted_for_related_chat(self):
        self.plan()
        self.assertEqual(self.hints(self.event('今天的午饭挺好吃')), '')
        self.assertEqual(self.hints(self.event('好，就这样吧。')), '',
                         'closing another topic does not finish an unrelated arrangement')
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertIn('下一步：看外套', self.hints(self.event('外套呢')))
        self.assertIn('下一步：看外套', self.hints(self.event('继续')))

    def test_cancellation_and_topic_change_block_late_segments(self):
        for text in ('外套先不看了', '不用再看外套了', '取消外套这一步',
                     '换个话题吧', '先不聊这个', '换个话题，聊聊玫瑰'):
            with self.subTest(text=text):
                self.state = ConversationState()
                original = self.plan()
                closing = self.event(text)
                self.assertFalse(self.state.unresolved_promises)
                self.assertIn('对方已取消或结束', self.hints(closing))
                self.state.record_reply(original, '等下我再挑鞋子。')
                self.assertFalse(self.state.unresolved_promises, 'a late old segment cannot reopen the plan')
                fresh = self.event('现在想挑鞋子了')
                self.state.record_reply(fresh, '待会再看鞋子。')
                self.assertEqual(len(self.state.unresolved_promises), 1)

    def test_repeated_future_or_unfinished_steps_are_not_completed_progress(self):
        self.plan()
        for text in ('之后再看外套。', '我还没挑外套。', '外套尚未选好。',
                     '假如要选外套，可以挑短一点的。'):
            with self.subTest(text=text):
                event = self.event('外套呢')
                self.state.record_reply(event, text)
                self.assertNotIn('已经谈到', self.hints(event))
        actual = self.event('那外套呢')
        self.state.record_reply(actual, '外套选短一点。')
        self.assertIn('已经谈到', self.hints(actual))

    def test_negated_completion_and_cancel_negation_do_not_close(self):
        self.plan()
        for text in ('外套还没挑好', '外套尚未完成', '不是已经挑好外套了',
                     '不要取消，外套还没挑好', '不用取消外套这一步',
                     '别忘了之后看外套', '假如外套已经挑好了怎么办？'):
            with self.subTest(text=text):
                self.event(text)
                self.assertEqual(len(self.state.unresolved_promises), 1)

    def test_segmented_deliveries_keep_anchors_and_deduplicate_receipts(self):
        event = self.event('帮我搭一下衣服')
        first = self.audio(event, '先挑裙子。')
        second = self.audio(event, '之后再看外套。')
        self.state.record_playback(second, False)
        self.assertFalse(self.state.unresolved_promises, 'an unfinished clip is not a promise')
        self.state.record_playback(first, True)
        self.state.record_playback(second, True)
        self.state.record_playback(second, True)
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertEqual(sum(turn.text == second.text for turn in self.state.turns), 1)
        self.assertIn('下一步：看外套', self.hints(self.event('就这条黑裙子')))

    def test_retract_source_and_delayed_playback_cannot_restore_it(self):
        original = self.plan()
        self.state.retract_turn(original.ui_id)
        self.assertFalse(self.state.unresolved_promises)
        self.assertIsNone(self.state.record_playback(self.audio(original, '之后再看外套。'), True))
        self.state.record_reply(original, '等下我再挑鞋子。')
        self.assertFalse(self.state.unresolved_promises)

    def test_retract_completion_restores_previous_progress(self):
        self.plan()
        choice = self.event('就这条裙子')
        self.state.record_reply(choice, '外套选短一点。')
        closing = self.event('好，就这么穿。')
        self.assertFalse(self.state.unresolved_promises)
        self.state.retract_turn(closing.ui_id)
        self.assertEqual(len(self.state.unresolved_promises), 1)
        self.assertIn('已经谈到：外套选短一点', self.hints(self.event('外套还得再看看')))

    def test_forget_or_edit_clears_only_owner_and_blocks_old_receipts(self):
        for replacement in (None, '观众甲不喜欢外套'):
            with self.subTest(replacement=replacement):
                self.state = ConversationState()
                first = self.plan()
                self.plan(owner='99')
                cutoff = first.ts + .1
                memory = {'slot': 'fixture', 'subject': '观众甲',
                          'owner_key': viewer_identity(first)}
                self.state.apply_memory_change(memory, replacement, cutoff)
                self.assertEqual(len(self.state.unresolved_promises), 1)
                self.assertEqual(self.hints(self.event('外套呢')), '')
                self.assertIn('下一步：看外套', self.hints(self.event('外套呢', owner='99')))
                self.assertIsNone(self.state.record_playback(self.audio(first, '之后再看外套。'), True))
                self.assertEqual(len(self.state.unresolved_promises), 1)


if __name__ == '__main__':
    unittest.main()
