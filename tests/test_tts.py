from __future__ import annotations

import unittest

from brain.config import TTSConfig
from brain.tts import FishSpeechTTS, GptSovitsTTS, IndexTTS2TTS, MockTTS, make_tts
from brain.speech_text import sanitize_for_tts
from indextts2_server import EMOTION_VECTORS


class TTSBackendTests(unittest.TestCase):
    def test_factory_selects_all_backends(self) -> None:
        self.assertIsInstance(make_tts(TTSConfig(backend="mock")), MockTTS)
        self.assertIsInstance(make_tts(TTSConfig(backend="gpt_sovits")), GptSovitsTTS)
        self.assertIsInstance(make_tts(TTSConfig(backend="indextts2")), IndexTTS2TTS)
        self.assertIsInstance(make_tts(TTSConfig(backend="fish_speech")), FishSpeechTTS)

    def test_emotion_vectors_match_indextts2_contract(self) -> None:
        self.assertEqual(set(EMOTION_VECTORS), {"开心", "吐槽", "惊讶", "得意", "委屈", "平静"})
        for vector in EMOTION_VECTORS.values():
            self.assertEqual(len(vector), 8)
            self.assertLessEqual(sum(vector), 0.800001)
            self.assertTrue(all(0 <= value <= 1 for value in vector))

    def test_tts_defensively_removes_performance_tags(self) -> None:
        self.assertEqual(
            sanitize_for_tts("[委屈][嘟嘴]嗯……你好（轻笑）"),
            "嗯……你好",
        )


if __name__ == "__main__":
    unittest.main()
