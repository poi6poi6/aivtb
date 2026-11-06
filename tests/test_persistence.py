from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.persistence import CoalescedJSONWriter, atomic_json_write
from brain.memory import Memory
from brain.learning import LearningEngine
from brain.events import Event
from brain.personality_evolution import PersonalityEvolution
from brain.autobiographical_memory import AutobiographicalMemory
from brain.config import LLMConfig, PersonalityEvolutionConfig, SelectiveMemoryConfig


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = {"count": 0, "items": []}
        self.writer = CoalescedJSONWriter(self.root / "state.json", lambda: self.state)

    async def asyncTearDown(self):
        await self.writer.close()
        self.temp.cleanup()

    async def test_burst_coalesces_and_close_flushes_latest_state(self):
        self.writer.enable(10)
        for value in range(50):
            self.state["count"] = value
            self.writer.request()
        self.assertFalse(self.writer.path.exists())
        self.assertTrue(self.writer.status()["pending"])
        await self.writer.close()
        self.assertEqual(json.loads(self.writer.path.read_text())["count"], 49)
        self.assertEqual(self.writer.status()["writes"], 1)
        self.assertFalse(self.writer.status()["pending"])

    async def test_slow_write_does_not_block_loop_or_overwrite_newer_revision(self):
        started, release = threading.Event(), threading.Event()
        snapshots = []

        def blocked_write(path, snapshot):
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                started.set()
                if not release.wait(3):
                    raise RuntimeError("test writer timeout")
            atomic_json_write(path, snapshot)

        self.writer.enable(0)
        with patch("brain.persistence.atomic_json_write", blocked_write):
            self.state["items"].append("first")
            self.writer.request()
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(started.is_set())
            self.state["items"].append("latest")
            self.writer.request()
            # Mutable live state must not leak into an in-flight snapshot.
            self.assertEqual(snapshots[0]["items"], ["first"])
            release.set()
            await self.writer.flush()
        self.assertEqual(json.loads(self.writer.path.read_text())["items"], ["first", "latest"])
        self.assertEqual(self.writer.status()["saved_revision"], 2)
        self.assertEqual(len(snapshots), 2)

    async def test_failure_preserves_previous_json_is_visible_and_flush_retries(self):
        atomic_json_write(self.writer.path, {"count": -1})
        self.writer.enable(10)
        self.writer.request()
        with patch("brain.persistence.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertLogs("brain.persistence", level="ERROR"):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    await self.writer.flush()
        self.assertEqual(json.loads(self.writer.path.read_text()), {"count": -1})
        self.assertIn("OSError", self.writer.status()["last_error"])
        self.assertNotIn("disk unavailable", self.writer.status()["last_error"])
        self.assertTrue(self.writer.status()["pending"])
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        await self.writer.flush()
        self.assertEqual(json.loads(self.writer.path.read_text()), self.state)
        self.assertEqual(self.writer.status()["last_error"], "")

    async def test_all_hot_state_owners_batch_and_durable_memory_management(self):
        memory = Memory(str(self.root / "events.jsonl"), str(self.root / "viewers.json"))
        learning = LearningEngine(str(self.root / "learning.json"))
        personality = PersonalityEvolution(
            PersonalityEvolutionConfig(state_path=str(self.root / "personality.json")), "助手"
        )
        autobiography = AutobiographicalMemory(
            SelectiveMemoryConfig(state_path=str(self.root / "autobiography.json"),
                                  core_memories_path=str(self.root / "missing.yaml"),
                                  llm_extraction=False),
            LLMConfig(api_key_env="ABSENT_TEST_KEY"), "助手",
        )
        owners = [memory, learning, personality, autobiography]
        for owner in owners:
            owner.enable_background_persistence(10)
        try:
            for index in range(10):
                event = Event("danmaku", "chat", "我喜欢蓝色花朵", user="访客", viewer_key="bili:1")
                memory.touch_viewer(event)
                learning.observe_event(event)
                personality.observe_event(event)
                await autobiography.consolidate(event, "我记下了。", "平静")
            self.assertTrue(all(owner.persistence_status()["pending"] for owner in owners))
            record = (await autobiography.list_memories())[0]
            self.assertTrue(await autobiography.edit_memory(record["id"], "喜欢紫色花朵"))
            saved = json.loads(autobiography.path.read_text(encoding="utf-8"))
            corrected = next(item for item in saved["memories"] if item["status"] == "active")
            self.assertEqual(corrected["value"], "喜欢紫色花朵")
            self.assertTrue(await autobiography.forget(corrected["id"]))
            saved = json.loads(autobiography.path.read_text(encoding="utf-8"))
            self.assertFalse(any(item["status"] == "active" for item in saved["memories"]))
            self.assertTrue(saved["write_barriers"])
        finally:
            await asyncio.gather(*(owner.close_persistence() for owner in owners))
        self.assertEqual(memory.persistence_status()["writes"], 1)
        self.assertEqual(learning.persistence_status()["writes"], 1)
        self.assertEqual(personality.persistence_status()["writes"], 1)


class SynchronousPersistenceTests(unittest.TestCase):
    def test_default_write_is_immediately_readable_without_event_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            writer = CoalescedJSONWriter(path, lambda: {"value": "已保存"})
            writer.request()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": "已保存"})
