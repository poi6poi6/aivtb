from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.config import LLMConfig, PersonaConfig, PlaybackConfig
from brain.conversation import ConversationState, is_backchannel
from brain.events import Event
from brain.memory import Memory
from brain.persona import Persona
from brain.playback import SpeechController, SpeechItem
from brain.tts import emotion_strength


class DeliveryMemoryTests(unittest.TestCase):
    def item(self, number, text):
        return SpeechItem(number, "/audio/test.wav", text, "平静", [],
                          {"source": "voice", "kind": "chat", "text": "怎么做", "user": "你"},
                          10, turn_id=1, segment_index=number - 1)

    def test_only_finished_segments_become_spoken_history(self):
        memory = ConversationState()
        controller = SpeechController(PlaybackConfig())
        persisted = []

        def delivered(item, completed):
            event = memory.record_playback(item, completed)
            if event is not None:
                persisted.append(item.text)

        controller.on_delivery = delivered
        controller.enqueue(self.item(1, "先把任务写下来。"))
        controller.enqueue(self.item(2, "我待会帮你做完全部任务。"))
        self.assertFalse(memory.turns)
        controller.audio_started(1)
        self.assertFalse(memory.turns)
        controller.audio_finished(1)
        controller.audio_started(2)
        controller.audio_progress(2, 0.75)
        controller.cancel_turn(1)
        controller.audio_finished(2)  # late client receipt must not commit stopped text
        self.assertEqual(persisted, ["先把任务写下来。"])
        self.assertEqual([turn.text for turn in memory.turns], persisted)
        self.assertFalse(memory.unresolved_promises)
        context = memory.context_for(Event("voice", "chat", "不是这个意思"))
        self.assertIn("无法确认具体听到了哪些词", context)
        self.assertIn("0.8秒", context)

    def test_unplayed_audio_is_never_committed(self):
        for reason in ("play_rejected", "autoplay_disabled", "error", "ended"):
            with self.subTest(reason=reason):
                received = []
                controller = SpeechController(PlaybackConfig())
                controller.on_delivery = lambda item, done: received.append(done)
                controller.enqueue(self.item(1, "未播放的内容。"))
                controller.audio_finished(1, reason)
                self.assertFalse(received)

    def test_progress_ignores_stale_ids_and_nonfinite_numbers(self):
        controller = SpeechController(PlaybackConfig())
        item = self.item(1, "正文。")
        controller.enqueue(item)
        controller.audio_started(1)
        controller.audio_progress(1, 0.5)
        for audio_id, seconds in ((9, 10), (1, float("nan")), (1, -1), (1, "bad"), (1, 0.1)):
            controller.audio_progress(audio_id, seconds)
        self.assertEqual(item.played_seconds, 0.5)

    def test_skip_clear_and_preemption_report_partial_delivery(self):
        for action in ("skip", "clear", "preempt"):
            with self.subTest(action=action):
                results = []
                controller = SpeechController(PlaybackConfig())
                controller.on_delivery = lambda item, done: results.append((item.id, done))
                controller.enqueue(self.item(1, "正文。"))
                controller.audio_started(1)
                if action == "skip":
                    controller.skip_current()
                elif action == "clear":
                    controller.clear()
                else:
                    urgent = self.item(2, "紧急内容。")
                    urgent.priority = 100
                    controller.enqueue(urgent)
                self.assertEqual(results, [(1, False)])


class BackchannelTests(unittest.TestCase):
    def test_only_complete_acknowledgements_match(self):
        for text in ("嗯嗯", "对。", "你继续！", "然后呢", "我在听"):
            self.assertTrue(is_backchannel(text), text)
        for text in ("对，但是我说的是明天", "不是这个意思", "嗯，我想换个话题", "好难过", "等一下", "", "继续帮我分析这个问题"):
            self.assertFalse(is_backchannel(text), text)

    def test_emotion_strength_is_contextual_bounded_and_smooth(self):
        calm = emotion_strength("平静", "我在听。", 0.75)
        excited = emotion_strength("惊讶", "真的！", 0.75)
        self.assertLess(calm, excited)
        self.assertLessEqual(abs(emotion_strength("惊讶", "真的！", 0.75, calm) - calm), 0.121)
        self.assertGreaterEqual(calm, 0)
        self.assertLessEqual(excited, 1)


class SegmentEmotionTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_metadata_changes_next_sentence_and_is_not_spoken(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"PRESENCE_TEST_KEY": "test"}):
            root = Path(directory)
            persona = Persona(PersonaConfig(), LLMConfig(api_key_env="PRESENCE_TEST_KEY"),
                              Memory(str(root / "m.jsonl"), str(root / "v.json")))

            async def fake_stream(*args):
                for token in ("[惊", "讶][动作:", "挥手]真的呀！", "[平", "静]那我们慢慢分析。", "先写下问题"):
                    yield token

            persona._stream_llm = fake_stream
            segments = [segment async for segment in persona.stream_reply(Event("voice", "chat", "帮我分析"))]
            self.assertEqual([s.emotion for s in segments], ["惊讶", "平静", "平静"])
            self.assertEqual([s.actions for s in segments], [["挥手"], [], []])
            self.assertEqual([s.text for s in segments], ["真的呀！", "那我们慢慢分析。", "先写下问题"])
            self.assertEqual(persona.last_text, "真的呀！那我们慢慢分析。先写下问题")


if __name__ == "__main__":
    unittest.main()
