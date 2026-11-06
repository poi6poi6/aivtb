"""Offline multi-turn checks of the policy actually sent to the speaker."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from brain.character_behavior import CharacterBehavior
from brain.conversation import memory_change_owner
from brain.config import LLMConfig, PersonaConfig
from brain.dialogue_policy import DialoguePolicy
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.viewer_identity import viewer_identity
from tests.test_runtime_integration import main_function


class PersonaDialogueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        key = patch.dict(os.environ, {"DIALOGUE_OFFLINE_KEY": "offline-fake-key"})
        key.start()
        self.addCleanup(key.stop)
        network = patch("brain.persona.httpx.AsyncClient", side_effect=AssertionError("offline test"))
        network.start()
        self.addCleanup(network.stop)
        self.persona = Persona(
            PersonaConfig(), LLMConfig(api_key_env="DIALOGUE_OFFLINE_KEY"),
            Memory(str(root / "memory.jsonl"), str(root / "viewers.json")),
        )
        self.now = 100.0
        self.persona.behavior = CharacterBehavior(clock=lambda: self.now)
        self.persona.dialogue = DialoguePolicy(
            clock=lambda: self.now, care_detector=self.persona.behavior.needs_care,
        )
        self.serial = 0
        self.captured = []

    def event(self, text, *, owner="a", source="companion"):
        self.serial += 1
        value = Event(source, "chat", text, user="同名", viewer_key=owner)
        value.ui_id = self.serial
        return value

    async def reply(self, event, *, stream=False, raw="[平静]这件事，我们接着说。"):
        async def call(*args):
            self.captured.append(self.persona._request_payload(event))
            return raw

        async def tokens(*args):
            yield await call()

        if stream:
            self.persona._stream_llm = tokens
            result = [part async for part in self.persona.stream_reply(event)]
            spoken = "".join(part.text for part in result)
        else:
            self.persona._call_llm = AsyncMock(side_effect=call)
            _, spoken = await self.persona.reply(event)
            self.persona._call_llm.assert_awaited_once()
        self.assertNotIn("本轮回答策略", spoken)
        self.assertIsNone(self.persona._client)
        return spoken

    async def test_current_need_overrides_learned_default_in_both_generation_paths(self):
        self.persona.personality = Mock()
        self.persona.personality.context_for.return_value = "长期更习惯直接获得建议。"
        listening = self.event("我很难过，先听我说，不要给建议")
        await self.reply(listening)
        payload = self.captured[-1]
        self.assertEqual(self.persona.dialogue.inspect(listening).mode, "listening")
        self.assertIn("不主动给方案", payload["messages"][1]["content"])
        advice = self.event("现在给我一点建议")
        await self.reply(advice, stream=True)
        self.assertEqual(self.persona.dialogue.inspect(advice).mode, "advice")
        self.assertIn("一个贴近实际的下一步", self.captured[-1]["messages"][1]["content"])
        self.assertNotIn("不主动给方案", self.captured[-1]["messages"][1]["content"])
        self.assertEqual(len(self.captured), 2)

    async def test_emotional_thread_survives_timer_and_other_viewer_then_closes(self):
        await self.reply(self.event("我很难过，先听我说，不要给建议"))
        self.now += 90
        other = self.event("你喜欢玫瑰吗", owner="b")
        await self.reply(other, raw="[开心][动作:弯眼]玫瑰很适合放在窗边。")
        self.assertFalse(self.persona.dialogue.inspect(other).care)
        self.assertIn("当前“花卉”", self.captured[-1]["messages"][0]["content"])
        followup = self.event("他后来还是没有回复")
        await self.reply(followup, stream=True, raw="[吐槽][动作:邪恶眼]这次等待确实不好受。")
        self.assertEqual(self.persona.last_emotion, "平静")
        self.assertEqual(self.persona.last_actions, [])
        self.assertIn("收起挖苦", self.captured[-1]["messages"][0]["content"])
        closed = self.event("这件事解决了，换个话题吧")
        await self.reply(closed)
        next_topic = self.event("你喜欢玫瑰吗")
        await self.reply(next_topic, raw="[开心][动作:弯眼]我偏爱浅色的品种。")
        self.assertFalse(self.persona.dialogue.inspect(next_topic).care)
        self.assertEqual(self.persona.last_emotion, "开心")

    async def test_correction_guidance_persists_and_retraction_removes_it(self):
        correction = self.event("你刚才理解错了，我不是这个意思")
        await self.reply(correction)
        self.assertTrue(self.persona.dialogue.inspect(correction).repair)
        followup = self.event("我说的是昨天那件事")
        await self.reply(followup, stream=True)
        self.assertTrue(self.persona.dialogue.inspect(followup).repair)
        self.persona.retract_turn(correction.ui_id)
        next_event = self.event("接着说")
        self.assertFalse(self.persona.dialogue.inspect(next_event).repair)
        self.assertNotIn("修复", self.persona.last_text)

    async def test_requested_lightness_is_allowed_but_new_distress_overrides_it(self):
        await self.reply(self.event("我很难过，先听我说"))
        light = self.event("好了，逗逗我")
        await self.reply(light, raw="[开心][动作:弯眼]今天这杯茶，倒比我先精神了。")
        self.assertEqual(self.persona.last_emotion, "开心")
        self.assertNotIn("收起挖苦、得意和玩笑", self.captured[-1]["messages"][0]["content"])
        serious = self.event("我现在又很焦虑")
        await self.reply(serious, stream=True, raw="[吐槽][动作:邪恶眼]先说眼下最让你不安的那件事。")
        self.assertEqual(self.persona.last_emotion, "平静")
        self.assertEqual(self.persona.last_actions, [])

    async def test_memory_change_clears_only_owner_and_rejects_unseen_old_event(self):
        a = self.event("我很难过，先听我说")
        b = self.event("我很焦虑，先听我说", owner="b")
        await self.reply(a)
        await self.reply(b)
        late = self.event("先听我说，不要给建议")
        memory = {
            "owner_key": viewer_identity(a), "subject": "观众同名",
            "source": {"source": a.source, "user": a.user, "viewer_key": a.viewer_key},
        }
        self.persona.apply_memory_change(memory)
        self.persona.dialogue.observe(late)
        self.assertFalse(self.persona.dialogue.inspect(self.event("接着说")).care)
        self.assertTrue(self.persona.dialogue.inspect(self.event("接着说", owner="b")).care)
        self.assertIsNone(self.persona._behavior_event)

    def test_preview_is_read_only_and_policy_never_copies_private_input(self):
        private = self.event("先听我说，不要给建议，我的私密标记是 SECRET-ONLY-HERE")
        for _ in range(10):
            self.persona.plan_for(private)
            payload = self.persona._request_payload(private)
            self.assertNotIn("SECRET-ONLY-HERE", payload["messages"][0]["content"])
        ordinary = self.event("接着说")
        self.assertFalse(self.persona.dialogue.inspect(ordinary).mode)
        self.assertTrue(self.persona.behavior.allow_proactive)

    async def test_memory_management_hook_clears_persona_policy(self):
        persona = Mock()
        namespace = dict(
            Event=Event, time=time, register_event=AsyncMock(), conversation=Mock(),
            persona=persona, voice_revisions=Mock(), push_event=AsyncMock(),
            director=Mock(), memory_change_owner=memory_change_owner,
            qq_channel=None,
        )
        namespace["voice_revisions"].apply_memory_change.return_value = None
        change = main_function("memory_changed", namespace)
        memory = {"subject": "角色", "source": {"source": "manual"}}
        await change(memory, None)
        persona.apply_memory_change.assert_called_once_with(memory)


if __name__ == "__main__":
    unittest.main()
