"""Stable identity shared by visit counts, learning and personality state."""
from __future__ import annotations

import hashlib
import json
import re

from .events import Event


def identity_key(*parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return parts[0] + ":" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def viewer_identity(event: Event | str | None) -> str:
    # String callers predate platform IDs; they explicitly address local history.
    if isinstance(event, str):
        return identity_key("local", event.strip()) if event.strip() else ""
    if event is None:
        return ""
    if event.viewer_key:
        return identity_key("account", event.source, event.viewer_key)
    name = str(event.user or "").strip()
    if not name:
        return ""
    if event.source in {"companion", "voice"}:
        return identity_key("local", name)
    return identity_key("unkeyed", event.source, name)


def qq_private_binding(app_id: str, peer_id: str) -> str:
    """Host-supplied, verified QQ binding; never accept it from message text."""
    if any(not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_.:=+/-]{1,256}", part)
           for part in (app_id, peer_id)):
        raise ValueError("QQ 私聊记忆必须绑定已验证的账号。")
    return identity_key("qq-private", app_id, peer_id)


def private_memory_binding(event: Event | None) -> str:
    """Read only the host-created private session marker, never public sources."""
    if event is None or event.source != "companion" or event.kind != "chat":
        return ""
    binding = getattr(event, "_qq_private_binding", "")
    return binding if isinstance(binding, str) and re.fullmatch(r"qq-private:[0-9a-f]{64}", binding) else ""


def migrate_local_profiles(profiles: dict) -> dict:
    """Nickname-only archives have no evidence of a platform account owner."""
    return {
        identity_key("local", str(name).strip()): {
            **profile, "display_name": str(name), "legacy_nickname": str(name),
        }
        for name, profile in profiles.items()
        if isinstance(profile, dict) and str(name).strip()
    }
