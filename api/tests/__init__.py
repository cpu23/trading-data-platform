# API integration tests
"""Hermetic defaults for API tests.

Tests must never inherit the mounted operator activation, credentials, or state
from a running deployment.
"""

import os
import tempfile

_TEST_STATE_DIR = tempfile.mkdtemp(prefix="trading-api-tests-")
os.environ["STATE_DIR"] = _TEST_STATE_DIR
os.environ["LEGACY_STATE_DIR"] = ""
os.environ["LEGACY_BASIC_AUTH"] = "true"
os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"
os.environ.setdefault("CONFIG_DIR", "/app/config")
