"""Small in-process latency and reliability metrics for the live pipeline."""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable


class RuntimeMetrics:
    """Track recent end-to-end latency without adding an external dependency."""

    def __init__(self, *, sample_size: int = 100, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._started: dict[int, float] = {}
        self._first_sentences: set[int] = set()
        self._samples = {
            "llm_ttft": deque(maxlen=sample_size),
            "first_sentence": deque(maxlen=sample_size),
            "reply": deque(maxlen=sample_size),
            "audio_start": deque(maxlen=sample_size),
            "tts_queue": deque(maxlen=sample_size),
            "tts_synthesis": deque(maxlen=sample_size),
            "playback_wait": deque(maxlen=sample_size),
            "search": deque(maxlen=sample_size),
            "voice_review": deque(maxlen=sample_size),
            "event_loop_lag": deque(maxlen=sample_size),
            "context_prepare": deque(maxlen=sample_size),
            "memory_recall": deque(maxlen=sample_size),
            "cognition_recall": deque(maxlen=sample_size),
            "audit_prepare": deque(maxlen=sample_size),
        }
        self.events = 0
        self.replies = 0
        self.cancelled = 0
        self.errors = 0
        self.latest: dict[str, float] = {}

    def record_event(self, event_id: int) -> None:
        self.events += 1
        self._started[event_id] = self._clock()
        # Bound the pending map even if a browser never acknowledges audio.
        if len(self._started) > 500:
            oldest = next(iter(self._started))
            self._started.pop(oldest, None)
            self._first_sentences.discard(oldest)

    def record_first_sentence(self, event_id: int) -> None:
        if event_id in self._first_sentences:
            return
        self._first_sentences.add(event_id)
        self._record_elapsed("first_sentence", event_id)

    def record_reply(self, event_id: int, *, latency: float, llm_ttft: float) -> None:
        self.replies += 1
        self._record("reply", latency)
        if llm_ttft > 0:
            self._record("llm_ttft", llm_ttft)

    def record_stage(self, name: str, seconds: float) -> None:
        if name not in {"tts_queue", "tts_synthesis", "playback_wait", "search", "voice_review", "event_loop_lag",
                        "context_prepare", "memory_recall", "cognition_recall", "audit_prepare"}:
            raise ValueError(f"Unknown pipeline stage: {name}")
        self._record(name, seconds)

    def record_audio_started(self, event_id: int) -> None:
        self._record_elapsed("audio_start", event_id)
        self._finish(event_id)

    def record_no_audio(self, event_id: int) -> None:
        """Finish an event that used mock TTS or whose synthesis produced no file."""
        self._finish(event_id)

    def record_cancelled(self, event_id: int) -> None:
        self.cancelled += 1
        self._finish(event_id)

    def record_error(self, event_id: int) -> None:
        self.errors += 1
        self._finish(event_id)

    def summary(self) -> dict:
        latency = {}
        for name, values in self._samples.items():
            samples = list(values)
            latency[name] = {
                "latest": round(self.latest.get(name, 0.0), 3),
                "p50": round(self._percentile(samples, 0.50), 3),
                "p95": round(self._percentile(samples, 0.95), 3),
                "samples": len(samples),
            }
        return {
            "events": self.events,
            "replies": self.replies,
            "cancelled": self.cancelled,
            "errors": self.errors,
            "pending": len(self._started),
            "latency": latency,
        }

    def _record_elapsed(self, name: str, event_id: int) -> None:
        started = self._started.get(event_id)
        if started is not None:
            self._record(name, self._clock() - started)

    def _record(self, name: str, value: float) -> None:
        if not math.isfinite(float(value)):
            return
        value = max(0.0, float(value))
        self._samples[name].append(value)
        self.latest[name] = value

    def _finish(self, event_id: int) -> None:
        self._started.pop(event_id, None)
        self._first_sentences.discard(event_id)

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, math.ceil(len(ordered) * fraction) - 1)
        return ordered[index]
