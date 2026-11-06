"""Coalesced JSON persistence with one ordered writer and explicit durable flush."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


class PersistenceWriteError(OSError):
    """A durable flush failed; dirty state remains available for retry."""


def atomic_json_write(path: Path, snapshot: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique siblings avoid collisions with other state files or stale temp files.
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CoalescedJSONWriter:
    """Snapshots are captured on the event loop, then serialized off the loop.

    A single task awaits each write before capturing the next revision. At most
    one snapshot is in flight; dirty updates coalesce in the owner's live state.
    Objects remain synchronous until explicitly enabled by their application.
    """

    def __init__(self, path: Path, snapshot: Callable[[], Any]) -> None:
        self.path = path
        self._snapshot = snapshot
        self._background = False
        self._debounce = 0.25
        self._revision = 0
        self._written = 0
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._waiters: list[tuple[int, asyncio.Future]] = []
        self._closed = False
        self.last_error = ""
        self.last_saved_at = 0.0
        self.write_count = 0

    def enable(self, debounce_sec: float = 0.25) -> None:
        asyncio.get_running_loop()
        if self._closed:
            raise RuntimeError("persistence is closed")
        self._debounce = max(0.0, float(debounce_sec))
        self._background = True
        if self._wake is None:
            self._wake = asyncio.Event()

    def request(self) -> None:
        if self._closed:
            raise RuntimeError("persistence is closed")
        self._revision += 1
        if self._background:
            self._ensure_task()
        else:
            self._write_sync()

    def _write_sync(self) -> None:
        try:
            atomic_json_write(self.path, self._snapshot())
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: 状态写入失败，等待重试"
            raise
        self._saved(self._revision)

    def _saved(self, revision: int) -> None:
        self._written = revision
        self.last_error = ""
        self.last_saved_at = time.time()
        self.write_count += 1

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"persist:{self.path.name}")

    def _finish_waiters(self, revision: int, error: Exception | None = None) -> None:
        remaining = []
        for target, future in self._waiters:
            if future.done():
                continue
            if target <= revision:
                if error is None:
                    future.set_result(None)
                else:
                    # Do not share the worker's live traceback with consumers:
                    # traceback clearing by one waiter must not touch this task.
                    future.set_exception(PersistenceWriteError(
                        f"{self.path.name}: {type(error).__name__}: {error}"
                    ))
            else:
                remaining.append((target, future))
        self._waiters = remaining

    async def _wait(self, delay: float) -> None:
        assert self._wake is not None
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _run(self) -> None:
        await self._wait(self._debounce)
        while self._written < self._revision and not self._closed:
            revision = self._revision
            try:
                snapshot = copy.deepcopy(self._snapshot())
                await asyncio.to_thread(atomic_json_write, self.path, snapshot)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: 状态写入失败，等待重试"
                log.error("State persistence failed for %s: %s", self.path.name, exc)
                self._finish_waiters(revision, exc)
                # Keep the dirty revision and expose the failure; new flushes
                # wake the retry immediately, otherwise retry at a bounded rate.
                await self._wait(max(1.0, self._debounce))
            else:
                self._saved(revision)
                self._finish_waiters(revision)
                if self._written < self._revision:
                    await self._wait(self._debounce)

    async def flush(self) -> None:
        if self._written >= self._revision:
            return
        if self._closed:
            raise RuntimeError("persistence is closed with unsaved changes")
        if not self._background:
            self._write_sync()
            return
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((self._revision, future))
        self._ensure_task()
        assert self._wake is not None
        self._wake.set()
        try:
            await future
        finally:
            self._waiters = [(target, item) for target, item in self._waiters if item is not future]

    async def close(self) -> None:
        if self._closed:
            return
        try:
            while self._written < self._revision:
                await self.flush()
        finally:
            self._closed = True
            if self._wake is not None:
                self._wake.set()
            if self._task is not None:
                await self._task

    def status(self) -> dict[str, Any]:
        return {
            "background": self._background,
            "pending": self._revision > self._written,
            "revision": self._revision,
            "saved_revision": self._written,
            "writes": self.write_count,
            "last_saved_at": self.last_saved_at,
            "last_error": self.last_error,
            "closed": self._closed,
        }


class PersistentState:
    """Lifecycle shared by state owners; application starts and drains writers."""

    _persistence: CoalescedJSONWriter

    def enable_background_persistence(self, debounce_sec: float = 0.25) -> None:
        self._persistence.enable(debounce_sec)

    async def flush_persistence(self) -> None:
        await self._persistence.flush()

    async def close_persistence(self) -> None:
        await self._persistence.close()

    def persistence_status(self) -> dict[str, Any]:
        return self._persistence.status()
