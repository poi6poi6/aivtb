"""Locate the existing Codex CLI without changing the user's PATH or login."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class CodexNotFoundError(FileNotFoundError):
    """The configured Codex executable cannot be found on this host."""


def resolve_codex_command(command='codex'):
    if not isinstance(command, str) or not command.strip():
        return None
    found = shutil.which(command)
    if found:
        # The child runs from a temporary folder, not necessarily our cwd.
        return str(Path(found).absolute())
    # Respect explicit paths/other commands: never silently switch clients.
    if os.name != 'nt' or command.lower() not in {'codex', 'codex.exe'}:
        return None
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    if not base.is_absolute():
        return None
    # Desktop-launched services do not inherit Codex's private PATH. Only
    # inspect the desktop app's own bin directory, not arbitrary user folders.
    folder = base / 'OpenAI' / 'Codex' / 'bin'
    try:
        candidates = [folder / 'codex.exe', *folder.glob('*/codex.exe')]
    except OSError:
        return None
    available = []
    for path in candidates:
        try:
            if path.is_file():
                available.append((path.stat().st_mtime_ns, str(path)))
        except OSError:
            continue
    # No persistent cache: app updates can remove/replace versioned folders.
    return max(available)[1] if available else None
