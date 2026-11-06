from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brain.character_behavior import BehaviorTurn, CharacterBehavior
from brain.config import LLMConfig, load_persona
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.speech_text import AVATAR_ACTIONS, DEFAULT_EMOTIONS, sanitize_for_tts, strip_performance_tags


CONVERSATION_ACTIONS = {"倾听", "思考", "认可", "疑惑", "轻吐槽", "被夸"}
LEGACY_ACTIONS = {
    "挥手", "脸红", "嘟嘴", "弯眼", "弯眼口型", "脸黑", "邪恶眼", "邪恶眼口型",
    "伤心", "生气", "惊讶", "瞳孔地震", "前倾", "手机", "手机点击", "手柄",
    "向左", "向右", "穿外套", "脱外套", "戴眼镜", "摘眼镜", "眼镜1", "眼镜2",
    "短发", "披发",
}


class ConversationGestureParsingTests(unittest.TestCase):
    def test_whitelist_adds_exactly_six_actions_without_changing_emotions(self):
        self.assertEqual(AVATAR_ACTIONS, LEGACY_ACTIONS | CONVERSATION_ACTIONS)
        self.assertEqual(DEFAULT_EMOTIONS, {"开心", "吐槽", "惊讶", "得意", "委屈", "平静"})
        self.assertNotIn("吐槽", AVATAR_ACTIONS)

    def test_new_action_metadata_is_extracted_and_never_spoken(self):
        for action in CONVERSATION_ACTIONS:
            for tag in (f"[动作:{action}]", f"【动作：{action}】", f"[{action}]", f"（{action}）"):
                with self.subTest(tag=tag):
                    raw = f"[平静]{tag}这一点我听见了。"
                    self.assertEqual(strip_performance_tags(raw), ("这一点我听见了。", [action]))
                    self.assertEqual(sanitize_for_tts(raw), "这一点我听见了。")

    def test_normal_prose_and_unknown_bracketed_content_are_preserved(self):
        text = "我在倾听，也在思考；认可不代表没有疑惑，轻吐槽和被夸都很常见。[思考过程]（认可这个判断）[来源]"
        self.assertEqual(strip_performance_tags(text), (text, []))
        self.assertEqual(sanitize_for_tts(text), text)

    def test_legacy_actions_keep_their_existing_parsing(self):
        for action in LEGACY_ACTIONS:
            with self.subTest(action=action):
                self.assertEqual(strip_performance_tags(f"[动作:{action}]好的。"), ("好的。", [action]))

    def test_unknown_actions_stay_out_of_performance_and_tts(self):
        raw = "[动作:认可,删除文件,认可]这点说得通。"
        self.assertEqual(strip_performance_tags(raw), ("这点说得通。", ["认可"]))

    def test_behavior_keeps_gestures_without_promoting_them_to_emotions(self):
        for turn in (BehaviorTurn(), BehaviorTurn(care=True), BehaviorTurn(settling=True), BehaviorTurn(working="drawing")):
            for action in CONVERSATION_ACTIONS:
                with self.subTest(turn=turn, action=action):
                    behavior = CharacterBehavior(clock=lambda: 100.0)
                    self.assertEqual(behavior.perform(turn, "平静", [action]), ("平静", [action]))

    def test_behavior_keeps_native_expression_and_gesture_together(self):
        for action in CONVERSATION_ACTIONS:
            for actions in (["弯眼", action], [action, "弯眼"]):
                with self.subTest(actions=actions):
                    behavior = CharacterBehavior(clock=lambda: 100.0)
                    self.assertEqual(behavior.perform(BehaviorTurn(), "开心", actions), ("开心", actions))


class ConversationGesturePersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.key_patch = patch.dict(os.environ, {"GESTURE_TEST_KEY": "offline-test"})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        # Loading only the formal character card does not read .env or settings.
        self.persona = Persona(
            load_persona(), LLMConfig(api_key_env="GESTURE_TEST_KEY"),
            Memory(str(root / "memory.jsonl"), str(root / "viewers.json")),
        )
        # Every generation is supplied by a fake below; accidental network use fails.
        self.network_patch = patch.object(self.persona, "_http_client", side_effect=AssertionError("offline test"))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def test_formal_persona_prompt_limits_gestures_and_preserves_character(self):
        prompt = self.persona.system_prompt(Event("voice", "chat", "这一点说得通"))
        for action in CONVERSATION_ACTIONS:
            self.assertIn(action, prompt)
        for requirement in (
            "倾听和思考主要由前端", "状态自动处理", "不要求每句输出",
            "轻点头", "轻歪头", "短暂侧目", "视线短暂避开再回看",
            "与情绪“吐槽”不同", "合适时才选择一个语义主动作", "一轮不要反复指定",
            "不新增强情绪、不改写正式人设", "不自动脸红", "认真求助时收起轻吐槽",
            "前端优先保留原生脸部表情", "所有动作必须合并到[动作:...]标签",
            self.persona.cfg.name, self.persona.cfg.tone,
        ):
            self.assertIn(requirement, prompt)
        self.assertEqual(set(self.persona.cfg.emotions), DEFAULT_EMOTIONS)

    def test_light_teasing_action_remains_distinct_from_teasing_emotion(self):
        self.assertEqual(
            self.persona._parse_response("[平静][动作:轻吐槽]你这个理由倒很省事。"),
            ("平静", "你这个理由倒很省事。", ["轻吐槽"]),
        )
        self.assertEqual(
            self.persona._parse_response("[吐槽][动作:轻吐槽]你这个理由倒很省事。"),
            ("吐槽", "你这个理由倒很省事。", ["轻吐槽"]),
        )

    async def test_fragmented_stream_transmits_each_gesture_without_reading_its_tag(self):
        for action in CONVERSATION_ACTIONS:
            with self.subTest(action=action):
                async def fake_stream(*args, **kwargs):
                    for char in f"[平静][动作:{action}]这一点我听见了。":
                        yield char

                self.persona._stream_llm = fake_stream
                segments = [part async for part in self.persona.stream_reply(Event("danmaku", "chat", "这点说得通"))]
                self.assertEqual([(p.text, p.emotion, p.actions) for p in segments], [("这一点我听见了。", "平静", [action])])
                self.assertEqual(self.persona.last_actions, [action])
                self.assertEqual(self.persona.last_mode, "llm")

    async def test_stream_does_not_repeat_gesture_on_following_sentence(self):
        async def fake_stream(*args, **kwargs):
            yield "[平静][动作:认可]这一点说得通。"
            yield "后面的细节也值得看看。"

        self.persona._stream_llm = fake_stream
        segments = [part async for part in self.persona.stream_reply(Event("voice", "chat", "这点说得通"))]
        self.assertEqual([p.text for p in segments], ["这一点说得通。", "后面的细节也值得看看。"])
        self.assertEqual([p.actions for p in segments], [["认可"], []])

    async def test_stream_allows_native_expression_with_semantic_gesture(self):
        async def fake_stream(*args, **kwargs):
            for part in ("[开心][动作:弯", "眼,被", "夸]这份心意我收到了。"):
                yield part

        self.persona._stream_llm = fake_stream
        segments = [part async for part in self.persona.stream_reply(Event("danmaku", "chat", "你刚才讲得真好"))]
        self.assertEqual([(p.text, p.emotion, p.actions) for p in segments], [("这份心意我收到了。", "开心", ["弯眼", "被夸"])])

    async def test_nonstream_reply_keeps_gesture_and_native_expression(self):
        self.persona._call_llm = AsyncMock(return_value="[开心][动作:被夸,弯眼]这份心意我收到了。")
        self.assertEqual(await self.persona.reply(Event("danmaku", "chat", "你刚才讲得真好")), ("开心", "这份心意我收到了。"))
        self.assertEqual(self.persona.last_actions, ["被夸", "弯眼"])


if __name__ == "__main__":
    unittest.main()
