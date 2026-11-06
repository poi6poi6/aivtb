"""记忆层：事件日志（jsonl，只追加）+ 观众档案（json）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .events import Event
from .persistence import CoalescedJSONWriter, PersistentState
from .viewer_identity import migrate_local_profiles, viewer_identity


class Memory(PersistentState):
    def __init__(self, log_path: str, viewer_db_path: str):
        self.log_path = Path(log_path)
        self.viewer_path = Path(viewer_db_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.viewers: dict[str, dict] = self._load_viewers()
        self._persistence = CoalescedJSONWriter(
            self.viewer_path, lambda: {"version": 2, "viewers": self.viewers}
        )

    def _load_viewers(self) -> dict:
        if self.viewer_path.exists():
            try:
                data = json.loads(self.viewer_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return {}
                if data.get("version") == 2 and isinstance(data.get("viewers"), dict):
                    return {key: value for key, value in data["viewers"].items() if isinstance(value, dict)}
                return migrate_local_profiles(data)
            except (OSError, json.JSONDecodeError):
                pass  # 损坏就从零开始，旧文件留着自己修
        return {}

    def _save_viewers(self) -> None:
        self._persistence.request()

    def touch_viewer(self, event: Event | str) -> None:
        """每条弹幕都记一次，不管这条最终有没有被回应。"""
        identity = viewer_identity(event)
        if not identity:
            return
        name = event if isinstance(event, str) else event.user
        viewer = self.viewers.setdefault(identity, {
            "count": 0, "first_seen": time.time(), "last_seen": 0.0,
        })
        if name:
            viewer["display_name"] = name
        viewer["count"] += 1
        viewer["last_seen"] = time.time()
        self._save_viewers()

    def viewer_note(self, event: Event | str | None) -> str:
        identity = viewer_identity(event)
        if not identity:
            return "未提供身份，不能推断是否来过"
        viewer = self.viewers.get(identity)
        if not viewer or viewer["count"] <= 1:
            return "历史发言记录很少，不能据此断定首次来访"
        if viewer["count"] >= 5:
            return f"老粉，发言过{viewer['count']}次"
        return f"见过{viewer['count']}次"

    def top_viewers(self, n: int = 5) -> str:
        items = sorted(
            self.viewers.items(), key=lambda kv: kv[1]["count"], reverse=True
        )[:n]
        return "、".join(f"{v.get('display_name', '匿名观众')}({v['count']}次)" for _, v in items)

    def record(self, event: Event, emotion: str, reply: str) -> None:
        entry = {
            "ts": time.time(),
            "ui_id": getattr(event, "ui_id", None),
            "source": event.source,
            "kind": event.kind,
            "user": event.user,
            "identity": viewer_identity(event),
            "text": event.text,
            "emotion": emotion,
            "reply": reply,
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
