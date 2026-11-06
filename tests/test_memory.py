from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain.events import Event
from brain.memory import Memory


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log_path = root / "events.jsonl"
        self.memory = Memory(str(self.log_path), str(root / "viewers.json"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sparse_history_does_not_claim_first_visit(self) -> None:
        self.assertIn("不能推断", self.memory.viewer_note(None))
        self.assertIn("不能据此断定", self.memory.viewer_note("小明"))

        self.memory.touch_viewer("小明")
        self.assertIn("不能据此断定", self.memory.viewer_note("小明"))

        self.memory.touch_viewer("小明")
        self.assertEqual(self.memory.viewer_note("小明"), "见过2次")

    def test_audit_record_keeps_ui_event_id(self) -> None:
        event = Event("danmaku", "chat", "晚上好", user="小明")
        event.ui_id = "ui-42"
        self.memory.record(event, "开心", "晚上好呀。")

        record = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["ui_id"], "ui-42")


if __name__ == "__main__":
    unittest.main()
