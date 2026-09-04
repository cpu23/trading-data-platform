"""Shared support fixtures and fake session harness for thesis fusion tests."""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

ORCH_ROOT = Path(__file__).resolve().parents[1]
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-thesis-fusion-test-state",
        "CONFIG_DIR": str(ORCH_ROOT.parent / "config"),
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "trading_data",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
THEME_ID = UUID("11111111-1111-4111-8111-111111111111")
GROUP_ID = UUID("12121212-1212-4121-8121-121212121212")
THESIS_ID = UUID("22222222-2222-4222-8222-222222222222")
BEAR_THESIS_ID = UUID("23232323-2323-4232-8232-232323232323")
SCENARIO_ID = UUID("34343434-3434-4343-8343-343434343434")
FORECAST_ID = UUID("45454545-4545-4545-8454-454545454545")
RUN_ID = UUID("56565656-5656-4565-8565-565656565656")
POSITION_ID = UUID("67676767-6767-4767-8767-676767676767")
LINK_ID = UUID("78787878-7878-4787-8787-787878787878")


class Result:
    def __init__(self, *, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class _Nested:
    """Fake ``Session.begin_nested()`` savepoint context.

    Records a rollback whenever the ``with`` body raises, mirroring the
    SQLAlchemy contract that an exception rolls the savepoint back and
    re-raises.  Statement results are unaffected: the nested body reads
    from the same queued result list.
    """

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.session.savepoint_rollbacks += 1
        return False


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commit = MagicMock()
        self.savepoint_rollbacks = 0

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.results:
            raise AssertionError(f"unexpected SQL call: {statement}")
        return self.results.pop(0)

    def begin_nested(self):
        return _Nested(self)


def evidence_row(**overrides):
    value = {
        "evidence_type": "source_claim",
        "evidence_id": "claim:capex-2026",
        "relationship": "supports",
        "excerpt": "Management raised the capex guide for the current quarter.",
        "source_family": "filings",
        "origin_key": "sec:10q:nvda:2026q2",
        "independence_key": "filings:nvda",
        "evidence_fingerprint": "a" * 64,
        "source_timestamp": NOW,
        "available_at": NOW,
        "quality_score": 0.8,
        "entailment_score": 0.9,
        "freshness_score": 0.7,
        "effective_weight": 1.0,
        "created_at": NOW,
    }
    value.update(overrides)
    return value


def desk_evidence_item(**overrides):
    value = {
        "evidence_type": "source_claim",
        "evidence_id": "claim:capex-2026",
        "relationship": "supports",
        "source_name": "Nvidia 10-Q",
        "source_family": "filings",
        "origin_key": "sec:10q:nvda:2026q2",
        "independence_key": "filings:nvda",
        "content": {"statement": "Capex guide raised.", "period": "2026Q2"},
        "source_timestamp": NOW.isoformat(),
        "quality_score": 0.8,
        "entailment_score": 0.9,
        "freshness_score": 0.7,
        "effective_weight": 1.0,
    }
    value.update(overrides)
    return value
