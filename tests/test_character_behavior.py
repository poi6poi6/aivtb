from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brain.character_behavior import CharacterBehavior
from brain.config import LLMConfig, PersonaConfig
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona


class CharacterStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.behavior = CharacterBehavior(clock=lambda: self.now)

    def test_rapid_incompatible_emotions_settle_and_state_expires(self):
        event = Event("danmaku", "chat", "今天玩得怎么样")
        first = self.behavior.begin(event)
        self.assertEqual(self.behavior.perform(first, "开心", ["弯眼"]), ("开心", ["弯眼"]))
        self.now += 5
        second = self.behavior.begin(event)
        # Going through calm prevents the previous smile from contradicting tone.
        self.assertEqual(self.behavior.perform(second, "委屈", ["伤心"]), ("平静", []))
        self.assertEqual(second.mood, "开心")
        self.assertEqual(self.behavior.perform(second, "平静", []), ("平静", []))
        self.now += 20
        self.assertEqual(self.behavior.perform(self.behavior.begin(event), "委屈", ["伤心"]), ("委屈", ["伤心"]))
        self.now += 61
        self.assertEqual(self.behavior.inspect(event).mood, "平静")

    def test_care_overrides_cheer_immediately_then_recovers(self):
        normal = Event("danmaku", "chat", "太好了")
        self.behavior.perform(self.behavior.begin(normal), "开心", ["弯眼"])
        concern = Event("danmaku", "chat", "最近我很焦虑，能陪我说说吗")
        frame = self.behavior.begin(concern)
        self.assertTrue(frame.care)
        self.assertEqual(self.behavior.perform(frame, "吐槽", ["邪恶眼", "挥手"]), ("平静", []))
        self.assertFalse(self.behavior.allow_proactive)
        self.now += 10
        another = self.behavior.begin(normal)
        self.assertFalse(another.care)
        self.assertTrue(another.settling)
        self.assertNotIn("焦虑", self.behavior.context(another))
        self.assertEqual(self.behavior.perform(another, "开心", ["弯眼"]), ("平静", []))
        self.now += 16
        self.assertTrue(self.behavior.allow_proactive)
        self.assertEqual(self.behavior.perform(self.behavior.begin(normal), "开心", ["弯眼"]), ("开心", ["弯眼"]))

    def test_interests_do_not_override_grief_or_simple_action(self):
        for text, category in (("院里的玫瑰该怎么照顾", "花卉"), ("喜欢哪种衣服搭配", "衣物"), ("这张旧照片很特别", "旧物")):
            event = Event("danmaku", "chat", text)
            self.assertEqual(self.behavior.inspect(event).interest, category)
            self.assertEqual(self.behavior.segment_limit(event, 1), 2)
        grief = Event("danmaku", "chat", "这是我去世的母亲留下的旧照片")
        self.assertTrue(self.behavior.inspect(grief).care)
        self.assertFalse(self.behavior.inspect(grief).interest)
        for text in ("穿上外套", "一句话说说你喜欢的玫瑰"):
            self.assertEqual(self.behavior.segment_limit(Event("danmaku", "chat", text), 1), 1)

    def test_work_state_only_comes_from_host_and_does_not_block_direct_interaction(self):
        event = Event("danmaku", "chat", "你已经在画玫瑰了")
        self.assertTrue(self.behavior.allow_proactive)
        self.assertFalse(self.behavior.inspect(event).working)
        self.behavior.set_working("drawing")
        self.assertFalse(self.behavior.allow_proactive)
        frame = self.behavior.begin(event)
        self.assertEqual(frame.working, "drawing")
        self.assertEqual(self.behavior.perform(frame, "开心", ["手机", "前倾"]), ("开心", ["前倾"]))
        self.assertEqual(self.behavior.segment_limit(event, 1), 1)
        self.behavior.set_working("")
        self.assertTrue(self.behavior.allow_proactive)

    def test_requested_expression_and_wardrobe_are_not_filtered(self):
        frame = self.behavior.begin(Event("voice", "chat", "我今天有点难过，穿上外套，笑一下吧"))
        self.assertEqual(self.behavior.perform(frame, "开心", ["弯眼", "穿外套"]), ("平静", ["弯眼", "穿外套"]))

    def test_technical_topics_are_not_mistaken_for_personal_distress_or_clothes(self):
        for text in ("帮我搭配一台电脑", "我正在做压力测试", "我的程序崩溃了"):
            turn = self.behavior.inspect(Event("voice", "chat", text))
            self.assertFalse(turn.care, text)
            self.assertFalse(turn.interest, text)
        self.assertTrue(self.behavior.needs_care(Event("voice", "chat", "系统崩溃了，我很焦虑")))


class PersonaCharacterDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.key_patch = patch.dict(os.environ, {"CHARACTER_TEST_KEY": "offline-test"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.persona = Persona(PersonaConfig(), LLMConfig(api_key_env="CHARACTER_TEST_KEY"), Memory(str(root / "m.jsonl"), str(root / "v.json")))
        self.now = 100.0
        self.persona.behavior = CharacterBehavior(clock=lambda: self.now)

    def stream(self, *parts):
        async def fake_stream(*args, **kwargs):
            for part in parts:
                yield part
        self.persona._stream_llm = fake_stream

    async def collect(self, text, *, source="danmaku"):
        return [part async for part in self.persona.stream_reply(Event(source, "chat", text))]

    async def test_interested_danmaku_keeps_two_sentences_but_ordinary_chat_stays_short(self):
        self.stream("[平静]玫瑰喜欢通风。花盆也别积水。第三句不会播。")
        parts = await self.collect("你怎么照顾玫瑰")
        self.assertEqual([part.text for part in parts], ["玫瑰喜欢通风。花盆也别积水。"])
        self.stream("[平静]我在听。你慢慢说。")
        self.assertEqual(len(await self.collect("你好")), 1)
        self.stream("[平静]玫瑰喜欢通风。花盆也别积水。")
        self.assertEqual(len(await self.collect("一句话说说玫瑰怎么养")), 1)

    async def test_care_applies_to_actual_streamed_emotion_actions_and_prompt(self):
        self.stream("[开心][动作:弯眼]这话我收下了。")
        self.assertEqual((await self.collect("很高兴见到你"))[0].emotion, "开心")
        event = Event("voice", "chat", "我今天有点难过，穿上外套陪我说说吧")
        captured = {}

        async def fake_stream(*args):
            captured.update(self.persona._request_payload(event))
            for part in ("[吐", "槽][动作:邪恶", "眼,穿外套]先说说今天发生了什么。", "[开心][动作:弯眼]不用急着整理好再开口。"):
                yield part

        self.persona._stream_llm = fake_stream
        parts = [part async for part in self.persona.stream_reply(event)]
        self.assertEqual([part.emotion for part in parts], ["平静", "平静"])
        self.assertEqual([part.actions for part in parts], [["穿外套"], []])
        self.assertEqual(self.persona.last_emotion, "平静")
        self.assertIn("收起挖苦", captured["messages"][0]["content"])
        self.assertNotIn("当前“衣物”", captured["messages"][0]["content"])
        self.assertFalse(any("[" in part.text for part in parts))

    async def test_nonstream_path_obeys_same_delivery_and_interest_limits(self):
        self.persona._call_llm = AsyncMock(return_value="[平静]旧照片要避光保存。也可以做一份扫描备份。再说一句。")
        emotion, text = await self.persona.reply(Event("danmaku", "chat", "旧照片该怎么保存"))
        self.assertEqual(text, "旧照片要避光保存。也可以做一份扫描备份。")
        self.persona._call_llm = AsyncMock(return_value="[吐槽][动作:邪恶眼,穿外套]我听着，慢慢说。")
        emotion, _ = await self.persona.reply(Event("voice", "chat", "我很难过，穿上外套陪陪我"))
        self.assertEqual(emotion, "平静")
        self.assertEqual(self.persona.last_actions, ["穿外套"])

    async def test_working_still_answers_and_updates_real_token_budget(self):
        event = Event("danmaku", "chat", "聊聊你对玫瑰的喜好")
        payload = self.persona._request_payload(event)
        self.assertEqual(payload["max_tokens"], 160)
        self.assertIn("多展开一个具体细节", payload["messages"][0]["content"])
        self.persona.behavior.set_working("drawing")
        payload = self.persona._request_payload(event)
        self.assertEqual(payload["max_tokens"], 80)
        self.assertIn("宿主确认绘画任务正在执行", payload["messages"][0]["content"])
        self.stream("[平静]我比较喜欢浅色的品种。还有一句。")
        self.assertEqual(len(await self.collect(event.text)), 1)

    async def test_cancel_closes_generation_and_never_flushes_unspoken_tail(self):
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def fake_stream(*args):
            try:
                yield "[平静]先听你说。"
                entered.set()
                await asyncio.Event().wait()
                yield "这句不能出现。"
            finally:
                closed.set()

        self.persona._stream_llm = fake_stream
        stream = self.persona.stream_reply(Event("voice", "chat", "详细聊聊衣物搭配"))
        self.assertEqual((await anext(stream)).text, "先听你说。")
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.assertEqual(self.persona.last_mode, "cancelled")

    async def test_task_receipts_remain_verbatim_and_do_not_gain_interest_sentences(self):
        event = Event("danmaku", "chat", "帮我画玫瑰")
        event.agent_reply = "任务已排队，尚未开始。"
        self.stream("这个生成器不应调用。")
        parts = [part async for part in self.persona.stream_reply(event)]
        self.assertEqual([(part.text, part.emotion, part.actions) for part in parts], [(event.agent_reply, "平静", [])])
        self.assertEqual(self.persona.last_mode, "task_receipt")


if __name__ == "__main__":
    unittest.main()
