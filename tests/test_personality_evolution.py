from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.config import LLMConfig, PersonaConfig, PersonalityEvolutionConfig
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.personality_evolution import PersonalityEvolution


class PersonalityEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "personality.json"
        self.now = [1_800_000_000.0]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _engine(self, **overrides) -> PersonalityEvolution:
        cfg = PersonalityEvolutionConfig(
            state_path=str(self.path),
            consolidation_interval=overrides.pop("consolidation_interval", 2),
            **overrides,
        )
        return PersonalityEvolution(cfg, "助手", clock=lambda: self.now[0])

    def test_initial_profile_is_generic_profile_with_boundaries(self) -> None:
        engine = self._engine()
        summary = engine.summary()
        context = engine.context_for(Event("companion", "chat", "你好", user="小雨"))

        self.assertEqual(summary["profile"], "generic-v1")
        self.assertLess(summary["traits"]["energy"], 0.45)
        self.assertGreaterEqual(summary["traits"]["boundary_strength"], 0.72)
        self.assertIn("generic-v1", context)
        self.assertIn("不得覆盖核心身份", context)

    def test_relationship_feedback_is_private_to_each_user(self) -> None:
        engine = self._engine()
        first = Event(
            "companion", "chat", "今天有点累，陪我聊聊",
            user="小雨", ts=self.now[0],
        )
        first.ui_id = 1
        engine.observe_event(first)
        engine.record_reply(first, "我在呢。", "平静")

        self.now[0] += 5
        feedback = Event(
            "companion", "chat", "谢谢，你说得对，我好多了",
            user="小雨", ts=self.now[0],
        )
        feedback.ui_id = 2
        engine.observe_event(feedback)

        relationship = engine.state["relationships"][engine._user_key(feedback)]
        self.assertGreater(relationship["familiarity"], 0)
        self.assertGreater(relationship["trust"], 0.5)
        self.assertIn("小雨", engine.context_for(feedback))
        self.assertNotIn(
            "与当前用户“阿星”的关系",
            engine.context_for(Event("companion", "chat", "你好", user="阿星")),
        )
        self.assertTrue(any(
            item["source"].get("type") == "feedback"
            for item in engine.state["evidence"]
        ))

    def test_repeated_support_preference_changes_relationship_style(self) -> None:
        engine = self._engine()
        for index, text in enumerate(("先别建议，听我说", "我只想倾诉一下"), 1):
            event = Event(
                "companion", "chat", text, user="小雨", ts=self.now[0] + index
            )
            engine.observe_event(event)
        self.assertEqual(
            engine.state["relationships"][engine._user_key(event)]["preferred_support"],
            "listening",
        )
        self.assertIn("更偏好先被倾听", engine.context_for(event))

    def test_durable_memory_creates_auditable_personality_snapshot(self) -> None:
        engine = self._engine(consolidation_interval=1)
        event = Event(
            "companion", "chat", "我终于完成了准备很久的作品",
            user="小雨", ts=self.now[0],
        )
        event.ui_id = 7
        engine.record_reply(event, "好耶，努力有结果啦！", "开心")
        before = engine.summary()["traits"]["energy"]
        engine.absorb_memories(event, [{
            "id": 23,
            "kind": "shared_episode",
            "predicate": "完成作品",
            "summary": "观众小雨终于完成了准备很久的作品。",
            "salience": 0.9,
            "confidence": 0.9,
            "reinforcement_count": 0,
            "timeline_event_ids": [41],
        }])

        self.assertGreater(engine.summary()["traits"]["energy"], before)
        self.assertEqual(engine.summary()["snapshots"], 1)
        evidence = engine.state["evidence"][0]
        self.assertEqual(evidence["source"]["memory_id"], 23)
        self.assertEqual(evidence["source"]["timeline_event_ids"], [41])
        self.assertEqual(
            engine.state["snapshots"][0]["evidence_ids"], [evidence["id"]]
        )

    def test_single_consolidation_is_limited_and_state_reloads(self) -> None:
        engine = self._engine(consolidation_interval=1)
        engine.state["total_turns"] = 1
        engine._add_evidence(
            source={"type": "test", "ref": "bounded"},
            reason="边界测试",
            deltas={"energy": -5.0, "boundary_strength": -5.0},
        )
        self.assertTrue(engine.consolidate(force=True))
        engine.close()

        loaded = self._engine(consolidation_interval=1)
        traits = loaded.summary()["traits"]
        self.assertGreaterEqual(traits["boundary_strength"], 0.72)
        self.assertGreaterEqual(traits["energy"], 0.30)
        self.assertLessEqual(
            abs(loaded.state["snapshots"][0]["changes"]["energy"]), 0.025
        )
        self.assertEqual(loaded.summary()["snapshots"], 1)

    def test_persona_prompt_receives_adaptive_overlay_below_core_rules(self) -> None:
        engine = self._engine()
        memory = Memory(
            str(Path(self.temp.name) / "memory.jsonl"),
            str(Path(self.temp.name) / "viewers.json"),
        )
        persona = Persona(
            PersonaConfig(tone="核心人格"),
            LLMConfig(api_key_env="MISSING_PERSONALITY_TEST_KEY"),
            memory,
            personality=engine,
        )
        prompt = persona.system_prompt(
            Event("companion", "chat", "你好", user="小雨")
        )
        self.assertLess(prompt.index("核心人格"), prompt.index("可塑人格状态"))
        self.assertIn("优先级低于上面的核心人格与安全规则", prompt)

    def test_account_support_preferences_survive_rename_reload_and_isolate_sources(self):
        engine = self._engine()
        first = Event("danmaku", "chat", "先别建议，听我说", user="同名", viewer_key="bili:1")
        engine.observe_event(first)
        engine.observe_event(first)
        for source, key in (("danmaku", "bili:2"), ("game", "bili:1"), ("companion", "")):
            other = Event(source, "chat", "你好", user="同名", viewer_key=key)
            self.assertNotIn("更偏好先被倾听", engine.context_for(other))
        engine.close()
        loaded = self._engine()
        renamed = Event("danmaku", "chat", "你好", user="改名后", viewer_key="bili:1")
        context = loaded.context_for(renamed)
        self.assertIn("与当前用户“改名后”", context)
        self.assertIn("更偏好先被倾听", context)
        self.assertNotIn("bili:1", context)
        self.assertNotIn(loaded._user_key(renamed), context)
        loaded.observe_event(renamed)
        self.assertEqual(len(loaded.state["relationships"]), 1)
        self.assertEqual(loaded.state["relationships"][loaded._user_key(first)]["display_name"], "改名后")

    def test_feedback_is_bound_to_account_even_when_nicknames_match(self):
        engine = self._engine(consolidation_interval=1)
        first = Event("danmaku", "chat", "你好", user="同名", viewer_key="bili:1", ts=self.now[0])
        first.ui_id = 10
        engine.observe_event(first)
        engine.record_reply(first, "你好", "开心")
        before = dict(engine.state["relationships"][engine._user_key(first)])
        other = Event("danmaku", "chat", "谢谢", user="同名", viewer_key="bili:2", ts=self.now[0] + 1)
        engine.observe_event(other)
        self.assertEqual(engine.state["relationships"][engine._user_key(first)], before)
        self.assertEqual(engine.state["evidence"], [])
        self.assertIn(engine._user_key(first), engine._pending_replies)
        self.now[0] += 2
        response = Event("danmaku", "chat", "谢谢，你刚才的回答很有帮助", user="新名字", viewer_key="bili:1", ts=self.now[0])
        engine.observe_event(response)
        relation = engine.state["relationships"][engine._user_key(first)]
        self.assertGreater(relation["trust"], before["trust"])
        self.assertEqual(engine.state["evidence"][-1]["source"]["identity"], engine._user_key(first))
        self.assertEqual(engine.state["relationship_events"][-1]["identity"], engine._user_key(first))
        self.assertEqual(engine.state["relationship_events"][-1]["user"], "新名字")
        engine.consolidate(force=True)
        engine.close()
        loaded = self._engine()
        self.assertEqual(loaded.state["snapshots"], engine.state["snapshots"])
        self.assertEqual(loaded.state["evidence"], engine.state["evidence"])
        self.assertEqual(loaded._pending_replies, {})

    def test_legacy_nickname_history_is_never_claimed_by_platform_accounts(self):
        engine = self._engine()
        state = engine._empty()
        state["version"] = 1
        state["relationships"] = {"小雨": {"familiarity": .7, "trust": .8,
            "support_counts": {"listening": 2, "advice": 0, "playful": 0},
            "preferred_support": "listening", "last_seen": self.now[0]}}
        state["relationship_events"] = [{"user": "小雨", "source": {"type": "reply"}}]
        state["evidence"] = [{"id": 1, "source": {"type": "feedback"}, "applied_snapshot_id": 1}]
        state["snapshots"] = [{"id": 1, "evidence_ids": [1]}]
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        loaded = self._engine()
        account = Event("danmaku", "chat", "你好", user="小雨", viewer_key="bili:1")
        self.assertNotIn("更偏好先被倾听", loaded.context_for(account))
        self.assertNotIn("更偏好先被倾听", loaded.context_for(Event("danmaku", "chat", "你好", user="小雨")))
        for source in ("voice", "companion"):
            self.assertIn("更偏好先被倾听", loaded.context_for(Event(source, "chat", "你好", user="小雨")))
        loaded.observe_event(account)
        self.assertEqual(loaded.state["relationships"][loaded._user_key(account)]["trust"], .5)
        for field in ("relationship_events", "evidence", "snapshots"):
            self.assertEqual(loaded.state[field], state[field])
        loaded.close()
        reloaded = self._engine()
        self.assertEqual(reloaded.state["version"], 2)
        self.assertEqual(reloaded.state["relationships"], loaded.state["relationships"])

    def test_memory_evidence_keeps_account_ownership(self):
        engine = self._engine()
        first = Event("danmaku", "chat", "一起完成作品", user="同名", viewer_key="bili:1")
        other = Event("danmaku", "chat", "你好", user="同名", viewer_key="bili:2")
        engine.absorb_memories(first, [{"id": 2, "kind": "shared_episode", "summary": "一起完成作品"}])
        self.assertIn(engine._user_key(first), engine.state["relationships"])
        self.assertNotIn(engine._user_key(other), engine.state["relationships"])
        self.assertEqual(engine.state["evidence"][0]["source"]["identity"], engine._user_key(first))

    def feedback_after_reply(self, engine, text, *, account="bili:1"):
        first = Event("danmaku", "chat", "我想聊聊", user="小雨",
                      viewer_key=account, ts=self.now[0])
        first.ui_id = 40
        engine.observe_event(first)
        engine.record_reply(first, "我在听。", "平静")
        before = dict(engine.state["relationships"][engine._user_key(first)])
        self.now[0] += 1
        feedback = Event("danmaku", "chat", text, user="小雨",
                         viewer_key=account, ts=self.now[0])
        feedback.ui_id = 41
        engine.observe_event(feedback)
        return before, engine.state["relationships"][engine._user_key(first)], feedback

    def test_polite_correction_does_not_reward_or_damage_the_relationship(self):
        engine = self._engine()
        before, after, event = self.feedback_after_reply(
            engine, "谢谢，但你说得不对，别说教")
        self.assertEqual(after["trust"], before["trust"])
        self.assertEqual(engine.state["evidence"], [])
        self.assertEqual(engine.state["traits"], engine.base_traits)
        self.assertEqual(after["support_counts"]["listening"], 1)
        self.assertEqual(after["support_counts"]["advice"], 0)
        self.assertNotIn(engine._user_key(event), engine._pending_replies)
        self.assertIn("不视为关系受损", engine.state["relationship_events"][-1]["reason"])

    def test_explicit_negative_reply_evaluation_wins_over_positive_fragment(self):
        engine = self._engine()
        before, after, _ = self.feedback_after_reply(
            engine, "谢谢，你刚才的回答有帮助，但这次的回复很敷衍")
        self.assertLess(after["trust"], before["trust"])
        self.assertIn("负向反馈", engine.state["relationship_events"][-1]["reason"])
        self.assertEqual(engine.state["evidence"][-1]["source"]["reply_ui_id"], 40)
        engine.consolidate(force=True)
        self.assertGreaterEqual(engine.state["traits"]["warmth"], engine.base_traits["warmth"])

    def test_fact_need_and_aesthetic_disagreements_are_not_bad_reply_votes(self):
        engine = self._engine()
        for text in (
            "你说得不对，我住杭州，不是上海。",
            "你错了，日期是周三。",
            "我问的是怎么保存，不是怎么打开。",
            "谢谢，别说教，先听我说。",
            "别复读，换个回答。",
            "你刚才的回答太长了，简短一点。",
            "你说得太快了。",
            "你的回复像在说教，别这样讲。",
            "你刚才的回答一直复读，换个说法。",
            "你说话太吵了，小声一点。",
            "你喜欢玫瑰，我更喜欢百合。",
            "我不喜欢粉色，我要绿色的那件。",
        ):
            with self.subTest(text=text):
                before, after, _ = self.feedback_after_reply(engine, text)
                self.assertEqual(after["trust"], before["trust"])
                self.assertEqual(engine.state["evidence"], [])
                self.assertEqual(engine.state["traits"], engine.base_traits)

    def test_reported_quoted_negated_and_generic_words_do_not_learn_feedback(self):
        engine = self._engine()
        for text in (
            "谢谢", "哈哈，好可爱", "今天好多了",
            "朋友说，你刚才的回答很有帮助。",
            "小明觉得你刚才的回答很敷衍。",
            "他说谢谢，你说得对。",
            "他说：‘你刚才的回答很有帮助’。",
            '“你刚才的回答很敷衍”是小说里的台词。',
            "> 你刚才的回答很有帮助\n我是在引用。",
            "不是说你刚才的回答没用。",
            "我没有说你刚才的回复很敷衍。",
            "你刚才的回答不是没用。",
            "你刚才的回复不敷衍。",
            "你刚才的回答没有让我觉得有帮助。",
            "你说得对才怪。",
            "你说得对，我不这么认为。",
            "如果你刚才的回复很有用，我会感谢你。",
            "你觉得你刚才的回答有帮助吗？",
        ):
            with self.subTest(text=text):
                before, after, _ = self.feedback_after_reply(engine, text)
                self.assertEqual(after["trust"], before["trust"])
                self.assertEqual(engine.state["evidence"], [])

    def test_feedback_window_does_not_attach_later_comments_to_an_old_reply(self):
        engine = self._engine()
        self.feedback_after_reply(engine, "接下来聊电影吧。")
        engine.observe_event(Event("danmaku", "chat", "你刚才的回答很有帮助",
            user="小雨", viewer_key="bili:1", ts=self.now[0] + 1))
        self.assertEqual(engine.state["evidence"], [])

    def test_support_requests_respect_negation_and_last_explicit_choice(self):
        engine = self._engine()
        cases = [
            ("别给我建议，先听我说", "listening"),
            ("不需要你给我建议", "listening"),
            ("不要讲笑话，给我建议", "advice"),
            ("先别给建议，现在还是给我建议吧", "advice"),
            ("我不是只想倾诉，给我建议", "advice"),
            ("给我建议，不，先听我说", "listening"),
            ("不要逗我，陪我聊", "listening"),
        ]
        for index, (text, expected) in enumerate(cases):
            with self.subTest(text=text):
                event = Event("danmaku", "chat", text, user="同名",
                              viewer_key=f"bili:{index}", ts=self.now[0])
                engine.observe_event(event)
                counts = engine.state["relationships"][engine._user_key(event)]["support_counts"]
                self.assertEqual(counts[expected], 1)
                self.assertEqual(sum(counts.values()), 1)

    def test_quoted_third_party_and_denied_requests_do_not_accumulate_preferences(self):
        engine = self._engine()
        for text in (
            "他说给我建议。",
            "朋友说，先别给建议，听我说。",
            "‘给我建议’是原话。",
            "我不是说给我建议。",
            "别听我说，先休息。",
            "不要逗我，也不要讲笑话。",
            "如果我说给我建议，你会怎么回答？",
        ):
            event = Event("companion", "chat", text, user="小雨", ts=self.now[0])
            engine.observe_event(event)
        relation = engine.state["relationships"][engine._user_key(event)]
        self.assertEqual(sum(relation["support_counts"].values()), 0)
        self.assertEqual(relation["preferred_support"], "balanced")

    def test_long_term_preference_survives_restart_without_overriding_current_request(self):
        engine = self._engine()
        for _ in range(3):
            event = Event("danmaku", "chat", "给我建议", user="小雨",
                          viewer_key="bili:1", ts=self.now[0])
            engine.observe_event(event)
        current = Event("danmaku", "chat", "现在先别给建议，听我说", user="小雨",
                        viewer_key="bili:1", ts=self.now[0])
        engine.observe_event(current)
        counts = engine.state["relationships"][engine._user_key(current)]["support_counts"]
        self.assertEqual(counts, {"listening": 1, "advice": 3, "playful": 0})
        engine.close()
        loaded = self._engine()
        self.assertEqual(loaded.state["relationships"][loaded._user_key(current)]["support_counts"], counts)
        self.assertIn("当前明确表达优先", loaded.context_for(current))
        other = Event("danmaku", "chat", "你好", user="小雨", viewer_key="bili:2")
        self.assertNotIn("更偏好得到具体建议", loaded.context_for(other))

    def test_familiarity_gives_grounded_behavior_without_sycophancy_or_invented_tastes(self):
        engine = self._engine()
        event = Event("companion", "chat", "你好", user="小雨")
        engine.observe_event(event)
        engine.state["relationships"][engine._user_key(event)]["familiarity"] = .7
        context = engine.context_for(event)
        self.assertIn("少重复询问已确认的背景", context)
        self.assertIn("话题相关且本轮确有证实记忆", context)
        self.assertIn("没有证据就不补写共同经历", context)
        self.assertIn("保持当前角色设定", context)
        self.assertIn("不一味赞同", context)
        self.assertIn("不随机编造新的私人爱好", context)


if __name__ == "__main__":
    unittest.main()
