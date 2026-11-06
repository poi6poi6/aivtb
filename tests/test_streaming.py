from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from brain.config import LLMConfig, PersonaConfig
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.speech_text import SentenceChunker


class SentenceChunkerTests(unittest.TestCase):
    def test_emits_complete_sentences_and_flushes_tail(self) -> None:
        chunker = SentenceChunker(max_chars=20)
        self.assertEqual(chunker.feed("你好呀。后面还"), ["你好呀。"])
        self.assertEqual(chunker.feed("有一句！尾巴"), ["后面还有一句！"])
        self.assertEqual(chunker.flush(), ["尾巴"])

    def test_splits_long_text_without_terminal_punctuation(self) -> None:
        chunker = SentenceChunker(max_chars=12, min_soft_chars=6)
        chunks = chunker.feed("这是一段比较长的内容，需要尽快开始合成语音")
        self.assertTrue(chunks)
        self.assertLessEqual(max(map(len, chunks)), 12)


class PersonaStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_clean_sentences_and_keeps_actions_out_of_speech(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.environ["TEST_STREAM_KEY"] = "test"
            persona = Persona(
                PersonaConfig(),
                LLMConfig(api_key_env="TEST_STREAM_KEY", thinking=False),
                Memory(str(root / "memory.jsonl"), str(root / "viewers.json")),
            )

            async def fake_stream(*args, **kwargs):
                for token in ["[开心]", "[", "动作", ":挥手", "]", "你好呀。", "今天一起加油！"]:
                    yield token

            persona._stream_llm = fake_stream
            segments = [
                segment
                async for segment in persona.stream_reply(
                    Event("danmaku", "chat", "你好", user="测试观众")
                )
            ]
            os.environ.pop("TEST_STREAM_KEY", None)

        self.assertEqual([segment.text for segment in segments], ["你好呀。"])
        self.assertEqual(segments[0].actions, ["挥手"])
        self.assertEqual(persona.last_text, "你好呀。")
        self.assertEqual(persona.last_mode, "llm")

    def test_one_sentence_guard_truncates_extra_or_long_text(self) -> None:
        self.assertEqual(
            Persona._one_sentence("我会认真听你说。然后再给你很多建议！"),
            "我会认真听你说。",
        )
        sentence = Persona._one_sentence("这是一段" * 20)
        self.assertLessEqual(len(sentence), 48)
        self.assertTrue(sentence.endswith("。"))
        long_first_sentence = "这是一个非常详细的回答" * 8 + "。第二句。"
        self.assertLessEqual(len(Persona._one_sentence(long_first_sentence)), 48)


if __name__ == "__main__":
    unittest.main()
