"""Test-only path setup for the sibling cognitive-os source project."""

import sys
from pathlib import Path

COGNITIVE_SRC = Path(__file__).resolve().parents[2] / "构想" / "src"
if COGNITIVE_SRC.is_dir():
    sys.path.insert(0, str(COGNITIVE_SRC))
