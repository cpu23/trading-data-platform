"""Hermetic defaults for API tests."""

import os
import tempfile
from pathlib import Path

_TEST_STATE_DIR = tempfile.mkdtemp(prefix="trading-api-tests-")
os.environ["STATE_DIR"] = _TEST_STATE_DIR
os.environ.setdefault("DEPLOYMENT_MODE", "test")
os.environ.setdefault("CONFIG_DIR", str(Path(__file__).resolve().parents[2] / "config"))
