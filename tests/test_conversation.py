from __future__ import annotations

import unittest

from brain.conversation import ConversationState
from brain.events import Event


class ConversationStateTests(unittest.TestCase):
    def test_tracks_recent_dialogue_question_and_interruption(self) -> None:
        state = ConversationState()
        event = Event("danmaku", "chat", "最近在玩什么游戏？", user="小明", ts=10)
        state.observe_event(event)
        state.record_reply(event, "我最近在探索新地图，你们想看哪个区域？")
        next_event = Event("danmaku", "chat", "去雪山", user="小红", ts=20)
        state.observe_event(next_event)
        state.mark_interrupted(next_event)
        context = state.context_for(next_event)
        self.assertIn("最近对话", context)
        self.assertIn("小明", context)
        self.assertIn("被打断", context)


if __name__ == "__main__":
    unittest.main()
