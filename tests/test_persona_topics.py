"""Exercise topic boundaries through actual persona payloads and delivery state."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brain.character_behavior import CharacterBehavior
from brain.config import LLMConfig, PersonaConfig
from brain.conversation import ConversationState
from brain.dialogue_policy import DialoguePolicy
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.topic_flow import TopicTracker


class PersonaTopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        key = patch.dict(os.environ, {"TOPIC_OFFLINE_KEY": "offline-fake-key"})
        key.start()
        self.addCleanup(key.stop)
        network = patch("brain.persona.httpx.AsyncClient", side_effect=AssertionError("offline only"))
        network.start()
        self.addCleanup(network.stop)
        self.now = 1000.0
        self.topics = TopicTracker(clock=lambda: self.now, wall_clock=lambda: self.now)
        self.conversation = ConversationState()
        self.conversation.topics = self.topics
        self.persona = Persona(
            PersonaConfig(), LLMConfig(api_key_env="TOPIC_OFFLINE_KEY"),
            Memory(str(root / "m.jsonl"), str(root / "v.json")), topics=self.topics,
        )
        self.persona.behavior = CharacterBehavior(clock=lambda: self.now)
        self.persona.dialogue = DialoguePolicy(
            clock=lambda: self.now, wall_clock=lambda: self.now,
            care_detector=self.persona.behavior.needs_care, topics=self.topics,
        )
        self.serial = 0
        self.payloads = []

    def event(self, text, *, owner="你", observe=True):
        self.serial += 1
        self.now += 1
        event = Event("companion", "chat", text, user=owner, ts=self.now)
        event.ui_id = self.serial
        if observe:
            self.conversation.observe_event(event)
        return event

    async def respond(self, event, *, raw="[平静]先说眼下这一点。", stream=False, deliver=True):
        context = self.conversation.context_for(event)

        async def call(*args):
            self.payloads.append(self.persona._request_payload(event, extra_context=context))
            return raw

        async def tokens(*args):
            yield await call()

        if stream:
            self.persona._stream_llm = tokens
            parts = [item async for item in self.persona.stream_reply(event, extra_context=context)]
            text = "".join(item.text for item in parts)
        else:
            self.persona._call_llm = AsyncMock(side_effect=call)
            _, text = await self.persona.reply(event)
            self.persona._call_llm.assert_awaited_once()
        if deliver:
            self.conversation.record_reply(event, text)
        self.assertNotIn("topic_id", text)
        self.assertIsNone(self.persona._client)
        return text

    async def test_stable_topic_focus_updates_from_a_supplement(self):
        opening = self.event("今天开会，我说到一半又被打断了")
        await self.respond(opening, raw="[平静]你还没有讲完。")
        initial = self.topics.inspect(opening)
        supplement = self.event("主要是我准备了很久")
        await self.respond(supplement, stream=True)
        frame = self.topics.inspect(supplement)
        self.assertEqual(frame.topic_id, initial.topic_id)
        self.assertEqual(frame.anchor, initial.anchor)
        self.assertIn("准备了很久", frame.focus)
        self.assertTrue(any("你还没有讲完" in item for item in frame.delivered))
        self.assertIn("准备了很久", self.payloads[-1]["messages"][1]["content"])

    async def test_explicit_end_releases_mode_and_new_topic_starts_cleanly(self):
        opening = self.event("我很难过，先听我说，不要给建议")
        await self.respond(opening)
        self.assertTrue(self.persona.dialogue.inspect(opening).care)
        end = self.event("这件事先不聊了")
        spoken = await self.respond(
            end, stream=True, raw="[平静]好，先到这里。你还想补充什么吗？",
        )
        self.assertEqual(spoken, "好，先到这里。")
        self.assertEqual(self.topics.inspect(end).action, "close")
        self.assertEqual(self.persona.plan_for(end)["intent"], "closing")
        self.assertFalse(self.persona.dialogue.inspect(end).care)
        fresh = self.event("聊聊你喜欢的玫瑰")
        await self.respond(fresh, raw="[开心][动作:弯眼]我喜欢浅色的品种。")
        self.assertEqual(self.topics.inspect(fresh).action, "start")
        self.assertNotEqual(self.topics.inspect(opening).topic_id, self.topics.inspect(fresh).topic_id)
        self.assertFalse(self.persona.dialogue.inspect(fresh).mode)
        self.assertEqual(self.persona.last_emotion, "开心")

    async def test_explicit_shift_in_same_message_answers_the_new_content(self):
        opening = self.event("我很难过，先听我说")
        await self.respond(opening)
        shifted = self.event("换个话题，玫瑰怎么养？")
        await self.respond(shifted, stream=True, raw="[平静]先保证通风，别让花盆积水。")
        frame = self.topics.inspect(shifted)
        self.assertEqual(frame.action, "shift")
        self.assertIn("玫瑰", frame.focus)
        self.assertNotEqual(frame.topic_id, self.topics.inspect(opening).topic_id)
        self.assertFalse(self.persona.dialogue.inspect(shifted).care)
        self.assertNotEqual(self.persona.plan_for(shifted)["intent"], "closing")

    async def test_failed_generation_still_honors_topic_end(self):
        async def failed_stream(*args):
            raise RuntimeError("offline simulated model failure")
            yield  # Keep the streaming adapter's async iterator contract.

        for stream in (False, True):
            with self.subTest(stream=stream):
                opening = self.event("我今天开会很难过，先听我说")
                await self.respond(opening)
                closing = self.event("不聊这个了")
                with patch.object(self.persona, "_call_llm", AsyncMock(
                    side_effect=RuntimeError("offline simulated model failure"),
                )), patch.object(self.persona, "_stream_llm", failed_stream):
                    if stream:
                        parts = [part async for part in self.persona.stream_reply(closing)]
                        spoken = "".join(part.text for part in parts)
                    else:
                        _, spoken = await self.persona.reply(closing)
                self.assertEqual(self.persona.last_mode, "fallback")
                self.assertIn(spoken, {"好，那就先到这里。", "嗯，这件事先放下。"})
                self.assertFalse(self.persona.dialogue.inspect(closing).care)
                self.assertIsNone(self.persona._client)

    async def test_side_question_and_resume_preserve_main_stance(self):
        opening = self.event("我今天开会很难过，先听我说")
        await self.respond(opening)
        main_id = self.topics.inspect(opening).topic_id
        side = self.event("顺便问一下，玫瑰怎么养？")
        await self.respond(side, stream=True, raw="[平静]花盆先保持通风。")
        self.assertEqual(self.topics.inspect(side).action, "aside")
        self.assertFalse(self.persona.dialogue.inspect(side).care)
        self.assertFalse(self.persona.dialogue.inspect(side).mode)
        resume = self.event("回到刚才的话题")
        await self.respond(resume)
        self.assertEqual(self.topics.inspect(resume).action, "resume")
        self.assertEqual(self.topics.inspect(resume).topic_id, main_id)
        self.assertEqual(self.persona.dialogue.inspect(resume).mode, "listening")
        self.assertTrue(self.persona.dialogue.inspect(resume).care)

    async def test_end_is_not_undone_by_late_old_delivery_or_bare_continue(self):
        original = self.event("我很难过，先听我说")
        await self.respond(original, deliver=False, raw="[平静]先聊开会，之后再看外套。")
        end = self.event("这个话题到此为止")
        await self.respond(end)
        self.conversation.record_reply(original, "之后再看外套。")
        continued = self.event("继续")
        await self.respond(continued)
        self.assertNotEqual(self.topics.inspect(continued).topic_id, self.topics.inspect(original).topic_id)
        self.assertFalse(self.persona.dialogue.inspect(continued).care)
        self.assertFalse(self.conversation.unresolved_promises)

    async def test_other_viewer_and_acknowledgement_do_not_end_main_topic(self):
        original = self.event("我很难过，先听我说")
        await self.respond(original)
        other = self.event("这个话题到此为止", owner="另一个人")
        await self.respond(other)
        ack = self.event("好")
        await self.respond(ack)
        self.assertEqual(self.topics.inspect(ack).topic_id, self.topics.inspect(original).topic_id)
        self.assertNotEqual(self.topics.inspect(ack).action, "close")
        self.assertTrue(self.persona.dialogue.inspect(ack).care)

    async def test_retracting_end_restores_topic_and_original_interaction_mode(self):
        opening = self.event("我今天很难过，先听我说")
        await self.respond(opening)
        closing = self.event("这个先不聊了")
        await self.respond(closing)
        self.conversation.retract_turn(closing.ui_id)
        self.persona.retract_turn(closing.ui_id)
        followup = self.event("刚才识别错了，我还想接着说")
        await self.respond(followup)
        self.assertEqual(self.topics.inspect(followup).topic_id, self.topics.inspect(opening).topic_id)
        self.assertTrue(self.persona.dialogue.inspect(followup).care)

    def test_preview_does_not_start_topic_and_system_contains_only_fixed_guidance(self):
        preview = self.event("聊聊 SECRET_TOPIC_123 这件事", observe=False)
        for _ in range(5):
            self.persona.plan_for(preview)
            payload = self.persona._request_payload(preview)
            self.assertNotIn("SECRET_TOPIC_123", payload["messages"][0]["content"])
        next_event = self.event("开始聊玫瑰", observe=False)
        self.assertEqual(self.topics.inspect(next_event).action, "start")
        self.assertNotIn("SECRET_TOPIC_123", self.topics.context(self.topics.inspect(next_event)))


if __name__ == "__main__":
    unittest.main()
