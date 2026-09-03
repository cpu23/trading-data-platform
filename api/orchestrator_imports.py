"""Shared helper ensuring orchestrator/ is available on sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

_ORCHESTRATOR_DIR = Path(__file__).resolve().parents[1] / "orchestrator"
_orchestrator_path = str(_ORCHESTRATOR_DIR)

if _ORCHESTRATOR_DIR.is_dir() and _orchestrator_path not in sys.path:
    sys.path.append(_orchestrator_path)
