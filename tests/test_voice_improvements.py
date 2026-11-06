from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer

import httpx

from brain.arbiter import Arbiter
from brain.config import LLMConfig, PersonaConfig, PlaybackConfig, TTSConfig, WebUIConfig
from brain.events import Event
from brain.memory import Memory
from brain.metrics import RuntimeMetrics
from brain.persona import Persona
from brain.playback import SpeechController, SpeechItem
from brain.speech_pipeline import SpeechPipeline
from brain.tts import IndexTTS2TTS
from brain.webui import WebUI
from indextts2_server import CancellationRegistry, CancelAtGenerationStep, SynthesisCancelled, make_handler


class VoicePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_cancels_busy_synthesis_and_rejects_late_segments(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        calls = []

        class Voice:
            async def speak(self, text, emotion):
                calls.append(text)
                if text == "旧回答":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                return "C:/fake/new.wav"

        messages = []

        async def broadcast(message):
            messages.append(message)

        metrics = RuntimeMetrics()
        playback = SpeechController(PlaybackConfig())
        pipeline = SpeechPipeline(Voice(), playback, broadcast, metrics)
        worker = asyncio.create_task(pipeline.run())

        def enqueue(turn, text):
            return pipeline.enqueue(turn_id=turn, segment_index=0, text=text,
                                    emotion="平静", actions=[], event=Event("voice", "chat", "问题"))

        try:
            first = enqueue(1, "旧回答")
            stale = enqueue(1, "旧回答第二段")
            await asyncio.wait_for(started.wait(), 1)
            await pipeline.interrupt_before(2)
            late = enqueue(1, "取消后晚到的旧段")
            new = enqueue(2, "新回答")
            results = await asyncio.wait_for(asyncio.gather(first, stale, late, new), 1)
            self.assertTrue(cancelled.is_set())
            self.assertEqual([r.cancelled for r in results], [True, True, True, False])
            self.assertEqual(calls, ["旧回答", "新回答"])
            self.assertEqual(playback.current.turn_id, 2)
            self.assertEqual([m["turn_id"] for m in messages if m["type"] == "audio"], [2])
            self.assertEqual(metrics.summary()["latency"]["tts_synthesis"]["samples"], 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_interrupt_after_generation_stops_all_old_playback(self):
        messages = []

        async def broadcast(message):
            messages.append(message)

        playback = SpeechController(PlaybackConfig())
        for audio_id, turn_id in ((10, 1), (11, 2), (12, 2)):
            playback.enqueue(SpeechItem(audio_id, "/audio/test.wav", "旧段", "平静", [], {}, 10,
                                        turn_id=turn_id))
        pipeline = SpeechPipeline(None, playback, broadcast)
        await pipeline.interrupt_before(3)
        self.assertIsNone(playback.current)
        self.assertFalse(playback.queue)
        self.assertFalse(any(m["type"] == "audio" for m in messages))
        self.assertTrue(any(m["type"] == "audio_control" for m in messages))

    async def test_client_sends_matching_server_cancellation(self):
        started = asyncio.Event()
        requests = []

        async def transport(request):
            requests.append((request.url.path, json.loads(request.content)))
            if request.url.path == "/tts":
                started.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json={"cancelled": True})

        with tempfile.TemporaryDirectory() as directory:
            voice = IndexTTS2TTS(TTSConfig(out_dir=directory))
            voice._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
            task = asyncio.create_task(voice.speak("旧回答"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual([p for p, _ in requests], ["/tts", "/cancel"])
                self.assertEqual(requests[0][1]["request_id"], requests[1][1]["request_id"])
            finally:
                await voice.close()

    async def test_microphone_interrupt_is_control_event(self):
        queue = asyncio.Queue()
        ui = WebUI(WebUIConfig(), queue, "data/audio", info={})
        await ui._interrupt_conversation()
        event = queue.get_nowait()
        self.assertEqual((event.source, event.kind, event.text), ("voice", "interrupt", ""))
        self.assertTrue(event.forced)

    async def test_stale_and_duplicate_audio_receipts_do_not_change_metrics(self):
        metrics = RuntimeMetrics()
        metrics.record_event(5)
        playback = SpeechController(PlaybackConfig())
        item = SpeechItem(20, "/audio/a.wav", "回答", "平静", [], {}, 10,
                          turn_id=5, ready_at=time.perf_counter() - 0.2)
        playback.enqueue(item)
        ui = WebUI(WebUIConfig(), asyncio.Queue(), "data/audio", info={}, playback=playback, metrics=metrics)
        await ui._audio_started(5)  # old audio ID happens to equal current turn ID
        self.assertEqual(metrics.summary()["pending"], 1)
        await ui._audio_started(20)
        await ui._audio_started(20)
        self.assertEqual(metrics.summary()["latency"]["audio_start"]["samples"], 1)
        self.assertEqual(metrics.summary()["latency"]["playback_wait"]["samples"], 1)


class AdaptiveReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_explanation_streams_first_sentence_before_completion_and_keeps_tail(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEST_VOICE_KEY": "test"}):
            root = Path(directory)
            persona = Persona(PersonaConfig(), LLMConfig(api_key_env="TEST_VOICE_KEY"),
                              Memory(str(root / "m.jsonl"), str(root / "v.json")))
            resumed = False

            async def fake_stream(*args):
                nonlocal resumed
                yield "[平静]先从最简单的一步开始。"
                resumed = True
                yield "把任务写下来。再选今天能做的一件事"

            persona._stream_llm = fake_stream
            event = Event("companion", "chat", "怎么安排今天的任务")
            stream = persona.stream_reply(event)
            first = await stream.__anext__()
            self.assertFalse(resumed)
            rest = [segment async for segment in stream]
            self.assertEqual([s.index for s in [first, *rest]], [0, 1, 2])
            self.assertEqual(persona.last_text, "先从最简单的一步开始。把任务写下来。再选今天能做的一件事")
            self.assertEqual(persona._request_payload(event)["max_tokens"], 320)

    async def test_explicit_short_reply_closes_stream_and_preserves_budget(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEST_VOICE_KEY": "test"}):
            root = Path(directory)
            persona = Persona(PersonaConfig(), LLMConfig(api_key_env="TEST_VOICE_KEY"),
                              Memory(str(root / "m.jsonl"), str(root / "v.json")))
            closed = []

            async def fake_stream(*args):
                try:
                    yield "[平静]先做最重要的事。第二句不该继续。"
                    self.fail("short response kept generating")
                finally:
                    closed.append(True)

            persona._stream_llm = fake_stream
            event = Event("voice", "chat", "一句话解释怎么安排")
            segments = [s async for s in persona.stream_reply(event)]
            self.assertEqual([s.text for s in segments], ["先做最重要的事。"])
            self.assertEqual(closed, [True])
            self.assertEqual(persona._request_payload(event)["max_tokens"], 80)


class CancellationTests(unittest.TestCase):
    def test_http_cancel_stops_inference_and_service_accepts_next_request(self):
        vectors = []
        started = threading.Event()

        class FakeTTS:
            def infer(self, **kwargs):
                vectors.append(kwargs['emo_vector'])
                if kwargs["text"] == "old":
                    started.set()
                    for _ in range(200):
                        kwargs["stopping_criteria"][0]()
                        time.sleep(0.01)
                    raise AssertionError("old inference was not cancelled")
                Path(kwargs["output_path"]).write_bytes(b"RIFF-test-audio")
                return kwargs["output_path"]

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeTTS(), warmed_up=True))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=1) as pool:
                reference = Path(directory) / "ref.wav"
                reference.write_bytes(b"reference")
                payload = {"text": "old", "request_id": "old", "ref_audio_path": str(reference)}
                with httpx.Client(timeout=3, trust_env=False) as first, httpx.Client(timeout=3, trust_env=False) as second:
                    old = pool.submit(first.post, url + "/tts", json=payload)
                    self.assertTrue(started.wait(2))
                    response = second.post(url + "/cancel", json={"request_id": "old"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(old.result(timeout=2).status_code, 409)
                    result = second.post(url + "/tts", json={**payload, "request_id": "new", "text": "new"})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.content, b"RIFF-test-audio")
                    self.assertIsNone(vectors[-1])
                    result = second.post(url + '/tts', json={**payload, 'request_id':'vector',
                                                              'text':'new', 'reference_style':False})
                    self.assertEqual(result.status_code, 200)
                    self.assertIsNotNone(vectors[-1])
                    self.assertTrue(second.get(url + "/health").json()["cancellation"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_cancel_before_registration_and_during_generation(self):
        registry = CancellationRegistry()
        registry.cancel("early")
        with self.assertRaises(SynthesisCancelled):
            CancelAtGenerationStep(registry.register("early"))()
        registry.finish("early")
        event = registry.register("active")
        check = CancelAtGenerationStep(event)
        self.assertFalse(check())
        registry.cancel("active")
        with self.assertRaises(SynthesisCancelled):
            check()
        registry.finish("active")
        self.assertFalse(registry.register("active").is_set())

    def test_superseded_pending_inputs_are_removed(self):
        arbiter = Arbiter()
        first = Event("companion", "chat", "旧问题", forced=True)
        first.ui_id = 1
        second = Event("companion", "chat", "新问题", forced=True)
        second.ui_id = 2
        arbiter.push(first)
        self.assertEqual(arbiter.discard_before(2), [first])
        arbiter.push(second)
        self.assertIs(arbiter.pick(False), second)


if __name__ == "__main__":
    unittest.main()
