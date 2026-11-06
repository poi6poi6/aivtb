"""Non-blocking sentence TTS and playback dispatch."""
from __future__ import annotations

import asyncio
import itertools
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .events import Event
from .playback import SpeechController, SpeechItem
from .metrics import RuntimeMetrics

Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class SynthesisResult:
    audio_path: str | None
    playback_messages: list[dict[str, Any]]
    cancelled: bool = False


@dataclass
class _SynthesisJob:
    turn_id: int
    segment_index: int
    text: str
    emotion: str
    actions: list[str]
    event: Event
    future: asyncio.Future[SynthesisResult]
    queued_at: float
    playback_generation: int
    prepared_audio_path: str | None = None


class SpeechPipeline:
    def __init__(self, voice: Any, playback: SpeechController, broadcast: Broadcast,
                 metrics: RuntimeMetrics | None = None, *, synthesis_timeout_sec=60,
                 wait_notice_sec=8, on_wait=None, on_synthesized=None) -> None:
        if (not math.isfinite(synthesis_timeout_sec) or synthesis_timeout_sec <= 0
                or not math.isfinite(wait_notice_sec) or wait_notice_sec < 0):
            raise ValueError('语音合成期限必须为正数，等待提示时间不能小于零')
        self.voice = voice
        self.synthesis_timeout_sec = synthesis_timeout_sec
        self.wait_notice_sec = wait_notice_sec
        self.on_wait = on_wait
        self.on_synthesized = on_synthesized
        self.resource_available = lambda: True
        self.playback = playback
        self.broadcast = broadcast
        self.queue: asyncio.Queue[_SynthesisJob] = asyncio.Queue(maxsize=32)
        self._audio_ids = itertools.count(1)
        self._cancelled_turns: set[int] = set()
        self._pending_by_turn: dict[int, int] = {}
        self._prepared_paths: dict[str, int] = {}
        self.metrics = metrics
        self._minimum_turn = 0
        self._active_job: _SynthesisJob | None = None
        self.pending_audio_path: str | None = None
        self._active_synthesis: asyncio.Task | None = None
        self._active_cancel: asyncio.Event | None = None
        self.event_valid: Callable[[Event], bool] | None = None
        self.on_failure: Callable[[Event, str], None] | None = None
        self.expect_audio = True
        self._runner: asyncio.Task | None = None
        self._closed = False
        self._priority_interrupts: set[int] = set()
        self._generation_token = object()

    def _invalid(self, job):
        return (self._closed or job.playback_generation != self.playback.generation
                or job.turn_id in self._cancelled_turns or job.turn_id < self._minimum_turn
                or self.playback.turn_is_stale(job.turn_id, job.event.ts)
                or self.event_valid is not None and not self.event_valid(job.event))

    @property
    def busy(self) -> bool:
        return bool(self._pending_by_turn)

    def pending_turn_ids(self) -> set[int]:
        return set(self._pending_by_turn)

    def pending_audio_paths(self) -> set[str]:
        paths = set(self._prepared_paths)
        if self.pending_audio_path:
            paths.add(self.pending_audio_path)
        return paths

    def begin_turn(self, event: Event, turn_id: int) -> int:
        """Bind the whole stream to the playback generation in which it began."""
        binding = getattr(event, "_speech_generation", None)
        if binding is None or binding[:2] != (self._generation_token, turn_id):
            binding = (self._generation_token, turn_id, self.playback.generation)
            event._speech_generation = binding
        return binding[2]

    def enqueue(
        self,
        *,
        turn_id: int,
        segment_index: int,
        text: str,
        emotion: str,
        actions: list[str],
        event: Event,
        prepared_audio_path: str | None = None,
    ) -> asyncio.Future[SynthesisResult]:
        if prepared_audio_path is not None:
            if not isinstance(prepared_audio_path, (str, Path)) or not str(prepared_audio_path).strip():
                raise ValueError('缓存语音路径无效')
            prepared_audio_path = str(prepared_audio_path)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SynthesisResult] = loop.create_future()
        generation = self.begin_turn(event, turn_id)
        if (self._closed or turn_id < self._minimum_turn or turn_id in self._cancelled_turns
                or generation != self.playback.generation):
            future.set_result(SynthesisResult(None, [], cancelled=True))
            return future
        job = _SynthesisJob(
            turn_id=turn_id,
            segment_index=segment_index,
            text=text,
            emotion=emotion,
            actions=list(actions),
            event=event,
            future=future,
            queued_at=time.perf_counter(),
            playback_generation=generation,
            prepared_audio_path=prepared_audio_path,
        )
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            future.set_result(SynthesisResult(None, [], cancelled=True))
        else:
            if prepared_audio_path:
                self._prepared_paths[prepared_audio_path] = self._prepared_paths.get(prepared_audio_path, 0) + 1
            self._pending_by_turn[turn_id] = self._pending_by_turn.get(turn_id, 0) + 1
            if event.priority >= self.playback.cfg.interrupt_priority:
                candidates = []
                if self._active_job:
                    candidates.append((self._active_job.turn_id, self._active_job.event.priority))
                if self.playback.current:
                    item = self.playback.current
                    candidates.append((item.turn_id if item.turn_id is not None else item.id, item.priority))
                for owner, priority in candidates:
                    if owner != turn_id and priority < event.priority:
                        self._cancelled_turns.add(owner)
                        self._priority_interrupts.add(owner)
                        if self._active_job and self._active_job.turn_id == owner and self._active_cancel:
                            self._active_cancel.set()
        return future

    async def _speak_when_available(self, text, emotion, *, turn_id=None, segment_index=None):
        while not self.resource_available():
            await asyncio.sleep(.25)
        speak_segment = getattr(self.voice, "speak_segment", None)
        if callable(speak_segment) and turn_id is not None and segment_index is not None:
            return await speak_segment(text, emotion, turn_id=turn_id, segment_index=segment_index)
        return await self.voice.speak(text, emotion)

    async def _notify_wait(self, job):
        await asyncio.sleep(self.wait_notice_sec)
        if not self._invalid(job) and self.on_wait is not None:
            try:
                await self.on_wait(job.event)
            except Exception:
                # A presentation callback must not turn valid synthesis into failure.
                pass

    async def run(self) -> None:
        self._runner = asyncio.current_task()
        try:
            await self._run_jobs()
        finally:
            self._closed = True
            # Futures waiting for capacity must settle even when shutdown occurs
            # during a pause. No background synthesis survives its worker.
            while not self.queue.empty():
                job = self.queue.get_nowait()
                self._finish_cancelled(job)
                self.queue.task_done()
            self._pending_by_turn.clear()
            self._prepared_paths.clear()
            self._cancelled_turns.clear()
            self._priority_interrupts.clear()
            self._runner = None

    async def _wait_for_capacity(self, job: _SynthesisJob) -> bool:
        while not self._invalid(job):
            if self.playback.has_capacity(job.event.priority):
                return True
            # Playback controls are synchronous. A bounded poll also notices
            # receipt invalidation and clear without registering per-turn hooks.
            await asyncio.sleep(.05)
        return False

    async def _prepare_audio(self, job: _SynthesisJob) -> str | None:
        if not await self._wait_for_capacity(job):
            return None
        if job.prepared_audio_path is not None:
            self.pending_audio_path = job.prepared_audio_path
            return job.prepared_audio_path
        started = time.perf_counter()
        if self.metrics:
            self.metrics.record_stage("tts_queue", started - job.queued_at)
        notice = asyncio.create_task(self._notify_wait(job)) if self.on_wait is not None else None
        try:
            audio_path = await asyncio.wait_for(
                self._speak_when_available(job.text, job.emotion, turn_id=job.turn_id,
                                           segment_index=job.segment_index), self.synthesis_timeout_sec)
        finally:
            if notice is not None:
                notice.cancel()
                await asyncio.gather(notice, return_exceptions=True)
        self.pending_audio_path = audio_path
        if self.metrics:
            self.metrics.record_stage("tts_synthesis", time.perf_counter() - started)
        if audio_path and not self._invalid(job) and self.on_synthesized is not None:
            try:
                self.on_synthesized(job.event, audio_path)
            except Exception:
                # Optional cache bookkeeping cannot discard audio already synthesized.
                pass
        return audio_path

    async def _run_jobs(self) -> None:
        while True:
            job = await self.queue.get()
            cancellation_waiter = None
            try:
                for owner in tuple(self._priority_interrupts):
                    self._priority_interrupts.discard(owner)
                    for message in self.playback.cancel_turn(owner):
                        await self.broadcast(message)
                    if owner not in self._pending_by_turn:
                        self._cancelled_turns.discard(owner)
                if self._invalid(job):
                    self._finish_cancelled(job)
                    continue
                self._active_job = job
                self._active_cancel = asyncio.Event()
                await self.broadcast({"type": "speech_stage", "turn_id": job.turn_id, "stage": "synthesizing"})
                if self._invalid(job):
                    self._finish_cancelled(job)
                    continue
                self._active_synthesis = asyncio.create_task(self._prepare_audio(job))
                cancellation_waiter = asyncio.create_task(self._active_cancel.wait())
                await asyncio.wait(
                    {self._active_synthesis, cancellation_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._active_cancel.is_set():
                    self._active_synthesis.cancel()
                    await asyncio.gather(self._active_synthesis, return_exceptions=True)
                    self._finish_cancelled(job)
                    continue
                audio_path = self._active_synthesis.result()
                if self._invalid(job):
                    self._finish_cancelled(job)
                    continue
                messages: list[dict[str, Any]] = []
                if not audio_path and self.expect_audio and self.on_failure:
                    self.on_failure(job.event, '语音合成未返回音频')
                if audio_path:
                    await self.broadcast({"type": "speech_stage", "turn_id": job.turn_id, "stage": "ready"})
                    # Controls may run while TTS/broadcast awaits. Check again
                    # before the synchronous enqueue so it cannot trim a reply.
                    if not await self._wait_for_capacity(job):
                        self._finish_cancelled(job)
                        continue
                    audio_id = next(self._audio_ids)
                    speech = SpeechItem(
                        id=audio_id,
                        url=f"/audio/{Path(audio_path).name}",
                        text=job.text,
                        emotion=job.emotion,
                        actions=job.actions,
                        event={
                            "source": job.event.source,
                            "kind": job.event.kind,
                            "text": job.event.text,
                            "user": job.event.user,
                            "viewer_key": job.event.viewer_key,
                            "ts": job.event.ts,
                            "related_messages": [dict(message) for message in job.event.related_messages],
                            "task_id": job.event.task_id,
                            "task_revision": job.event.task_revision,
                            "task_notice": job.event.task_notice,
                            "task_ts": job.event.ts,
                            **{name: getattr(job.event, name) for name in
                               ('rehearsal_id', 'host_question_id', 'host_question_attempt', 'is_fallback', 'program_key',
                                'director_topic_generation',
                                'tarot_session_id', 'tarot_reading_id', 'tarot_attempt_id', 'tarot_stage',
                                'companion_followup_token', 'companion_activity_revision',
                                'media_narration_id', 'media_narration_stage')
                               if hasattr(job.event, name)},
                        },
                        priority=job.event.priority,
                        created_at=job.event.ts,
                        turn_id=job.turn_id,
                        segment_index=job.segment_index,
                    )
                    messages = self.playback.enqueue(speech)
                    for message in messages:
                        await self.broadcast(message)
                if not job.future.done():
                    job.future.set_result(SynthesisResult(audio_path, messages))
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as exc:
                if self.on_failure:
                    self.on_failure(job.event, '语音合成失败：' + type(exc).__name__)
                await self.broadcast({
                    "type": "tts_error",
                    "turn_id": job.turn_id,
                    "segment_index": job.segment_index,
                    "error": f"{type(exc).__name__}：语音合成未完成，请检查语音服务或稍后重试",
                })
                if not job.future.done():
                    job.future.set_result(SynthesisResult(None, []))
            finally:
                children = [task for task in (self._active_synthesis, cancellation_waiter) if task is not None]
                for child in children:
                    if not child.done():
                        child.cancel()
                if children:
                    await asyncio.gather(*children, return_exceptions=True)
                self._active_job = None
                self.pending_audio_path = None
                self._active_synthesis = None
                self._active_cancel = None
                if job.prepared_audio_path:
                    count = self._prepared_paths.get(job.prepared_audio_path, 1) - 1
                    if count > 0:
                        self._prepared_paths[job.prepared_audio_path] = count
                    else:
                        self._prepared_paths.pop(job.prepared_audio_path, None)
                pending = self._pending_by_turn.get(job.turn_id, 1) - 1
                if pending > 0:
                    self._pending_by_turn[job.turn_id] = pending
                else:
                    self._pending_by_turn.pop(job.turn_id, None)
                    self._cancelled_turns.discard(job.turn_id)
                    await self.broadcast({"type": "speech_stage", "turn_id": job.turn_id, "stage": "drained"})
                self.queue.task_done()

    async def cancel_turn(self, turn_id: int) -> None:
        self._cancelled_turns.add(turn_id)
        if self._active_job and self._active_job.turn_id == turn_id and self._active_cancel:
            self._active_cancel.set()
        for message in self.playback.cancel_turn(turn_id):
            await self.broadcast(message)
        if self._pending_by_turn.get(turn_id, 0) == 0:
            self._cancelled_turns.discard(turn_id)

    async def interrupt_before(self, turn_id: int) -> None:
        """Keep the new conversational turn; reject even late segments of older turns."""
        self._minimum_turn = max(self._minimum_turn, turn_id)
        owners = set(self._pending_by_turn)
        items = list(self.playback.queue)
        if self.playback.current:
            items.append(self.playback.current)
        owners.update(item.turn_id if item.turn_id is not None else item.id for item in items)
        # Remove queued items before current to avoid dispatching another stale item.
        if self.playback.current:
            current_owner = self.playback.current.turn_id
        else:
            current_owner = None
        for owner in sorted(owners, key=lambda value: value == current_owner):
            if owner < self._minimum_turn:
                await self.cancel_turn(owner)

    def release_turn(self, turn_id: int) -> None:
        self._cancelled_turns.discard(turn_id)

    async def close(self) -> None:
        self._closed = True
        if self._runner and self._runner is not asyncio.current_task():
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        while not self.queue.empty():
            job = self.queue.get_nowait()
            self._finish_cancelled(job)
            self.queue.task_done()
        self._pending_by_turn.clear()
        self._prepared_paths.clear()
        close = getattr(self.voice, "close", None)
        if close is not None:
            await close()

    @staticmethod
    def _finish_cancelled(job: _SynthesisJob) -> None:
        if not job.future.done():
            job.future.set_result(SynthesisResult(None, [], cancelled=True))
