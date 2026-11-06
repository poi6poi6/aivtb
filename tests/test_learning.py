from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from brain.events import Event
from brain.learning import LearningEngine
from brain.viewer_identity import viewer_identity


class LearningEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "learning.json"
        self.engine = LearningEngine(
            str(self.path), feedback_window_sec=5, consolidation_interval=5
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_learns_viewer_feedback_and_retrieves_success(self) -> None:
        base = time.time()
        prompt = Event("danmaku", "chat", "主播聊聊甜点", user="小明", ts=base)
        self.engine.observe_event(prompt)
        self.engine.learn_reply(prompt, "开心", "甜点当然会让人开心")

        follow_up = Event(
            "danmaku", "chat", "哈哈我喜欢甜点", user="小明", ts=base + 2
        )
        self.engine.observe_event(follow_up)

        episode = self.engine.state["episodes"][0]
        self.assertGreater(episode["reward"], 0)
        self.assertIn("甜点", self.engine.context_for(follow_up))
        self.assertEqual(self.engine.state["viewers"][viewer_identity(follow_up)]["interactions"], 2)
        self.assertTrue(self.path.exists())

    def test_negative_feedback_and_duplicate_reply_are_penalized(self) -> None:
        base = time.time()
        first = Event("danmaku", "chat", "说句话", user="小红", ts=base)
        self.engine.learn_reply(first, "平静", "固定回答")
        complaint = Event(
            "danmaku", "chat", "太无聊了别复读", user="小红", ts=base + 2
        )
        self.engine.observe_event(complaint)
        self.assertLess(self.engine.state["episodes"][0]["reward"], 0)

        second = Event("danmaku", "chat", "再说一句", user="小蓝", ts=base + 3)
        self.engine.learn_reply(second, "平静", "固定回答")
        self.assertEqual(self.engine.state["episodes"][-1]["reward"], -0.75)

    def test_timeout_is_settled_without_new_event(self) -> None:
        event = Event("game", "ambient", "下雨了")
        episode_id = self.engine.learn_reply(event, "吐槽", "怎么又下雨")
        self.engine.tick(time.time() + 6)
        episode = next(e for e in self.engine.state["episodes"] if e["id"] == episode_id)
        self.assertEqual(episode["reward"], -0.1)
        self.assertNotIn(episode_id, self.engine._pending)

    def test_consolidates_strategy_and_adjusts_priority(self) -> None:
        base = time.time()
        for index in range(5):
            event = Event(
                "danmaku", "chat", f"甜点话题{index}", user=f"观众{index}", ts=base + index * 3
            )
            self.engine.learn_reply(event, "开心", f"开心回复{index}")
            self.engine.observe_event(Event(
                "danmaku", "gift", "小心心", user=f"观众{index}", ts=base + index * 3 + 2
            ))
        self.engine.consolidate()
        self.assertTrue(any("开心" in item for item in self.engine.state["guidelines"]))
        self.assertGreater(self.engine.priority_bonus(Event("danmaku", "chat", "你好")), 0)

    def test_state_reloads(self) -> None:
        event = Event("game", "find", "解锁了隐藏成就")
        self.engine.learn_reply(event, "得意", "这就是实力")
        loaded = LearningEngine(str(self.path))
        self.assertEqual(loaded.summary()["interactions"], 1)
        self.assertEqual(loaded.summary()["episodes"], 1)

    def test_retired_character_lore_is_not_reused(self) -> None:
        self.engine.remember_research(
            "你是什么",
            "我是寄宿在古老佩刀里的少女守护灵",
            [{"title": "旧资料", "url": "https://example.com", "snippet": "旧设定"}],
        )
        context = self.engine.context_for(
            Event("danmaku", "chat", "你是什么", user="测试员")
        )
        self.assertNotIn("守护灵", context)
        self.assertNotIn("古老佩刀", context)

    def test_production_strategy_context_does_not_bypass_temporal_memory(self) -> None:
        event = Event("danmaku", "chat", "我最喜欢苹果", user="小明")
        self.engine.observe_event(event)
        self.engine.learn_reply(event, "开心", "原样复用会导致串记忆")

        context = self.engine.strategy_context_for(event)
        self.assertNotIn("我最喜欢苹果", context)
        self.assertNotIn("原样复用会导致串记忆", context)


if __name__ == "__main__":
    unittest.main()
