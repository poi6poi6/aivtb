from __future__ import annotations

import unittest

from brain.arbiter import Arbiter
from brain.events import Event


class ArbiterManualTests(unittest.TestCase):
    def test_forced_event_bypasses_cooldown(self) -> None:
        arbiter = Arbiter(danmaku_cooldown_sec=60)
        arbiter.push(Event("danmaku", "chat", "first"))
        self.assertIsNotNone(arbiter.pick())
        manual = Event("danmaku", "chat", "manual", forced=True)
        arbiter.push(manual)
        self.assertIs(arbiter.pick(), manual)

    def test_manual_mode_disables_idle_event(self) -> None:
        arbiter = Arbiter(idle_comment_sec=0)
        self.assertIsNone(arbiter.pick(allow_idle=False))
        self.assertEqual(arbiter.pick(allow_idle=True).kind, "idle")

    def test_manual_mode_discards_queued_automatic_events(self) -> None:
        arbiter = Arbiter()
        automatic = Event("game", "ambient", "rain")
        manual = Event("danmaku", "chat", "manual", forced=True)
        arbiter.push(automatic)
        arbiter.push(manual)
        arbiter.discard_unforced()
        self.assertIs(arbiter.pick(allow_idle=False), manual)
        self.assertIsNone(arbiter.pick(allow_idle=False))

    def test_cooldown_retains_pending_chat_instead_of_clearing_it(self) -> None:
        arbiter = Arbiter(danmaku_cooldown_sec=60)
        arbiter.push(Event("danmaku", "chat", "first"))
        self.assertIsNotNone(arbiter.pick(allow_idle=False))
        arbiter.push(Event("danmaku", "chat", "second"))
        self.assertIsNone(arbiter.pick(allow_idle=False))
        self.assertEqual(arbiter.summary()["pending"], 1)

    def test_related_chat_is_aggregated_into_selected_event(self) -> None:
        arbiter = Arbiter(danmaku_cooldown_sec=0, aggregate_limit=3)
        for index in range(4):
            arbiter.push(Event("danmaku", "chat", f"消息{index}", user="同一观众"))
        selected = arbiter.pick(allow_idle=False)
        self.assertIsNotNone(selected)
        self.assertEqual(len(selected.related_messages), 3)
        self.assertEqual(arbiter.summary()["pending"], 0)

    def test_different_viewers_keep_separate_turns(self):
        arbiter = Arbiter(danmaku_cooldown_sec=0)
        for key in ('a', 'b', 'c'):
            arbiter.push(Event('danmaku', 'chat', '你好', user='p***', viewer_key=key))
        first = arbiter.pick(False)
        self.assertEqual(first.viewer_key, 'a')
        self.assertEqual(first.related_messages, [])
        arbiter.push(Event('danmaku', 'chat', '又来了', user='p***', viewer_key='a'))
        self.assertEqual(arbiter.pick(False).viewer_key, 'b')
        self.assertEqual(arbiter.pick(False).viewer_key, 'c')
        self.assertEqual(arbiter.pick(False).viewer_key, 'a')


if __name__ == "__main__":
    unittest.main()
