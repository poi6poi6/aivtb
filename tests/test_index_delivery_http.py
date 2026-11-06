from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import unittest
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from brain.config import TTSConfig
from brain.tts import IndexTTS2TTS
from indextts2_server import make_handler


class IndexDeliveryHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.reference = self.root / "long.wav"
        self.short_reference = self.root / "short.wav"
        self.reference.write_bytes(b"long reference")
        self.short_reference.write_bytes(b"short reference")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            wav.writeframes(b"\xe8\x03" * 2205)
        self.audio = audio.getvalue()
        self.calls = []
        calls, audio_bytes = self.calls, self.audio

        class FakeTTS:
            def infer(self, **kwargs):
                calls.append(kwargs)
                Path(kwargs["output_path"]).write_bytes(audio_bytes)
                return kwargs["output_path"]

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeTTS()))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = httpx.AsyncClient(timeout=3, trust_env=False)
        self.base = {"text": "先把最重要的一件事做完。", "ref_audio_path": str(self.reference)}

    async def asyncTearDown(self):
        await self.client.aclose()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.worker.join(timeout=2)
        self.directory.cleanup()

    async def test_optional_temperature_reaches_model_without_changing_legacy_default(self):
        health = await self.client.get(self.url + "/health")
        self.assertIs(health.json()["temperature"], True)
        for override in ({}, {"temperature": 0.9}, {"temperature": 0.1},
                         {"temperature": 1}, {"temperature": 1.5}, {}):
            with self.subTest(override=override):
                response = await self.client.post(self.url + "/tts", json={**self.base, **override})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.content, self.audio)
                if override:
                    self.assertEqual(self.calls[-1]["temperature"], override["temperature"])
                    self.assertIsInstance(self.calls[-1]["temperature"], float)
                else:
                    self.assertNotIn("temperature", self.calls[-1])

    async def test_invalid_temperature_is_400_without_inference_or_request_id_leak(self):
        invalid = (True, False, None, "0.9", "", [], {}, 0, -0.1, 1.5001,
                   float("nan"), float("inf"), float("-inf"), 10 ** 500)
        for value in invalid:
            with self.subTest(temperature=value):
                # Raw JSON also exercises non-finite values rejected by httpx's
                # JSON encoder but accepted by Python's JSON decoder.
                response = await self.client.post(
                    self.url + "/tts",
                    content=json.dumps({**self.base, "request_id": "same", "temperature": value}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("temperature", response.json()["error"])
                self.assertEqual(self.calls, [])
        response = await self.client.post(
            self.url + "/tts", json={**self.base, "request_id": "same", "temperature": 0.9},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.calls), 1)

    async def test_b_and_mixed_delivery_survive_real_client_http_and_model_boundary(self):
        # The user kept B for chat/explanation and chose A for teasing. Check
        # both configurations through HTTP without needing the GPU or artifacts.
        cases = (
            ("今天这段旋律挺舒服的，前面轻轻铺开，后半段反而更耐听。", "平静",
             [0.42, 0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.18], 0.1927619047619048, 0.8),
            ("先把最重要的一件事做完，剩下的慢慢排，不用同时顾着所有事。", "平静",
             None, 0.408, 0.9),
            ("你这计划排得挺满，连休息都要预约，倒也很有你的风格。", "吐槽",
             [0.14, 0.06, 0.0, 0.0, 0.04, 0.0, 0.0, 0.28], 0.275, 0.8),
        )
        for expressive_teasing in (True, False):
            voice = IndexTTS2TTS(TTSConfig(
                out_dir=str(self.root / "audio"), indextts2_url=self.url,
                indextts2_ref_audio_path=str(self.reference),
                indextts2_short_ref_audio_path=str(self.short_reference),
                indextts2_long_min_chars=28, indextts2_num_beams=1, indextts2_long_num_beams=3,
                indextts2_emo_alpha=0.6, indextts2_reference_style=True,
                indextts2_natural_delivery=True, indextts2_delivery_preset="expressive",
                indextts2_expressive_teasing=expressive_teasing,
            ))
            voice._client = httpx.AsyncClient(timeout=3, trust_env=False)
            try:
                for text, emotion, vector, alpha, temperature in cases:
                    with self.subTest(text=text, expressive_teasing=expressive_teasing):
                        if emotion == "吐槽" and not expressive_teasing:
                            alpha = 0.21576923076923077  # A's .51 input, bounded natural mix
                        path = await voice.speak(text, emotion)
                        self.assertTrue(Path(path).is_file())
                        with wave.open(path, "rb") as wav:
                            self.assertGreater(wav.getnframes(), 0)
                        call = self.calls[-1]
                        self.assertEqual(call["text"], text)
                        self.assertEqual(call["emo_vector"], vector)
                        self.assertAlmostEqual(call["emo_alpha"], alpha)
                        self.assertEqual(call.get("temperature", 0.8), temperature)
                        self.assertEqual(call["num_beams"], 1)
                        self.assertEqual(Path(call["spk_audio_prompt"]), self.short_reference)
                        self.assertFalse(call["use_random"])
                        self.assertEqual(len(call["stopping_criteria"]), 1)
                        self.assertFalse(call["stopping_criteria"][0]())
            finally:
                await voice.close()
        self.assertEqual(len(self.calls), 6)


if __name__ == "__main__":
    unittest.main()
