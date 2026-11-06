from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from brain.config import LLMConfig, PersonaConfig, PersonalityEvolutionConfig
from brain.events import Event
from brain.learning import LearningEngine
from brain.memory import Memory
from brain.persona import Persona
from brain.personality_evolution import PersonalityEvolution
from brain.viewer_identity import viewer_identity


class ViewerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = Memory(str(self.root / "events.jsonl"), str(self.root / "viewers.json"))

    def tearDown(self):
        self.temp.cleanup()

    def test_visit_count_rename_reload_and_prompt_never_transfer_by_nickname(self):
        first = Event("danmaku", "chat", "你好", user="同名", viewer_key="bili:1")
        for _ in range(5):
            self.memory.touch_viewer(first)
        other = Event("danmaku", "chat", "你好", user="同名", viewer_key="bili:2")
        self.memory.touch_viewer(other)
        persona = Persona(PersonaConfig(), LLMConfig(api_key_env="ABSENT_TEST_KEY"), self.memory)
        self.assertIn("老粉", persona.user_prompt(first))
        self.assertNotIn("老粉", persona.user_prompt(other))
        for event in (
            Event("game", "chat", "你好", user="同名", viewer_key="bili:1"),
            Event("companion", "chat", "你好", user="同名"),
            Event("danmaku", "chat", "你好", user="同名"),
        ):
            self.assertNotIn("老粉", self.memory.viewer_note(event))
        renamed = Event("danmaku", "chat", "你好", user="改名", viewer_key="bili:1")
        self.memory.touch_viewer(renamed)
        reloaded = Memory(str(self.root / "events.jsonl"), str(self.root / "viewers.json"))
        self.assertEqual(reloaded.viewer_note(renamed), "老粉，发言过6次")
        self.assertIn("改名(6次)", reloaded.top_viewers())
        self.assertNotIn("bili:1", reloaded.top_viewers())
        self.assertNotIn(viewer_identity(renamed), persona.user_prompt(renamed))

    def test_legacy_visits_only_migrate_to_unkeyed_local_identity(self):
        self.memory.viewer_path.write_text(json.dumps({"旧昵称": {"count": 20}}), encoding="utf-8")
        migrated = Memory(str(self.root / "events.jsonl"), str(self.memory.viewer_path))
        local = Event("companion", "chat", "你好", user="旧昵称")
        voice = Event("voice", "chat", "你好", user="旧昵称")
        platform = Event("danmaku", "chat", "你好", user="旧昵称", viewer_key="bili:1")
        self.assertIn("20次", migrated.viewer_note(local))
        self.assertEqual(migrated.viewer_note(local), migrated.viewer_note(voice))
        self.assertNotIn("老粉", migrated.viewer_note(platform))
        migrated.touch_viewer(local)
        saved = json.loads(migrated.viewer_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertNotIn("旧昵称", saved["viewers"])

    def test_learning_profiles_and_personality_share_identity_and_feedback_isolation(self):
        learning = LearningEngine(str(self.root / "learning.json"))
        personality = PersonalityEvolution(
            PersonalityEvolutionConfig(state_path=str(self.root / "personality.json")), "助手"
        )
        base = time.time()
        first = Event("danmaku", "chat", "我喜欢蓝色花朵", user="同名", viewer_key="bili:1", ts=base)
        learning.observe_event(first)
        learning.learn_reply(first, "平静", "花朵很好看。")
        personality.observe_event(first)
        other = Event("danmaku", "chat", "无聊别复读", user="同名", viewer_key="bili:2", ts=base+2)
        learning.observe_event(other)
        episode = learning.state["episodes"][0]
        self.assertFalse(any("负向" in item["reason"] for item in episode["feedback"]))
        self.assertIn(episode["id"], learning._pending)
        self.assertEqual(personality._user_key(first), viewer_identity(first))
        self.assertIn(viewer_identity(first), learning.state["viewers"])
        self.assertNotIn("蓝色花朵", " ".join(learning.state["viewers"][viewer_identity(other)]["recent_messages"]))
        renamed = Event("danmaku", "chat", "无聊别复读", user="新名字", viewer_key="bili:1", ts=base+3)
        learning.observe_event(renamed)
        self.assertLess(episode["reward"], 0)
        self.assertNotIn(episode["id"], learning._pending)
        self.assertEqual(learning.state["viewers"][viewer_identity(first)]["interactions"], 2)
        self.assertEqual(learning.state["viewers"][viewer_identity(first)]["display_name"], "新名字")
        reloaded = LearningEngine(str(self.root / "learning.json"))
        self.assertEqual(reloaded.state["viewers"], learning.state["viewers"])

    def test_legacy_learning_interest_does_not_attach_to_platform_accounts(self):
        path = self.root / "learning.json"
        path.write_text(json.dumps({"version": 1, "viewers": {"同名": {
            "interactions": 9, "topics": {"蓝花": 9}, "recent_messages": ["我喜欢蓝花"],
        }}}), encoding="utf-8")
        learning = LearningEngine(str(path))
        local = Event("voice", "chat", "喜欢什么", user="同名")
        platform = Event("danmaku", "chat", "喜欢什么", user="同名", viewer_key="bili:1")
        self.assertIn("蓝花", learning.context_for(local))
        self.assertNotIn("蓝花", learning.context_for(platform))
        self.assertEqual(learning.state["version"], 2)
