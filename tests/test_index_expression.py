from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from brain.index_expression import EMOTION_VECTORS, index_emotion_options
from indextts2_server import make_handler


LEGACY_VECTORS = {
    "开心": [0.42, 0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.18],
    "吐槽": [0.14, 0.06, 0.0, 0.0, 0.04, 0.0, 0.0, 0.28],
    "惊讶": [0.08, 0.0, 0.0, 0.0, 0.0, 0.0, 0.35, 0.12],
    "得意": [0.30, 0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.22],
    "委屈": [0.0, 0.0, 0.28, 0.0, 0.0, 0.08, 0.0, 0.22],
    "平静": [0.03, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.40],
}


class IndexExpressionPolicyTests(unittest.TestCase):
    def test_default_delivery_preserves_reference_style_for_every_emotion(self):
        for emotion in (*LEGACY_VECTORS, "未识别情绪", ""):
            for alpha in (0.0, 0.25, 0.6, 1.0):
                with self.subTest(emotion=emotion, alpha=alpha):
                    options = index_emotion_options(emotion, alpha)
                    self.assertIsNone(options["emo_vector"])
                    self.assertEqual(options["emo_alpha"], alpha)
                    self.assertEqual(
                        options,
                        index_emotion_options(emotion, alpha, natural_delivery=False),
                    )

    def test_explicit_vector_mode_keeps_existing_emotions_and_strength(self):
        self.assertEqual(EMOTION_VECTORS, LEGACY_VECTORS)
        for emotion in (*LEGACY_VECTORS, "未识别情绪"):
            expected = LEGACY_VECTORS.get(emotion, LEGACY_VECTORS["平静"])
            for natural in (False, True):
                with self.subTest(emotion=emotion, natural=natural):
                    options = index_emotion_options(
                        emotion, 0.6, reference_style=False, natural_delivery=natural,
                    )
                    self.assertEqual(options["emo_vector"], expected)
                    self.assertEqual(options["emo_alpha"], 0.6)

    def test_natural_calm_and_unknown_emotions_keep_reference_delivery(self):
        for emotion in ("平静", "未识别情绪", ""):
            for alpha in (0.0, 0.6, 1.0):
                with self.subTest(emotion=emotion, alpha=alpha):
                    options = index_emotion_options(emotion, alpha, natural_delivery=True)
                    self.assertIsNone(options["emo_vector"])

    def test_natural_emotions_preserve_distinct_direction_with_small_total_mix(self):
        directions = set()
        for emotion, expected in LEGACY_VECTORS.items():
            if emotion == "平静":
                continue
            for alpha in (0.0, 0.25, 0.6, 1.0):
                with self.subTest(emotion=emotion, alpha=alpha):
                    options = index_emotion_options(emotion, alpha, natural_delivery=True)
                    vector = options["emo_vector"]
                    strength = options["emo_alpha"]
                    self.assertEqual(vector, expected)
                    self.assertGreaterEqual(strength, 0.0)
                    self.assertLessEqual(sum(vector) * strength, 0.15 + 1e-9)
                    if alpha == 0:
                        self.assertEqual(strength, 0.0)
                    else:
                        self.assertGreater(strength, 0.0)
                        self.assertLess(strength, alpha)
                        directions.add(tuple(vector))
        self.assertEqual(len(directions), 5)

    def test_natural_strength_does_not_decrease_when_requested_strength_increases(self):
        for emotion in ("开心", "吐槽", "惊讶", "得意", "委屈"):
            with self.subTest(emotion=emotion):
                strengths = [
                    index_emotion_options(emotion, alpha, natural_delivery=True)["emo_alpha"]
                    for alpha in (0.0, 0.25, 0.6, 1.0)
                ]
                self.assertEqual(strengths, sorted(strengths))

    def test_out_of_range_strengths_are_bounded_and_nonfinite_inputs_are_rejected(self):
        for reference_style, natural_delivery in ((True, False), (False, False), (True, True)):
            options = {
                "reference_style": reference_style,
                "natural_delivery": natural_delivery,
            }
            with self.subTest(**options):
                self.assertEqual(
                    index_emotion_options("开心", -2.0, **options),
                    index_emotion_options("开心", 0.0, **options),
                )
                self.assertEqual(
                    index_emotion_options("开心", 3.0, **options),
                    index_emotion_options("开心", 1.0, **options),
                )
                for strength in (float("nan"), float("inf"), float("-inf")):
                    with self.assertRaises(ValueError):
                        index_emotion_options("开心", strength, **options)

    def test_mutating_one_request_vector_does_not_affect_the_next_request(self):
        for reference_style in (True, False):
            with self.subTest(reference_style=reference_style):
                first = index_emotion_options(
                    "开心", 0.6, reference_style=reference_style, natural_delivery=True,
                )
                first["emo_vector"][0] = 99.0
                second = index_emotion_options(
                    "开心", 0.6, reference_style=reference_style, natural_delivery=True,
                )
                self.assertEqual(second["emo_vector"], LEGACY_VECTORS["开心"])
                self.assertEqual(EMOTION_VECTORS, LEGACY_VECTORS)


class IndexExpressionHTTPTests(unittest.TestCase):
    def test_each_request_controls_inference_without_changing_voice_reference(self):
        calls = []

        class FakeTTS:
            def infer(self, **kwargs):
                calls.append(kwargs)
                Path(kwargs["output_path"]).write_bytes(b"RIFF-test-audio")
                return kwargs["output_path"]

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeTTS(), warmed_up=True))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}/tts"
        try:
            with tempfile.TemporaryDirectory() as directory:
                reference = Path(directory) / "selected-reference.wav"
                reference.write_bytes(b"reference")
                base = {
                    "text": "这倒是个好消息。", "emotion": "开心",
                    "ref_audio_path": str(reference), "emo_alpha": 0.6,
                    "reference_style": True,
                }
                requests = [
                    {},
                    {"natural_delivery": True},
                    {"natural_delivery": True, "emotion": "平静"},
                    {"natural_delivery": False},
                    {"natural_delivery": True, "reference_style": False},
                    {"natural_delivery": True, "emotion": "未识别情绪"},
                ]
                with httpx.Client(timeout=3, trust_env=False) as client:
                    for overrides in requests:
                        result = client.post(url, json={**base, **overrides})
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertEqual(result.content, b"RIFF-test-audio")

                self.assertEqual(len(calls), len(requests))
                self.assertIsNone(calls[0]["emo_vector"])
                self.assertEqual(calls[0]["emo_alpha"], 0.6)
                self.assertEqual(calls[1]["emo_vector"], LEGACY_VECTORS["开心"])
                self.assertGreater(calls[1]["emo_alpha"], 0.0)
                self.assertLess(calls[1]["emo_alpha"], 0.6)
                self.assertLessEqual(
                    sum(calls[1]["emo_vector"]) * calls[1]["emo_alpha"], 0.15 + 1e-9,
                )
                self.assertIsNone(calls[2]["emo_vector"])
                self.assertIsNone(calls[3]["emo_vector"])
                self.assertEqual(calls[3]["emo_alpha"], 0.6)
                self.assertEqual(calls[4]["emo_vector"], LEGACY_VECTORS["开心"])
                self.assertEqual(calls[4]["emo_alpha"], 0.6)
                self.assertIsNone(calls[5]["emo_vector"])
                for call in calls:
                    self.assertEqual(Path(call["spk_audio_prompt"]), reference)
                    self.assertEqual(call["text"], base["text"])
                    self.assertFalse(call["use_random"])
                    self.assertNotIn("emo_audio_prompt", call)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
