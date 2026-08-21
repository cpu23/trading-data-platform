"""Repository tests for the autonomous thesis-fusion desk.

Every helper is exercised with a queued-result fake session, mirroring
test_research.py conventions: SQL text and bound parameters are asserted,
and no helper ever commits or rolls back the caller's transaction.
"""

import math
import os
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-thesis-fusion-test-state",
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
        "DEPLOYMENT_MODE": "test",
        "LEGACY_BASIC_AUTH": "1",
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

from research_intelligence.contracts import (  # noqa: E402
    EvidenceSignal,
    Scenario,
)
from thesis_fusion import (  # noqa: E402
    add_group_membership,
    append_opportunity_snapshot,
    attach_evidence,
    canonical_thesis_key,
    create_find_group,
    evaluate_thesis,
    freeze_forecast,
    link_position,
    list_ranked_opportunities,
    list_thesis_groups,
    load_group_tournament,
    load_thesis_detail,
    merge_or_create_thesis,
    record_falsification_run,
    record_forecast_outcome,
    remove_group_membership,
    thesis_desk_status,
    unlink_position,
    update_falsification_run,
    upsert_scenario,
)
from thesis_scoring import (  # noqa: E402
    CatalystSignal,
    assess_evidence,
    assess_opportunity,
    calculate_neglect,
    catalyst_readiness,
    scenario_valuation,
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


class CanonicalKeyTests(unittest.TestCase):
    def test_key_is_deterministic_and_sensitive_to_identity(self):
        key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        same = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="nvidia corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        self.assertEqual(key, same)
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="short",
                horizon="multi_year",
                mechanism="AI capex compounds.",
            ),
        )
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="long",
                horizon="multi_year",
                mechanism="Valuation is stretched.",
            ),
        )
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="long",
                horizon="months",
                mechanism="AI capex compounds.",
            ),
        )
        self.assertNotEqual(
            key,
            canonical_thesis_key(
                theme_id=str(uuid4()),
                subject="Nvidia Corp",
                direction="long",
                horizon="multi_year",
                mechanism="AI capex compounds.",
            ),
        )

    def test_invalid_direction_raises(self):
        with self.assertRaisesRegex(ValueError, "unsupported direction"):
            canonical_thesis_key(
                theme_id=str(THEME_ID),
                subject="Nvidia Corp",
                direction="sideways",
                horizon="multi_year",
                mechanism="x",
            )


class GroupHelperTests(unittest.TestCase):
    def test_create_find_group_creates_then_finds(self):
        session = Session([Result(first={"id": GROUP_ID})])  # INSERT RETURNING id
        created = create_find_group(
            session, name="NVDA bull vs bear", description="Tournament."
        )
        self.assertEqual(created, {"id": str(GROUP_ID), "created": True})
        self.assertEqual(len(session.calls), 1)
        insert_sql = session.calls[0][0]
        self.assertIn("INSERT INTO investment_thesis_groups", insert_sql)
        self.assertIn("ON CONFLICT (name) DO NOTHING", insert_sql)
        self.assertIn("RETURNING id", insert_sql)
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first=None),  # INSERT conflict, no row returned
                Result(first={"id": GROUP_ID}),  # SELECT fallback
            ]
        )
        found = create_find_group(session, name="NVDA bull vs bear")
        self.assertEqual(found, {"id": str(GROUP_ID), "created": False})
        self.assertEqual(len(session.calls), 2)
        self.assertIn("INSERT INTO investment_thesis_groups", session.calls[0][0])
        self.assertIn("FROM investment_thesis_groups", session.calls[1][0])
        session.commit.assert_not_called()

    def test_concurrent_same_name_creation_is_idempotent(self):
        # Lost the insert race to a committed winner: SELECT returns the
        # winner's row and the group is never duplicated.
        session = Session(
            [
                Result(first=None),  # INSERT conflict, no row returned
                Result(first={"id": GROUP_ID}),  # winner's row
            ]
        )
        result = create_find_group(session, name="NVDA bull vs bear")
        self.assertEqual(result, {"id": str(GROUP_ID), "created": False})
        self.assertEqual(len(session.calls), 2)
        session.commit.assert_not_called()

        # Lost the race to a winner that rolled back: the retry INSERT wins,
        # so the caller still gets a group.
        session = Session(
            [
                Result(first=None),  # INSERT conflict, no row returned
                Result(first=None),  # winner rolled back: SELECT misses
                Result(first={"id": GROUP_ID}),  # retry INSERT wins
            ]
        )
        result = create_find_group(session, name="NVDA bull vs bear")
        self.assertEqual(result, {"id": str(GROUP_ID), "created": True})
        self.assertEqual(len(session.calls), 3)
        self.assertIn("ON CONFLICT (name) DO NOTHING", session.calls[2][0])
        session.commit.assert_not_called()

    def test_unsupported_status_rejected_pre_insert(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported group status"):
            create_find_group(session, name="x", status="deleted")
        self.assertEqual(session.calls, [])


class MergeCreateThesisTests(unittest.TestCase):
    def test_create_writes_canonical_key_and_initial_version(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(first=None),  # input fingerprint lookup
                Result(first=None),  # canonical key lookup
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            rationale="Founding rationale.",
            input_fingerprint="f" * 64,
        )
        self.assertEqual(result["id"], str(THESIS_ID))
        self.assertTrue(result["created"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["canonical_key"], key)
        insert_params = session.calls[3][1]
        self.assertEqual(insert_params["canonical_key"], key)
        self.assertEqual(insert_params["direction"], "long")
        self.assertEqual(insert_params["origin"], "generated")
        self.assertEqual(insert_params["input_fingerprint"], "f" * 64)
        version_params = session.calls[5][1]
        self.assertEqual(version_params["version"], 1)
        self.assertEqual(version_params["changed_by"], "fusion")
        session.commit.assert_not_called()

    def test_merge_appends_version_and_preserves_status(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # canonical key lookup
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                    }
                ),
                Result(first={"max_version": 2}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            rationale="New filing evidence.",
        )
        self.assertEqual(result["id"], str(THESIS_ID))
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["version"], 3)
        version_params = session.calls[3][1]
        self.assertEqual(version_params["version"], 3)
        self.assertEqual(version_params["changed_by"], "fusion")
        update_sql = session.calls[4][0]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertNotIn("status", update_sql)
        session.commit.assert_not_called()

    def test_identical_candidate_is_idempotent_noop(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # canonical key lookup, identical content
                    first={
                        "id": THESIS_ID,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                    }
                ),
                Result(first={"max_version": 4}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.6,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["version"], 4)
        self.assertEqual(len(session.calls), 3)
        session.commit.assert_not_called()

    def test_bull_and_bear_are_distinct_theses(self):
        bull_key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        bear_key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="short",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        self.assertNotEqual(bull_key, bear_key)
        bull = merge_or_create_thesis(
            Session(
                [
                    Result(first={"present": 1}),
                    Result(first=None),  # canonical key lookup
                    Result(first={"id": THESIS_ID}),
                    Result(first={"max_version": 0}),
                    Result(),
                ]
            ),
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
        )
        bear = merge_or_create_thesis(
            Session(
                [
                    Result(first={"present": 1}),
                    Result(first=None),  # canonical key lookup
                    Result(first={"id": BEAR_THESIS_ID}),
                    Result(first={"max_version": 0}),
                    Result(),
                ]
            ),
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex peaks.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="short",
        )
        self.assertTrue(bull["created"])
        self.assertTrue(bear["created"])
        self.assertNotEqual(bull["id"], bear["id"])

    def test_input_fingerprint_merges_matching_candidate(self):
        key = canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # fingerprint lookup hits the same thesis
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": key,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["id"], str(THESIS_ID))

    def test_input_fingerprint_conflict_raises(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # fingerprint lookup with a different identity
                    first={
                        "id": THESIS_ID,
                        "claim": "x",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": "d" * 64,
                    }
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "input_fingerprint conflicts"):
            merge_or_create_thesis(
                session,
                theme_id=str(THEME_ID),
                company="Nvidia Corp",
                claim="AI capex compounds.",
                horizon="multi_year",
                mechanism="AI capex compounds.",
                direction="long",
                input_fingerprint="f" * 64,
            )
        self.assertEqual(len(session.calls), 2)

    def test_unknown_theme_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown theme"):
            merge_or_create_thesis(
                session,
                theme_id=str(THEME_ID),
                company="Nvidia Corp",
                claim="AI capex compounds.",
            )
        session.commit.assert_not_called()


class FusionReferenceClaimTests(unittest.TestCase):
    """Accepted-reference guard (migration 055) on merge claims."""

    REFERENCE = NOW
    OLDER = NOW - timedelta(days=1)
    NEWER = NOW + timedelta(days=1)

    def _key(self) -> str:
        return canonical_thesis_key(
            theme_id=str(THEME_ID),
            subject="Nvidia Corp",
            direction="long",
            horizon="multi_year",
            mechanism="AI capex compounds.",
        )

    def test_new_thesis_persists_the_accepted_reference_at_creation(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # input fingerprint lookup
                Result(first=None),  # canonical key lookup
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["created"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 1)
        insert_sql, insert_params = session.calls[4]
        self.assertEqual(insert_params["fusion_reference_at"], self.REFERENCE)
        self.assertIn("fusion_reference_at", insert_sql)
        # The accepted candidate fingerprint is persisted atomically with
        # the reference at creation.
        self.assertEqual(insert_params["fusion_candidate_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", insert_sql)
        session.commit.assert_not_called()

    def test_manual_create_keeps_the_reference_null(self):
        # No input_fingerprint: the fingerprint lookup never runs, so the
        # scripted queue starts at the canonical-key lookup.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(first=None),  # canonical key lookup
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
        )
        self.assertTrue(result["created"])
        self.assertFalse(result["stale"])
        insert_sql, insert_params = session.calls[2]
        self.assertIn("INSERT INTO investment_theses", insert_sql)
        self.assertIsNone(insert_params["fusion_reference_at"])
        # The fingerprint pair is meaningful only under an accepted
        # reference: manual creations persist no candidate fingerprint.
        self.assertIsNone(insert_params["fusion_candidate_fingerprint"])
        # Manual merges never acquire the canonical-key advisory lock.
        self.assertFalse(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )
        session.commit.assert_not_called()

    def test_autonomous_merge_claims_when_reference_is_not_stale(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, older accepted reference
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.OLDER,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 2}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 3)
        lookup_sql = session.calls[3][0]
        self.assertIn("FOR UPDATE", lookup_sql)
        self.assertIn("fusion_reference_at", lookup_sql)
        self.assertIn("fusion_candidate_fingerprint", lookup_sql)
        update_sql, update_params = session.calls[6]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertIn("fusion_reference_at", update_sql)
        self.assertEqual(update_params["accepted_reference"], self.REFERENCE)
        # The claim atomically persists the proven candidate fingerprint.
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        self.assertNotIn("status", update_sql)
        session.commit.assert_not_called()

    def test_stale_reference_is_an_explicit_noop_without_any_write(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, newer accepted reference
                    first={
                        "id": THESIS_ID,
                        "claim": "Newer claim.",
                        "variant_perception": None,
                        "confidence": 0.8,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.NEWER,
                        "fusion_candidate_fingerprint": "n" * 64,
                    }
                ),
                Result(first={"max_version": 4}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="Older claim.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="o" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], 4)
        self.assertEqual(result["id"], str(THESIS_ID))
        # No version append and no current-field mutation happened: the
        # only statements were the theme check, the advisory lock, the
        # fingerprint lookup, the canonical lookup, and the version probe
        # (the row-lock keyword "FOR UPDATE" is a SELECT, not an UPDATE).
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_null_stored_reference_is_claimable_by_any_cycle(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, legacy row without a guard
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": None,
                        "fusion_candidate_fingerprint": None,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["stale"])
        self.assertTrue(result["changed"])
        update_sql, update_params = session.calls[6]
        self.assertEqual(update_params["accepted_reference"], self.REFERENCE)
        # The claim persists the pair: reference plus proven fingerprint.
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        session.commit.assert_not_called()

    def test_unchanged_autonomous_merge_still_advances_the_guard(self):
        # The cycle accepted the thesis at the incoming reference even
        # though the claim content is unchanged; the guard must advance so
        # an older cycle finishing later cannot write after it.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, identical content
                    first={
                        "id": THESIS_ID,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.OLDER,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(),  # monotonic claim UPDATE (guard pair only)
                Result(first={"max_version": 3}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.6,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 3)
        claim_sql, claim_params = session.calls[4]
        self.assertIn("UPDATE investment_theses", claim_sql)
        self.assertIn("fusion_reference_at", claim_sql)
        self.assertNotIn("claim =", claim_sql)
        self.assertEqual(claim_params["accepted_reference"], self.REFERENCE)
        # The guard-only claim persists the proven fingerprint too.
        self.assertEqual(claim_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", claim_sql)
        session.commit.assert_not_called()

    def test_manual_merge_never_touches_the_reference_guard(self):
        # Non-autonomy callers keep the pre-055 behavior: they merge past
        # any guard and never write or reject on it.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(  # canonical key lookup, guarded thesis
                    first={
                        "id": THESIS_ID,
                        "claim": "Old claim.",
                        "variant_perception": None,
                        "confidence": None,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
        )
        self.assertFalse(result["stale"])
        self.assertTrue(result["changed"])
        update_sql, update_params = session.calls[4]
        self.assertNotIn("fusion_reference_at", update_sql)
        self.assertNotIn("accepted_reference", update_params)
        # Manual merges never erase the guard pair either: the fingerprint
        # column is absent from the write, so an existing claim stays
        # proven.
        self.assertNotIn("fusion_candidate_fingerprint", update_sql)
        self.assertNotIn("accepted_fingerprint", update_params)
        # Manual merges never acquire the canonical-key advisory lock.
        self.assertFalse(
            any("pg_advisory_xact_lock" in sql for sql, _params in session.calls)
        )
        session.commit.assert_not_called()

    def test_identical_fingerprint_rerun_at_the_same_reference_is_resumable(self):
        # An identical-input rerun (same fingerprint) at the same cycle
        # reference proves it is the same accepted output, so it stays a
        # valid (non-stale) resumable merge: child-state writes may safely
        # be retried, and the claim UPDATE is a no-op (identical reference).
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(  # fingerprint lookup hits the same thesis
                    first={
                        "id": THESIS_ID,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": "f" * 64,
                    }
                ),
                Result(),  # monotonic claim UPDATE (no-op pair write)
                Result(first={"max_version": 1}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.6,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["stale"])
        lookup_sql = session.calls[2][0]
        self.assertIn("FOR UPDATE", lookup_sql)
        claim_sql, claim_params = session.calls[3]
        self.assertEqual(claim_params["accepted_reference"], self.REFERENCE)
        # The resume re-asserts the identical proven fingerprint; the
        # IS DISTINCT FROM WHERE makes the equal-reference write a no-op.
        self.assertEqual(claim_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", claim_sql)
        self.assertIn("IS DISTINCT FROM", claim_sql)
        session.commit.assert_not_called()

    def test_accepted_reference_requires_a_nonblank_fingerprint(self):
        # An accepted-reference claim must prove WHICH candidate it is:
        # without a fingerprint, distinct model outputs could claim the
        # same reference interchangeably.  The merge fails before any
        # advisory lock, lookup, or write.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
            ]
        )
        with self.assertRaisesRegex(
            ValueError, "accepted_reference requires a nonblank candidate fingerprint"
        ):
            merge_or_create_thesis(
                session,
                theme_id=str(THEME_ID),
                company="Nvidia Corp",
                claim="AI capex compounds.",
                horizon="multi_year",
                mechanism="AI capex compounds.",
                direction="long",
                accepted_reference=self.REFERENCE,
            )
        # Only the theme probe ran: no advisory lock and no write.
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("pg_advisory_xact_lock", session.calls[0][0])
        self.assertFalse(
            session.calls[0][0].lstrip().upper().startswith(("INSERT", "UPDATE"))
        )
        session.commit.assert_not_called()

    def test_equal_reference_with_different_fingerprint_is_stale_noop(self):
        # The same accepted reference is already proven to a different
        # candidate output: only the first proven fingerprint is
        # authoritative at a reference, so this merge is a complete no-op
        # regardless of content -- the caller skips every child-state write.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, same reference, other output
                    first={
                        "id": THESIS_ID,
                        "claim": "Other output claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 3}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["version"], 3)
        self.assertEqual(result["id"], str(THESIS_ID))
        # No version append and no current-field mutation: the only
        # statements were the theme check, the advisory lock, the
        # fingerprint lookup, the canonical lookup, and the version probe.
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_equal_reference_with_unprovable_stored_fingerprint_is_stale_noop(self):
        # A legacy/NULL stored fingerprint cannot prove the incoming
        # candidate is the one that was accepted at the reference, so the
        # equal-reference claim fails closed: only a strictly newer
        # reference may claim the thesis.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, same reference, NULL proof
                    first={
                        "id": THESIS_ID,
                        "claim": "Legacy claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": None,
                    }
                ),
                Result(first={"max_version": 2}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["stale"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["version"], 2)
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_newer_reference_wins_even_with_a_different_fingerprint(self):
        # A strictly newer accepted reference claims and persists both
        # fields, even when the newer output's fingerprint differs from the
        # older stored one: reference order, not fingerprint equality,
        # decides across references.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, older reference other output
                    first={
                        "id": THESIS_ID,
                        "claim": "Older output claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.OLDER,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 2}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertFalse(result["created"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["version"], 3)
        update_sql, update_params = session.calls[6]
        self.assertEqual(update_params["accepted_reference"], self.REFERENCE)
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        session.commit.assert_not_called()

    def test_newer_reference_claims_legacy_row_with_null_fingerprint(self):
        # A legacy row whose reference was backfilled but whose fingerprint
        # is NULL is claimable by a strictly newer cycle: the newer cycle
        # stores the pair (reference + its own proven fingerprint).
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no match)
                Result(  # canonical key lookup, backfilled guard, NULL proof
                    first={
                        "id": THESIS_ID,
                        "claim": "Legacy claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": None,
                    }
                ),
                Result(first={"max_version": 1}),  # version max
                Result(),  # version INSERT
                Result(),  # UPDATE theses row with the claim
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.NEWER,
        )
        self.assertFalse(result["stale"])
        self.assertTrue(result["changed"])
        update_sql, update_params = session.calls[6]
        self.assertEqual(update_params["accepted_reference"], self.NEWER)
        self.assertEqual(update_params["accepted_fingerprint"], "f" * 64)
        self.assertIn("fusion_candidate_fingerprint", update_sql)
        session.commit.assert_not_called()

    def test_create_race_at_the_same_reference_keeps_only_the_first_fingerprint(self):
        # Two concurrent cycles at the same reference with different model
        # outputs: the advisory lock serializes the both-see-no-thesis
        # create race, the loser's lookups find the winner's row, and the
        # equal-reference different-fingerprint guard makes the loser a
        # stale no-op -- completion order cannot choose between outputs.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup: winner unknown yet
                Result(  # canonical key lookup: winner's row now committed
                    first={
                        "id": THESIS_ID,
                        "claim": "First output claim.",
                        "variant_perception": None,
                        "confidence": 0.6,
                        "status": "active",
                        "canonical_key": self._key(),
                        "fusion_reference_at": self.REFERENCE,
                        "fusion_candidate_fingerprint": "a" * 64,
                    }
                ),
                Result(first={"max_version": 1}),  # current version probe
            ]
        )
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="Second output claim.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            confidence=0.2,
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["stale"])
        self.assertFalse(result["created"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["id"], str(THESIS_ID))
        # The loser never inserts a competing canonical_key row and never
        # writes any claim or child state.
        self.assertEqual(len(session.calls), 5)
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        session.commit.assert_not_called()

    def test_autonomous_merge_locks_the_canonical_key_before_lookups(self):
        # The advisory lock closes the both-see-no-thesis create race: two
        # concurrent autonomous cycles for the same canonical identity
        # serialize here, so the loser waits and then finds (and claims or
        # rejects) the winner's row instead of failing on the unique index.
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # canonical-key advisory lock
                Result(first=None),  # fingerprint lookup (no thesis yet)
                Result(first=None),  # canonical key lookup (no thesis yet)
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(first={"max_version": 0}),  # version max
                Result(),  # version INSERT
            ]
        )
        key = self._key()
        result = merge_or_create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            horizon="multi_year",
            mechanism="AI capex compounds.",
            direction="long",
            input_fingerprint="f" * 64,
            accepted_reference=self.REFERENCE,
        )
        self.assertTrue(result["created"])
        lock_sql, lock_params = session.calls[1]
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        self.assertIn("hashtextextended", lock_sql)
        self.assertEqual(lock_params, {"key": key})
        # The lock precedes both thesis lookups: the fingerprint lookup is
        # the next statement.
        self.assertIn("FROM investment_theses", session.calls[2][0])
        self.assertIn("WHERE input_fingerprint", session.calls[2][0])
        session.commit.assert_not_called()


class GroupMembershipTests(unittest.TestCase):
    def test_add_membership_is_idempotent(self):
        session = Session(
            [
                Result(first={"present": 1}),  # group exists
                Result(first={"present": 1}),  # thesis exists
                Result(first={"id": THESIS_ID}),  # thesis row locked
                Result(first=None),  # no active membership in this group
                Result(first=None),  # no membership in another group
                Result(),  # INSERT ON CONFLICT DO NOTHING
                Result(),  # UPDATE investment_theses SET group_id
            ]
        )
        added = add_group_membership(
            session, str(GROUP_ID), str(THESIS_ID), note="Bull leg."
        )
        self.assertTrue(added)
        lock_sql = session.calls[2][0]
        self.assertIn("FOR UPDATE", lock_sql)
        insert_sql = session.calls[5][0]
        self.assertIn("INSERT INTO investment_thesis_group_members", insert_sql)
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)
        update_sql = session.calls[6][0]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertIn("SET group_id = CAST(:group_id AS UUID)", update_sql)
        self.assertEqual(
            session.calls[6][1],
            {"group_id": str(GROUP_ID), "thesis_id": str(THESIS_ID)},
        )
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"present": 1}),
                Result(first={"id": THESIS_ID}),
                Result(first={"present": 1}),  # already active
            ]
        )
        self.assertFalse(add_group_membership(session, str(GROUP_ID), str(THESIS_ID)))
        self.assertEqual(len(session.calls), 4)

    def test_add_membership_rejects_other_active_group(self):
        # One active group per thesis: a thesis already member of another
        # group is rejected before any write, so the snapshot never diverges.
        session = Session(
            [
                Result(first={"present": 1}),  # group exists
                Result(first={"present": 1}),  # thesis exists
                Result(first={"id": THESIS_ID}),  # thesis row locked
                Result(first=None),  # no active membership in this group
                Result(first={"present": 1}),  # active in ANOTHER group
            ]
        )
        with self.assertRaisesRegex(
            ValueError, "thesis already belongs to another group"
        ):
            add_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        self.assertEqual(len(session.calls), 5)
        session.commit.assert_not_called()

    def test_remove_membership_clears_group_snapshot(self):
        session = Session(
            [
                Result(first={"id": LINK_ID}),  # active membership found
                Result(),  # UPDATE membership SET removed_at
                Result(),  # UPDATE investment_theses SET group_id = NULL
            ]
        )
        removed = remove_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        self.assertTrue(removed)
        membership_sql = session.calls[1][0]
        self.assertIn("UPDATE investment_thesis_group_members", membership_sql)
        self.assertIn("removed_at = NOW()", membership_sql)
        self.assertIn("removed_at IS NULL", membership_sql)
        snapshot_sql = session.calls[2][0]
        self.assertIn("UPDATE investment_theses", snapshot_sql)
        self.assertIn("SET group_id = NULL", snapshot_sql)
        self.assertIn("AND group_id = CAST(:group_id AS UUID)", snapshot_sql)
        self.assertEqual(
            session.calls[2][1],
            {"thesis_id": str(THESIS_ID), "group_id": str(GROUP_ID)},
        )
        session.commit.assert_not_called()

    def test_remove_membership_without_active_row_returns_false(self):
        session = Session([Result(first=None)])
        self.assertFalse(
            remove_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        )
        session.commit.assert_not_called()

    def test_unknown_group_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown group"):
            add_group_membership(session, str(GROUP_ID), str(THESIS_ID))
        session.commit.assert_not_called()


class EvidenceAttachmentTests(unittest.TestCase):
    def test_attach_evidence_persists_provenance_and_is_idempotent(self):
        first = desk_evidence_item()
        second = desk_evidence_item(
            evidence_id="claim:capex-2026-peer",
            origin_key="peer:note:nvda:2026",
            independence_key="peers:nvda",
            content={"statement": "Peer confirms capex ramp."},
        )
        signal_first = EvidenceSignal.create(
            evidence_id=first["evidence_id"],
            evidence_type=first["evidence_type"],
            relationship=first["relationship"],
            source_name=first["source_name"],
            source_family=first["source_family"],
            origin_key=first["origin_key"],
            independence_key=first["independence_key"],
            content=first["content"],
            source_timestamp=first["source_timestamp"],
            quality_score=first["quality_score"],
            entailment_score=first["entailment_score"],
            freshness_score=first["freshness_score"],
            effective_weight=first["effective_weight"],
        )
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no prior evidence keys
                Result(),  # INSERT ... ON CONFLICT DO NOTHING
                Result(),  # UPDATE last_evidence_at
            ]
        )
        outcome = attach_evidence(session, str(THESIS_ID), evidence=[first, second])
        self.assertEqual(
            outcome,
            {
                "attached": 2,
                "skipped_duplicate_fingerprint": 0,
                "skipped_correlated": 0,
            },
        )
        insert_sql = session.calls[2][0]
        self.assertIn("INSERT INTO investment_thesis_evidence", insert_sql)
        self.assertIn("source_family", insert_sql)
        self.assertIn("origin_key", insert_sql)
        self.assertIn("independence_key", insert_sql)
        self.assertIn("evidence_fingerprint", insert_sql)
        self.assertIn("effective_weight", insert_sql)
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)
        inserted_rows = session.calls[2][1]
        self.assertEqual(len(inserted_rows), 2)
        self.assertEqual(
            inserted_rows[0]["evidence_fingerprint"],
            signal_first.evidence_fingerprint,
        )
        self.assertEqual(inserted_rows[0]["source_family"], "filings")
        update_sql = session.calls[3][0]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertIn("last_evidence_at", update_sql)
        session.commit.assert_not_called()

        # Re-attaching the identical rows is a no-op: fingerprint dedupe.
        signal_second = EvidenceSignal.create(
            evidence_id=second["evidence_id"],
            evidence_type=second["evidence_type"],
            relationship=second["relationship"],
            source_name=second["source_name"],
            source_family=second["source_family"],
            origin_key=second["origin_key"],
            independence_key=second["independence_key"],
            content=second["content"],
            source_timestamp=second["source_timestamp"],
            quality_score=second["quality_score"],
            entailment_score=second["entailment_score"],
            freshness_score=second["freshness_score"],
            effective_weight=second["effective_weight"],
        )
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    rows=[
                        {
                            "evidence_fingerprint": signal_first.evidence_fingerprint,
                            "independence_key": "filings:nvda",
                        },
                        {
                            "evidence_fingerprint": signal_second.evidence_fingerprint,
                            "independence_key": "peers:nvda",
                        },
                    ]
                ),
            ]
        )
        outcome = attach_evidence(session, str(THESIS_ID), evidence=[first, second])
        self.assertEqual(
            outcome,
            {
                "attached": 0,
                "skipped_duplicate_fingerprint": 2,
                "skipped_correlated": 0,
            },
        )
        self.assertEqual(len(session.calls), 2)
        session.commit.assert_not_called()

    def test_invalidation_evidence_attaches_idempotently(self):
        invalidating = desk_evidence_item(
            evidence_id="claim:capex-invalidated",
            relationship="invalidation",
            origin_key="sec:10q:nvda:2027q1",
            independence_key="filings:nvda",
            content={"statement": "Capex guide cut."},
        )
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no prior evidence keys
                Result(),  # INSERT ... ON CONFLICT DO NOTHING
                Result(),  # UPDATE last_evidence_at
            ]
        )
        outcome = attach_evidence(session, str(THESIS_ID), evidence=[invalidating])
        self.assertEqual(
            outcome,
            {
                "attached": 1,
                "skipped_duplicate_fingerprint": 0,
                "skipped_correlated": 0,
            },
        )
        inserted_rows = session.calls[2][1]
        self.assertEqual(inserted_rows[0]["relationship"], "invalidation")
        session.commit.assert_not_called()

        # Re-attaching the same invalidation row is a fingerprint no-op.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    rows=[
                        {
                            "evidence_fingerprint": EvidenceSignal.create(
                                evidence_id=invalidating["evidence_id"],
                                evidence_type=invalidating["evidence_type"],
                                relationship=invalidating["relationship"],
                                source_name=invalidating["source_name"],
                                source_family=invalidating["source_family"],
                                origin_key=invalidating["origin_key"],
                                independence_key=invalidating["independence_key"],
                                content=invalidating["content"],
                                source_timestamp=invalidating["source_timestamp"],
                            ).evidence_fingerprint,
                            "independence_key": "filings:nvda",
                        }
                    ]
                ),
            ]
        )
        outcome = attach_evidence(session, str(THESIS_ID), evidence=[invalidating])
        self.assertEqual(
            outcome,
            {
                "attached": 0,
                "skipped_duplicate_fingerprint": 1,
                "skipped_correlated": 0,
            },
        )
        self.assertEqual(len(session.calls), 2)
        session.commit.assert_not_called()

    def test_correlated_source_with_same_independence_key_is_skipped(self):
        held = desk_evidence_item()
        signal_held = EvidenceSignal.create(
            evidence_id=held["evidence_id"],
            evidence_type=held["evidence_type"],
            relationship=held["relationship"],
            source_name=held["source_name"],
            source_family=held["source_family"],
            origin_key=held["origin_key"],
            independence_key=held["independence_key"],
            content=held["content"],
            source_timestamp=held["source_timestamp"],
            quality_score=held["quality_score"],
            entailment_score=held["entailment_score"],
            freshness_score=held["freshness_score"],
            effective_weight=held["effective_weight"],
        )
        # New content, same independence_key: correlated duplicate.
        correlated = desk_evidence_item(
            evidence_id="claim:capex-revised",
            origin_key="sec:10q:nvda:2026q2-rev",
            content={"statement": "Same source, revised language."},
        )
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    rows=[
                        {
                            "evidence_fingerprint": signal_held.evidence_fingerprint,
                            "independence_key": "filings:nvda",
                        }
                    ]
                ),
            ]
        )
        outcome = attach_evidence(session, str(THESIS_ID), evidence=[correlated])
        self.assertEqual(
            outcome,
            {
                "attached": 0,
                "skipped_duplicate_fingerprint": 0,
                "skipped_correlated": 1,
            },
        )
        self.assertEqual(len(session.calls), 2)
        session.commit.assert_not_called()

    def test_in_batch_correlated_duplicates_attach_once(self):
        first = desk_evidence_item(evidence_id="claim:a", content={"n": 1})
        second = desk_evidence_item(
            evidence_id="claim:b",
            origin_key="sec:10q:nvda:2026q2-b",
            content={"n": 2},
        )
        session = Session(
            [
                Result(first={"present": 1}),
                Result(rows=[]),
                Result(),
                Result(),
            ]
        )
        outcome = attach_evidence(session, str(THESIS_ID), evidence=[first, second])
        self.assertEqual(outcome["attached"], 1)
        self.assertEqual(outcome["skipped_correlated"], 1)

    def test_attach_requires_source_family_and_content(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "source_family is required"):
            attach_evidence(
                session,
                str(THESIS_ID),
                evidence=[
                    {
                        "evidence_id": "claim:x",
                        "content": {"statement": "x"},
                        "source_timestamp": NOW.isoformat(),
                    }
                ],
            )
        self.assertEqual(session.calls, [])
        with self.assertRaisesRegex(ValueError, "invalid evidence row"):
            attach_evidence(
                session,
                str(THESIS_ID),
                evidence=[
                    {
                        "evidence_id": "claim:x",
                        "source_family": "filings",
                        "source_timestamp": NOW.isoformat(),
                    }
                ],
            )
        self.assertEqual(session.calls, [])


class ScenarioTests(unittest.TestCase):
    def test_upsert_scenario_creates_then_versions_on_change(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active scenario
                Result(first={"id": SCENARIO_ID}),  # INSERT RETURNING id
            ]
        )
        created = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Base",
            description="Capex holds.",
            probability=0.6,
            is_base_case=False,
        )
        self.assertEqual(
            created, {"id": str(SCENARIO_ID), "version": 1, "changed": True}
        )
        insert_params = session.calls[2][1]
        self.assertEqual(insert_params["version"], 1)
        self.assertEqual(insert_params["probability"], 0.6)
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(  # active scenario with old probability
                    first={
                        "id": SCENARIO_ID,
                        "version": 1,
                        "description": "Capex holds.",
                        "probability": 0.6,
                        "expected_return": 0.0,
                        "is_base_case": True,
                    }
                ),
                Result(),  # supersede old version
                Result(first={"id": uuid4()}),  # INSERT RETURNING id
            ]
        )
        revised = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Base",
            description="Capex holds.",
            probability=0.8,
        )
        self.assertEqual(revised["version"], 2)
        self.assertTrue(revised["changed"])
        supersede_sql = session.calls[2][0]
        self.assertIn("UPDATE investment_thesis_scenarios", supersede_sql)
        self.assertIn("superseded_at = NOW()", supersede_sql)
        self.assertEqual(session.calls[3][1]["version"], 2)
        session.commit.assert_not_called()

    def test_upsert_scenario_identical_revision_is_noop(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": SCENARIO_ID,
                        "version": 2,
                        "description": "Capex holds.",
                        "probability": 0.8,
                        "expected_return": 0.0,
                        "is_base_case": False,
                    }
                ),
            ]
        )
        result = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Base",
            description="Capex holds.",
            probability=0.8,
        )
        self.assertEqual(
            result, {"id": str(SCENARIO_ID), "version": 2, "changed": False}
        )
        self.assertEqual(len(session.calls), 2)

    def test_promoting_base_case_supersedes_previous_with_successor(self):
        base_id = uuid4()
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active scenario under this name
                Result(  # lock current active base case
                    first={
                        "id": base_id,
                        "name": "Base",
                        "version": 3,
                        "description": "Capex holds.",
                        "probability": 0.6,
                        "expected_return": 0.0,
                    }
                ),
                Result(),  # supersede old base row
                Result(first={"id": uuid4()}),  # successor INSERT RETURNING id
                Result(first={"id": uuid4()}),  # requested INSERT RETURNING id
            ]
        )
        result = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Upside",
            description="Upside risk.",
            probability=0.3,
            is_base_case=True,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["version"], 1)

        # Old base is superseded via superseded_at, never rewritten in place.
        base_lock_sql = session.calls[2][0]
        self.assertIn("is_base_case AND superseded_at IS NULL", base_lock_sql)
        self.assertIn("FOR UPDATE", base_lock_sql)
        supersede_sql = session.calls[3][0]
        self.assertIn("UPDATE investment_thesis_scenarios", supersede_sql)
        self.assertIn("SET superseded_at = NOW()", supersede_sql)
        self.assertEqual(session.calls[3][1]["id"], str(base_id))

        # Immutable non-base successor carries the old base's content.
        successor_params = session.calls[4][1]
        self.assertEqual(successor_params["name"], "Base")
        self.assertEqual(successor_params["description"], "Capex holds.")
        self.assertEqual(successor_params["probability"], 0.6)
        self.assertEqual(successor_params["expected_return"], 0.0)
        self.assertFalse(successor_params["is_base_case"])
        self.assertEqual(successor_params["version"], 4)  # base v3 + 1

        # Requested scenario is inserted as the new active base (version 1).
        promoted_params = session.calls[5][1]
        self.assertEqual(promoted_params["name"], "Upside")
        self.assertEqual(promoted_params["probability"], 0.3)
        self.assertTrue(promoted_params["is_base_case"])
        self.assertEqual(promoted_params["version"], 1)

        # No scenario content is ever mutated in place.
        self.assertFalse(any("SET is_base_case" in sql for sql, _ in session.calls))
        session.commit.assert_not_called()

    def test_promoting_different_base_versions_promoted_scenario(self):
        base_id = uuid4()
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(  # active non-base version of the promoted scenario
                    first={
                        "id": SCENARIO_ID,
                        "version": 2,
                        "description": "Upside risk.",
                        "probability": 0.3,
                        "expected_return": 0.0,
                        "is_base_case": False,
                    }
                ),
                Result(  # lock current active base case
                    first={
                        "id": base_id,
                        "name": "Base",
                        "version": 1,
                        "description": "Capex holds.",
                        "probability": 0.6,
                        "expected_return": 0.0,
                    }
                ),
                Result(),  # supersede old base row
                Result(first={"id": uuid4()}),  # successor INSERT RETURNING id
                Result(),  # supersede old version of promoted scenario
                Result(first={"id": uuid4()}),  # promoted INSERT RETURNING id
            ]
        )
        result = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Upside",
            description="Upside risk.",
            probability=0.3,
            is_base_case=True,
        )
        self.assertEqual(result["version"], 3)
        self.assertTrue(result["changed"])

        # Old base chain: superseded v1 followed by identical non-base v2.
        successor_params = session.calls[4][1]
        self.assertEqual(successor_params["name"], "Base")
        self.assertEqual(successor_params["version"], 2)
        self.assertEqual(successor_params["description"], "Capex holds.")
        self.assertEqual(successor_params["probability"], 0.6)
        self.assertEqual(successor_params["expected_return"], 0.0)
        self.assertFalse(successor_params["is_base_case"])

        # Promoted scenario chain: superseded v2 followed by base v3.
        supersede_sql = session.calls[5][0]
        self.assertIn("SET superseded_at = NOW()", supersede_sql)
        self.assertEqual(session.calls[5][1]["id"], str(SCENARIO_ID))
        promoted_params = session.calls[6][1]
        self.assertEqual(promoted_params["name"], "Upside")
        self.assertEqual(promoted_params["version"], 3)
        self.assertTrue(promoted_params["is_base_case"])

        # Exactly one active base remains: only the promoted insert is base.
        self.assertEqual(
            sum(1 for _, params in session.calls if params.get("is_base_case")),
            1,
        )
        self.assertFalse(any("SET is_base_case" in sql for sql, _ in session.calls))
        session.commit.assert_not_called()

    def test_revising_active_base_keeps_single_base_without_successor(self):
        # Promoting the already-active base scenario with new content is a
        # plain revision: supersede v1 and insert base v2, no duplicate
        # non-base successor.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(  # active scenario under this name is the base itself
                    first={
                        "id": SCENARIO_ID,
                        "version": 1,
                        "description": "Capex holds.",
                        "probability": 0.6,
                        "expected_return": 0.0,
                        "is_base_case": True,
                    }
                ),
                Result(  # lock current active base case (same row)
                    first={
                        "id": SCENARIO_ID,
                        "name": "Base",
                        "version": 1,
                        "description": "Capex holds.",
                        "probability": 0.6,
                        "expected_return": 0.0,
                    }
                ),
                Result(),  # supersede old base version
                Result(first={"id": uuid4()}),  # INSERT RETURNING id
            ]
        )
        result = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Base",
            description="Capex holds.",
            probability=0.8,
            is_base_case=True,
        )
        self.assertEqual(result["version"], 2)
        self.assertTrue(result["changed"])
        self.assertEqual(session.calls[3][1]["id"], str(SCENARIO_ID))
        insert_params = session.calls[4][1]
        self.assertEqual(insert_params["version"], 2)
        self.assertEqual(insert_params["probability"], 0.8)
        self.assertTrue(insert_params["is_base_case"])
        self.assertEqual(
            sum(1 for _, params in session.calls if params.get("is_base_case")),
            1,
        )
        session.commit.assert_not_called()

    def test_probability_is_optional_and_bounded(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "invalid probability"):
            upsert_scenario(session, str(THESIS_ID), name="Base", probability=1.5)
        with self.assertRaisesRegex(ValueError, "invalid expected_return"):
            upsert_scenario(session, str(THESIS_ID), name="Base", expected_return=101.0)
        self.assertEqual(session.calls, [])

    def test_unknown_probability_and_expected_return_round_trip(self):
        # Unknown probability persists as NULL (never defaulted to
        # conviction); expected_return persists for point-in-time scoring.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active scenario
                Result(first={"id": SCENARIO_ID}),  # INSERT RETURNING id
            ]
        )
        created = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Upside",
            probability=None,
            expected_return=0.35,
        )
        self.assertEqual(
            created, {"id": str(SCENARIO_ID), "version": 1, "changed": True}
        )
        select_sql = session.calls[1][0]
        self.assertIn("expected_return", select_sql)
        insert_params = session.calls[2][1]
        self.assertIsNone(insert_params["probability"])
        self.assertEqual(insert_params["expected_return"], 0.35)
        session.commit.assert_not_called()

        # Identical revision (unknown probability, same return) is a no-op.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": SCENARIO_ID,
                        "version": 1,
                        "description": None,
                        "probability": None,
                        "expected_return": 0.35,
                        "is_base_case": False,
                    }
                ),
            ]
        )
        result = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Upside",
            probability=None,
            expected_return=0.35,
        )
        self.assertEqual(
            result, {"id": str(SCENARIO_ID), "version": 1, "changed": False}
        )
        self.assertEqual(len(session.calls), 2)

        # A pure expected_return revision versions the scenario.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(
                    first={
                        "id": SCENARIO_ID,
                        "version": 1,
                        "description": None,
                        "probability": None,
                        "expected_return": 0.35,
                        "is_base_case": False,
                    }
                ),
                Result(),  # supersede old version
                Result(first={"id": uuid4()}),  # INSERT RETURNING id
            ]
        )
        revised = upsert_scenario(
            session,
            str(THESIS_ID),
            name="Upside",
            probability=None,
            expected_return=0.5,
        )
        self.assertEqual(revised["version"], 2)
        self.assertTrue(revised["changed"])
        self.assertEqual(session.calls[3][1]["expected_return"], 0.5)
        self.assertIsNone(session.calls[3][1]["probability"])
        session.commit.assert_not_called()


class ForecastTests(unittest.TestCase):
    def test_freeze_forecast_versions_and_is_idempotent(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast
                Result(first={"id": FORECAST_ID}),  # INSERT RETURNING id
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            forecast_type="price",
            direction="up",
            target_value=250.0,
            target_date="2027-08-06",
            as_of=NOW,
        )
        self.assertEqual(
            frozen, {"id": str(FORECAST_ID), "version": 1, "changed": True}
        )
        insert_params = session.calls[2][1]
        self.assertEqual(insert_params["forecast_key"], "nvda:12m")
        self.assertEqual(insert_params["target_value"], 250.0)
        self.assertEqual(insert_params["version"], 1)
        session.commit.assert_not_called()

        # Identical re-freeze is a no-op.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(  # active forecast with identical content
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": None,
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": date(2027, 8, 6),
                    }
                ),
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            forecast_type="price",
            direction="up",
            target_value=250.0,
            target_date="2027-08-06",
            as_of=NOW,
        )
        self.assertEqual(frozen["version"], 1)
        self.assertFalse(frozen["changed"])
        self.assertEqual(len(session.calls), 2)

        # Changed target supersedes the frozen row and inserts version 2.
        session = Session(
            [
                Result(first={"present": 1}),
                Result(  # active forecast with old target
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": None,
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": date(2027, 8, 6),
                    }
                ),
                Result(),  # supersede old version
                Result(first={"id": uuid4()}),  # INSERT RETURNING id
            ]
        )
        revised = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            forecast_type="price",
            direction="up",
            target_value=300.0,
            target_date="2027-08-06",
            as_of=NOW,
        )
        self.assertEqual(revised["version"], 2)
        self.assertTrue(revised["changed"])
        supersede_sql = session.calls[2][0]
        self.assertIn("UPDATE investment_thesis_forecasts", supersede_sql)
        self.assertIn("superseded_at = NOW()", supersede_sql)
        session.commit.assert_not_called()

    def test_forecast_key_is_globally_unique_per_active_version(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(  # active forecast belongs to another thesis
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(BEAR_THESIS_ID),
                        "scenario_id": None,
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": None,
                    }
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "already in use"):
            freeze_forecast(
                session,
                str(THESIS_ID),
                forecast_key="nvda:12m",
                target_value=250.0,
            )

    def test_unsupported_forecast_type_rejected_pre_insert(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported forecast_type"):
            freeze_forecast(
                session,
                str(THESIS_ID),
                forecast_key="k",
                forecast_type="sentiment",
            )
        self.assertEqual(session.calls, [])

    def test_omitted_as_of_materializes_aware_now(self):
        # as_of is NOT NULL: the bound parameter must never be NULL.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast
                Result(first={"id": FORECAST_ID}),  # INSERT RETURNING id
            ]
        )
        freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=250.0,
        )
        frozen_at = session.calls[2][1]["as_of"]
        self.assertIsNotNone(frozen_at)
        self.assertIsInstance(frozen_at, datetime)
        self.assertIsNotNone(frozen_at.tzinfo)

    def test_concurrent_freeze_race_is_idempotent(self):
        # Two autonomy jobs freeze the same key between their active
        # lookups: one INSERT persists, the loser's INSERT is a no-op on
        # the unique indexes, the loser reports the winner's row, and
        # neither aborts its transaction.
        winner = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast (race window)
                Result(first={"id": FORECAST_ID}),  # INSERT won
            ]
        )
        frozen = freeze_forecast(
            winner,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=250.0,
            as_of=NOW,
        )
        self.assertEqual(
            frozen, {"id": str(FORECAST_ID), "version": 1, "changed": True}
        )
        self.assertIn("ON CONFLICT DO NOTHING", winner.calls[2][0])
        winner.commit.assert_not_called()

        loser = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast (race window)
                Result(first=None),  # concurrent winner: INSERT no-op
                Result(  # bounded winner lookup
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "version": 1,
                    }
                ),
            ]
        )
        frozen = freeze_forecast(
            loser,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=250.0,
            as_of=NOW,
        )
        self.assertEqual(
            frozen, {"id": str(FORECAST_ID), "version": 1, "changed": False}
        )
        self.assertIn("ON CONFLICT DO NOTHING", loser.calls[2][0])
        loser.commit.assert_not_called()

    def test_scenario_owned_by_other_key_reports_owner_without_insert(self):
        # The target scenario already carries an active forecast under a
        # different forecast_key (rerun fingerprint drift): the preflight
        # reports that owner under the loser contract and never reaches
        # the INSERT or any supersede.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(first=None),  # no active forecast for this key
                Result(  # scenario preflight: owner under another key
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "forecast_key": "nvda:12m",
                        "version": 1,
                    }
                ),
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m:alt",
            target_value=250.0,
            as_of=NOW,
            scenario_id=str(SCENARIO_ID),
        )
        self.assertEqual(
            frozen, {"id": str(FORECAST_ID), "version": 1, "changed": False}
        )
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        self.assertIn("FOR UPDATE", session.calls[2][0])
        session.commit.assert_not_called()

    def test_scenario_race_noop_still_finds_winner_via_scenario(self):
        # The preflight misses (the concurrent owner committed between the
        # preflight and the INSERT): the INSERT no-ops on the
        # active-scenario unique index, the key lookup misses (different
        # key), and the winner is located through the scenario leg.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(first=None),  # no active forecast for this key
                Result(first=None),  # preflight: scenario still free
                Result(first=None),  # concurrent winner: INSERT no-op
                Result(first=None),  # key lookup misses (winner key differs)
                Result(  # scenario-leg winner lookup
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "version": 1,
                    }
                ),
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m:alt",
            target_value=250.0,
            as_of=NOW,
            scenario_id=str(SCENARIO_ID),
        )
        self.assertEqual(
            frozen, {"id": str(FORECAST_ID), "version": 1, "changed": False}
        )
        session.commit.assert_not_called()

    def test_revision_to_owned_scenario_preserves_active_forecast(self):
        # Regression: the caller's K/A forecast is active and the call
        # revises K/A -> K/B while another key already owns scenario B.
        # The conflict must report the scenario owner without superseding
        # the caller's active row: a call reporting unchanged/conflict can
        # never silently retire the caller's old active forecast.
        owner_id = uuid4()
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario B belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(  # caller's active forecast: key K, scenario A
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": str(SCENARIO_ID),
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": None,
                    }
                ),
                Result(  # scenario preflight: J/B active under another key
                    first={
                        "id": owner_id,
                        "thesis_id": str(THESIS_ID),
                        "forecast_key": "nvda:12m:other",
                        "version": 1,
                    }
                ),
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=300.0,
            as_of=NOW,
            scenario_id=str(uuid4()),
        )
        self.assertEqual(frozen, {"id": str(owner_id), "version": 1, "changed": False})
        # The caller's active row was never superseded and no INSERT ran.
        for sql, _params in session.calls:
            self.assertFalse(sql.lstrip().upper().startswith(("INSERT", "UPDATE")))
        # The key-leg read takes the row lock that serializes revisions.
        self.assertIn("FOR UPDATE", session.calls[3][0])
        session.commit.assert_not_called()

    def test_revision_losing_scenario_race_rolls_back_supersede(self):
        # Racy variant of the conflict regression: the preflight misses
        # (scenario B still free at read time) and the successor INSERT
        # loses to a concurrent owner.  The savepoint rolls back, undoing
        # the supersede, so the key-leg lookup finds the caller's own
        # still-active row.  That own row is not reported as the winner:
        # the scenario leg locates the real owner, and the caller's old
        # K/A forecast stays active and unsuperseded.
        owner_id = uuid4()
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario B belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(  # caller's active forecast: key K, scenario A
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": str(SCENARIO_ID),
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": None,
                    }
                ),
                Result(first=None),  # preflight: scenario B still free
                Result(),  # supersede of K/A (inside savepoint)
                Result(first=None),  # concurrent owner won: INSERT no-op
                Result(  # key lookup: own K/A row restored by rollback
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "version": 1,
                    }
                ),
                Result(  # scenario-leg lookup: the real owner J/B
                    first={
                        "id": owner_id,
                        "thesis_id": str(THESIS_ID),
                        "version": 1,
                    }
                ),
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=300.0,
            as_of=NOW,
            scenario_id=str(uuid4()),
        )
        self.assertEqual(frozen, {"id": str(owner_id), "version": 1, "changed": False})
        # The supersede ran but its savepoint rolled back, so the caller's
        # previously active forecast was never retired by this conflict
        # report.
        self.assertIn("superseded_at = NOW()", session.calls[5][0])
        self.assertEqual(session.savepoint_rollbacks, 1)
        session.commit.assert_not_called()

    def test_revision_to_free_scenario_supersedes_before_insert(self):
        # Successful revision control: K/A -> K/B with scenario B free.
        # The old row is superseded first (the partial active-key index
        # allows only one unsuperseded row per key), then the successor
        # INSERT wins; the savepoint commits both together.
        successor_id = uuid4()
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario B belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(  # caller's active forecast: key K, scenario A
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": str(SCENARIO_ID),
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": None,
                    }
                ),
                Result(first=None),  # preflight: scenario B is free
                Result(),  # supersede of K/A (inside savepoint)
                Result(first={"id": successor_id}),  # INSERT K/B won
            ]
        )
        revised = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=300.0,
            as_of=NOW,
            scenario_id=str(uuid4()),
        )
        self.assertEqual(
            revised, {"id": str(successor_id), "version": 2, "changed": True}
        )
        supersede_sql = session.calls[5][0]
        self.assertIn("UPDATE investment_thesis_forecasts", supersede_sql)
        self.assertIn("superseded_at = NOW()", supersede_sql)
        self.assertEqual(session.calls[5][1]["id"], str(FORECAST_ID))
        insert_sql = session.calls[6][0]
        self.assertIn("INSERT INTO investment_thesis_forecasts", insert_sql)
        self.assertIn("ON CONFLICT DO NOTHING", insert_sql)
        self.assertEqual(session.calls[6][1]["version"], 2)
        self.assertEqual(session.savepoint_rollbacks, 0)
        session.commit.assert_not_called()

    def test_revision_without_visible_winner_surfaces_invariant(self):
        # The supersede ran and the successor INSERT lost with no visible
        # winner (even the scenario leg misses): the savepoint rollback
        # already restored the caller's active row, so the RuntimeError
        # can never persist a retired forecast — it only surfaces the
        # uniqueness invariant.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario B belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(  # caller's active forecast: key K, scenario A
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": str(SCENARIO_ID),
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": None,
                    }
                ),
                Result(first=None),  # preflight: scenario B still free
                Result(),  # supersede of K/A (inside savepoint)
                Result(first=None),  # INSERT no-op
                Result(  # key lookup: own K/A row restored by rollback
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "version": 1,
                    }
                ),
                Result(first=None),  # scenario-leg lookup misses
                Result(first=None),  # retry INSERT no-op
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "without a winner"):
            freeze_forecast(
                session,
                str(THESIS_ID),
                forecast_key="nvda:12m",
                target_value=300.0,
                as_of=NOW,
                scenario_id=str(uuid4()),
            )
        self.assertEqual(session.savepoint_rollbacks, 1)
        self.assertIn("ON CONFLICT DO NOTHING", session.calls[9][0])
        session.commit.assert_not_called()

    def test_revision_keeping_scenario_supersedes_before_insert(self):
        # Same key and same scenario revision (K/B v1 -> K/B v2): the
        # active-scenario unique index would block the successor, so the
        # supersede precedes the INSERT; the key-leg lock makes that
        # ordering safe.
        successor_id = uuid4()
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # scenario belongs to thesis
                Result(),  # scenario-row lock (serialization point)
                Result(  # caller's active forecast: key K, scenario B
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "scenario_id": str(SCENARIO_ID),
                        "version": 1,
                        "forecast_type": "price",
                        "direction": "up",
                        "target_value": 250.0,
                        "target_date": None,
                    }
                ),
                Result(  # preflight: scenario B owned by our own key
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(THESIS_ID),
                        "forecast_key": "nvda:12m",
                        "version": 1,
                    }
                ),
                Result(),  # supersede old version first
                Result(first={"id": successor_id}),  # INSERT K/B v2 won
            ]
        )
        revised = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=300.0,
            as_of=NOW,
            scenario_id=str(SCENARIO_ID),
        )
        self.assertEqual(
            revised, {"id": str(successor_id), "version": 2, "changed": True}
        )
        supersede_sql = session.calls[5][0]
        self.assertIn("UPDATE investment_thesis_forecasts", supersede_sql)
        self.assertIn("superseded_at = NOW()", supersede_sql)
        insert_sql = session.calls[6][0]
        self.assertIn("INSERT INTO investment_thesis_forecasts", insert_sql)
        self.assertEqual(session.savepoint_rollbacks, 0)
        session.commit.assert_not_called()

    def test_concurrent_freeze_lost_to_another_thesis_raises(self):
        # The no-op loser still surfaces the global-key validation: a
        # winner holding the key on a different thesis is an error, never
        # a silent steal.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast (race window)
                Result(first=None),  # concurrent winner: INSERT no-op
                Result(  # winner lookup: key owned by another thesis
                    first={
                        "id": FORECAST_ID,
                        "thesis_id": str(BEAR_THESIS_ID),
                        "version": 1,
                    }
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "already in use"):
            freeze_forecast(
                session,
                str(THESIS_ID),
                forecast_key="nvda:12m",
                target_value=250.0,
            )
        session.commit.assert_not_called()

    def test_concurrent_freeze_retries_when_winner_rolled_back(self):
        # Lost the insert race to a winner that rolled back: the lookup
        # misses and the retry INSERT wins, so the forecast is still
        # frozen exactly once.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast (race window)
                Result(first=None),  # concurrent winner: INSERT no-op
                Result(first=None),  # winner rolled back: lookup misses
                Result(first={"id": FORECAST_ID}),  # retry INSERT wins
            ]
        )
        frozen = freeze_forecast(
            session,
            str(THESIS_ID),
            forecast_key="nvda:12m",
            target_value=250.0,
            as_of=NOW,
        )
        self.assertEqual(
            frozen, {"id": str(FORECAST_ID), "version": 1, "changed": True}
        )
        self.assertIn("ON CONFLICT DO NOTHING", session.calls[4][0])
        session.commit.assert_not_called()

    def test_concurrent_freeze_without_winner_raises(self):
        # Two no-ops with no visible winner violate the uniqueness
        # invariant: the failure is surfaced instead of guessed.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no active forecast (race window)
                Result(first=None),  # concurrent winner: INSERT no-op
                Result(first=None),  # winner lookup misses
                Result(first=None),  # retry INSERT no-op
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "without a winner"):
            freeze_forecast(
                session,
                str(THESIS_ID),
                forecast_key="nvda:12m",
                target_value=250.0,
            )
        session.commit.assert_not_called()


class OutcomeTests(unittest.TestCase):
    def test_record_forecast_outcome_is_idempotent(self):
        session = Session(
            [
                Result(first={"present": 1}),  # forecast exists
                Result(first=None),  # no prior outcome
                Result(first={"id": FORECAST_ID}),  # INSERT won
            ]
        )
        recorded = record_forecast_outcome(
            session,
            str(FORECAST_ID),
            status="hit",
            actual_value=268.0,
            measured_at=NOW,
            notes="Closed above target.",
        )
        self.assertTrue(recorded)
        insert_sql = session.calls[2][0]
        self.assertIn("INSERT INTO investment_forecast_outcomes", insert_sql)
        self.assertIn("ON CONFLICT (forecast_id) DO NOTHING", insert_sql)
        self.assertIn("RETURNING id", insert_sql)
        self.assertEqual(session.calls[2][1]["status"], "hit")
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"present": 1}),  # outcome already recorded
            ]
        )
        self.assertFalse(
            record_forecast_outcome(session, str(FORECAST_ID), status="miss")
        )
        self.assertEqual(len(session.calls), 2)

    def test_concurrent_outcome_insert_noop_reports_false(self):
        # A concurrent winner records the outcome between the precheck and
        # the INSERT: the INSERT is a no-op and the loser must not claim a
        # write that never happened.
        session = Session(
            [
                Result(first={"present": 1}),  # forecast exists
                Result(first=None),  # precheck misses
                Result(first=None),  # concurrent winner: INSERT no-op
            ]
        )
        self.assertFalse(
            record_forecast_outcome(session, str(FORECAST_ID), status="hit")
        )
        session.commit.assert_not_called()

    def test_omitted_measured_at_materializes_aware_now(self):
        # measured_at is NOT NULL: the bound parameter must never be NULL.
        session = Session(
            [
                Result(first={"present": 1}),  # forecast exists
                Result(first=None),  # no prior outcome
                Result(first={"id": FORECAST_ID}),  # INSERT won
            ]
        )
        record_forecast_outcome(session, str(FORECAST_ID), status="hit")
        measured = session.calls[2][1]["measured_at"]
        self.assertIsNotNone(measured)
        self.assertIsInstance(measured, datetime)
        self.assertIsNotNone(measured.tzinfo)

    def test_unknown_forecast_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown forecast"):
            record_forecast_outcome(session, str(FORECAST_ID), status="hit")
        session.commit.assert_not_called()


class OpportunitySnapshotTests(unittest.TestCase):
    def test_append_snapshot_is_idempotent(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no prior snapshot
                Result(first={"id": LINK_ID}),  # INSERT won
            ]
        )
        appended = append_opportunity_snapshot(
            session,
            str(THESIS_ID),
            snapshot_key="eval:2026-08-06",
            opportunity_score=0.72,
            expected_value=0.18,
            expected_shortfall=0.05,
            confidence_score=0.65,
            neglect_score=0.6,
            catalyst_score=0.5,
            evidence_strength=0.8,
            contradiction_strength=0.1,
        )
        self.assertTrue(appended)
        insert_sql = session.calls[2][0]
        self.assertIn("INSERT INTO investment_opportunity_snapshots", insert_sql)
        self.assertIn("ON CONFLICT (thesis_id, snapshot_key) DO NOTHING", insert_sql)
        self.assertIn("RETURNING id", insert_sql)
        params = session.calls[2][1]
        self.assertEqual(params["snapshot_key"], "eval:2026-08-06")
        self.assertEqual(params["opportunity_score"], 0.72)
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"present": 1}),  # snapshot already exists
            ]
        )
        self.assertFalse(
            append_opportunity_snapshot(
                session,
                str(THESIS_ID),
                snapshot_key="eval:2026-08-06",
                opportunity_score=0.72,
            )
        )
        self.assertEqual(len(session.calls), 2)

    def test_concurrent_snapshot_insert_noop_reports_false(self):
        # A concurrent winner froze the same snapshot key between the
        # precheck and the INSERT: the INSERT is a no-op and the loser must
        # report the truthful insertion result.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # precheck misses
                Result(first=None),  # concurrent winner: INSERT no-op
            ]
        )
        self.assertFalse(
            append_opportunity_snapshot(
                session,
                str(THESIS_ID),
                snapshot_key="eval:2026-08-06",
                opportunity_score=0.72,
            )
        )
        session.commit.assert_not_called()

    def test_omitted_captured_at_materializes_aware_now(self):
        # captured_at is NOT NULL: the bound parameter must never be NULL.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no prior snapshot
                Result(first={"id": LINK_ID}),  # INSERT won
            ]
        )
        append_opportunity_snapshot(
            session,
            str(THESIS_ID),
            snapshot_key="eval:2026-08-06",
            opportunity_score=0.72,
        )
        captured = session.calls[2][1]["captured_at"]
        self.assertIsNotNone(captured)
        self.assertIsInstance(captured, datetime)
        self.assertIsNotNone(captured.tzinfo)

    def test_unknown_submetrics_persist_as_null_in_snapshot(self):
        # Unknown sub-metrics (migration 057) are stored as NULL, never
        # coerced to favorable zeros: an absent neglect input, catalyst
        # set, or directional evidence is unknown, while the gated
        # opportunity_score is always numeric.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no prior snapshot
                Result(first={"id": LINK_ID}),  # INSERT won
            ]
        )
        appended = append_opportunity_snapshot(
            session,
            str(THESIS_ID),
            snapshot_key="eval:2026-08-06",
            opportunity_score=0.0,
            expected_value=None,
            expected_shortfall=None,
            confidence_score=None,
            neglect_score=None,
            catalyst_score=None,
            evidence_strength=None,
            contradiction_strength=None,
        )
        self.assertTrue(appended)
        params = session.calls[2][1]
        self.assertIsNone(params["expected_value"])
        self.assertIsNone(params["expected_shortfall"])
        self.assertIsNone(params["confidence_score"])
        self.assertIsNone(params["neglect_score"])
        self.assertIsNone(params["catalyst_score"])
        self.assertIsNone(params["evidence_strength"])
        self.assertIsNone(params["contradiction_strength"])
        # The gated score is a real evaluation: a frozen zero opportunity
        # is preserved as the numeric zero it is.
        self.assertEqual(params["opportunity_score"], 0.0)
        session.commit.assert_not_called()


class FalsificationRunTests(unittest.TestCase):
    def test_record_run_is_idempotent(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no prior run
                Result(first={"id": RUN_ID}),  # INSERT RETURNING id
            ]
        )
        run_id = record_falsification_run(
            session,
            str(THESIS_ID),
            run_key="run:2026-08-06",
            status="in_progress",
            findings=[{"check": "capex guide", "result": "pending"}],
            started_at=NOW,
        )
        self.assertEqual(run_id, str(RUN_ID))
        insert_params = session.calls[2][1]
        self.assertEqual(insert_params["run_key"], "run:2026-08-06")
        self.assertEqual(insert_params["status"], "in_progress")
        self.assertIn('"capex guide"', insert_params["findings"])
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"id": RUN_ID}),  # already recorded
            ]
        )
        self.assertEqual(
            record_falsification_run(session, str(THESIS_ID), run_key="run:2026-08-06"),
            str(RUN_ID),
        )
        self.assertEqual(len(session.calls), 2)

    def test_concurrent_run_insert_falls_back_to_winner(self):
        # A concurrent event/schedule race can win the unique
        # (thesis_id, run_key) insert: the loser's INSERT is a no-op and the
        # winner's id is returned through a bounded lookup — the existing
        # run is never mutated.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # precheck misses
                Result(first=None),  # concurrent winner: INSERT no-op
                Result(first={"id": RUN_ID}),  # bounded winner lookup
            ]
        )
        run_id = record_falsification_run(
            session, str(THESIS_ID), run_key="run:2026-08-06"
        )
        self.assertEqual(run_id, str(RUN_ID))
        insert_sql = session.calls[2][0]
        self.assertIn("ON CONFLICT (thesis_id, run_key) DO NOTHING", insert_sql)
        self.assertIn("RETURNING id", insert_sql)
        session.commit.assert_not_called()

    def test_concurrent_run_without_winner_raises(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # precheck misses
                Result(first=None),  # INSERT no-op
                Result(first=None),  # winner lookup misses
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "without a winner"):
            record_falsification_run(session, str(THESIS_ID), run_key="run:2026-08-06")
        session.commit.assert_not_called()

    def test_omitted_started_at_materializes_aware_now(self):
        # started_at is NOT NULL: the bound parameter must never be NULL.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first=None),  # no prior run
                Result(first={"id": RUN_ID}),  # INSERT won
            ]
        )
        record_falsification_run(session, str(THESIS_ID), run_key="run:2026-08-06")
        started = session.calls[2][1]["started_at"]
        self.assertIsNotNone(started)
        self.assertIsInstance(started, datetime)
        self.assertIsNotNone(started.tzinfo)

    def test_update_run_lifecycle_and_finality(self):
        session = Session(
            [
                Result(  # current row: pending
                    first={
                        "status": "pending",
                        "started_at": NOW,
                        "completed_at": None,
                    }
                ),
                Result(),  # UPDATE run
            ]
        )
        update_falsification_run(
            session,
            str(RUN_ID),
            status="falsified",
            findings=[{"check": "capex guide", "result": "contradicted"}],
            completed_at=NOW,
        )
        update_sql = session.calls[1][0]
        self.assertIn("UPDATE investment_thesis_falsification_runs", update_sql)
        params = session.calls[1][1]
        self.assertEqual(params["status"], "falsified")
        self.assertEqual(params["completed_at"], NOW)
        session.commit.assert_not_called()

        terminal = Session(
            [
                Result(
                    first={
                        "status": "falsified",
                        "started_at": NOW,
                        "completed_at": NOW,
                    }
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "status is final"):
            update_falsification_run(terminal, str(RUN_ID), status="inconclusive")
        self.assertEqual(len(terminal.calls), 1)

        with self.assertRaisesRegex(ValueError, "terminal status"):
            update_falsification_run(
                Session(
                    [
                        Result(
                            first={
                                "status": "pending",
                                "started_at": NOW,
                                "completed_at": None,
                            }
                        )
                    ]
                ),
                str(RUN_ID),
                status="pending",
                completed_at=NOW,
            )


class PositionLinkTests(unittest.TestCase):
    def test_link_unlink_are_idempotent_and_versioned(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # holding exists
                Result(first=None),  # no active link
                Result(first={"id": LINK_ID}),  # INSERT RETURNING id
            ]
        )
        linked = link_position(
            session, str(THESIS_ID), str(POSITION_ID), link_type="primary"
        )
        self.assertTrue(linked)
        probe_sql = session.calls[2][0]
        self.assertIn("removed_at IS NULL", probe_sql)
        insert_sql = session.calls[3][0]
        self.assertIn("INSERT INTO position_thesis_links", insert_sql)
        self.assertIn("ON CONFLICT (position_id, thesis_id, link_type)", insert_sql)
        self.assertIn("WHERE removed_at IS NULL", insert_sql)
        self.assertIn("DO NOTHING", insert_sql)
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"present": 1}),
                Result(first={"present": 1}),  # already linked (active)
            ]
        )
        self.assertFalse(link_position(session, str(THESIS_ID), str(POSITION_ID)))

    def test_relink_after_unlink_inserts_fresh_active_row(self):
        # A removed (unlinked) row must not block a relink: the probe only
        # sees active rows, and the partial unique index only guards
        # removed_at IS NULL rows, so the new link inserts a fresh row.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # holding exists
                Result(first=None),  # no ACTIVE link (stale row exists)
                Result(first={"id": LINK_ID}),  # fresh INSERT RETURNING id
            ]
        )
        linked = link_position(
            session, str(THESIS_ID), str(POSITION_ID), link_type="primary"
        )
        self.assertTrue(linked)
        probe_sql = session.calls[2][0]
        self.assertIn("removed_at IS NULL", probe_sql)
        insert_sql = session.calls[3][0]
        self.assertIn("ON CONFLICT (position_id, thesis_id, link_type)", insert_sql)
        self.assertIn("WHERE removed_at IS NULL", insert_sql)
        session.commit.assert_not_called()

    def test_concurrent_link_race_is_idempotent(self):
        # Two linkers race: both probes miss, one INSERT wins, the loser's
        # INSERT is a no-op on the partial active unique index instead of a
        # unique-violation error.  The loser reports the truthful result
        # (no row returned) while the link ends up present.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # holding exists
                Result(first=None),  # probe misses
                Result(first={"id": LINK_ID}),  # INSERT won
            ]
        )
        self.assertTrue(link_position(session, str(THESIS_ID), str(POSITION_ID)))
        insert_sql = session.calls[3][0]
        self.assertIn("ON CONFLICT (position_id, thesis_id, link_type)", insert_sql)
        self.assertIn("WHERE removed_at IS NULL", insert_sql)
        self.assertIn("DO NOTHING", insert_sql)
        self.assertIn("RETURNING id", insert_sql)
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(first={"present": 1}),  # holding exists
                Result(first=None),  # probe misses
                Result(first=None),  # concurrent winner: INSERT no-op
            ]
        )
        self.assertFalse(link_position(session, str(THESIS_ID), str(POSITION_ID)))
        session.commit.assert_not_called()

        session = Session(
            [
                Result(first={"id": LINK_ID}),  # active link found
                Result(),  # UPDATE removed_at
            ]
        )
        unlinked = unlink_position(
            session, str(THESIS_ID), str(POSITION_ID), link_type="primary"
        )
        self.assertTrue(unlinked)
        update_sql = session.calls[1][0]
        self.assertIn("UPDATE position_thesis_links", update_sql)
        self.assertIn("removed_at = NOW()", update_sql)
        self.assertIn("removed_at IS NULL", update_sql)
        session.commit.assert_not_called()

        session = Session([Result(first=None)])
        self.assertFalse(unlink_position(session, str(THESIS_ID), str(POSITION_ID)))

    def test_unknown_position_raises(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(first=None),  # holding missing
            ]
        )
        with self.assertRaisesRegex(ValueError, "unknown position"):
            link_position(session, str(THESIS_ID), str(POSITION_ID))
        session.commit.assert_not_called()


class EvaluateThesisTests(unittest.TestCase):
    def _strong_support_signal(self, evidence_id, independence_key, content):
        return EvidenceSignal.create(
            evidence_id=evidence_id,
            evidence_type="source_claim",
            relationship="supports",
            source_name="filings",
            source_family="filings",
            origin_key=f"sec:10q:{independence_key}",
            independence_key=independence_key,
            content=content,
            source_timestamp=NOW,
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
            effective_weight=1.0,
            provenance={
                "excerpt": "Verbatim disclosed evidence excerpt for the thesis claim.",
            },
        )

    def test_evaluate_persists_deterministic_scores(self):
        first = self._strong_support_signal(
            "claim:a", "filings:nvda", {"statement": "Capex up.", "n": 1}
        )
        second = self._strong_support_signal(
            "claim:b", "customers:nvda", {"statement": "Orders up.", "n": 2}
        )
        evidence_rows = [
            evidence_row(
                evidence_type="source_claim",
                evidence_id=first.evidence_id,
                relationship="supports",
                source_family="filings",
                origin_key=first.origin_key,
                independence_key=first.independence_key,
                evidence_fingerprint=first.evidence_fingerprint,
                quality_score=0.9,
                entailment_score=0.9,
                freshness_score=0.8,
            ),
            evidence_row(
                evidence_type="source_claim",
                evidence_id=second.evidence_id,
                relationship="supports",
                source_family="filings",
                origin_key=second.origin_key,
                independence_key=second.independence_key,
                evidence_fingerprint=second.evidence_fingerprint,
                quality_score=0.9,
                entailment_score=0.9,
                freshness_score=0.8,
            ),
        ]
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=evidence_rows),
                Result(  # one confirmed catalyst
                    rows=[
                        {
                            "description": "Capex guide raise",
                            "state": "confirmed",
                            "expected_at": NOW,
                        }
                    ]
                ),
                Result(  # one active base-case scenario
                    rows=[
                        {"name": "Base", "probability": 1.0},
                    ]
                ),
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=NOW,
            expected_returns={"Base": 0.2},
            cost=0.0,
            attention=0.1,
            crowding=0.2,
            liquidity=0.9,
            downside=0.1,
        )
        # Independently recompute the expected scores from the same inputs.
        expected_evidence = assess_evidence([first, second])
        expected_neglect = calculate_neglect(attention=0.1, crowding=0.2)
        expected_catalyst = catalyst_readiness(
            [CatalystSignal.create(description="Capex guide raise", state="confirmed")],
            as_of=NOW,
        )
        expected_opportunity = assess_opportunity(
            evidence_strength=expected_evidence.support_mass,
            confidence=expected_evidence.confidence,
            neglect=expected_neglect.neglect,
            catalyst_ready=expected_catalyst.readiness,
            liquidity=0.9,
            downside=0.1,
        )
        self.assertEqual(result["evidence"]["support_count"], 2)
        self.assertEqual(
            result["evidence"]["support_mass"], expected_evidence.support_mass
        )
        self.assertEqual(
            result["opportunity"]["opportunity"], expected_opportunity.opportunity
        )
        self.assertGreater(result["opportunity"]["opportunity"], 0.0)
        self.assertAlmostEqual(result["valuation"]["expected_value"], 0.2)
        update_params = session.calls[4][1]
        self.assertEqual(
            update_params["evidence_strength"], expected_evidence.support_mass
        )
        self.assertEqual(
            update_params["contradiction_strength"],
            expected_evidence.contradiction_mass,
        )
        self.assertEqual(
            update_params["confidence_score"], expected_evidence.confidence
        )
        self.assertEqual(
            update_params["opportunity_score"], expected_opportunity.opportunity
        )
        self.assertEqual(update_params["expected_value"], 0.2)
        session.commit.assert_not_called()

    def test_evaluate_uses_stored_scenario_expected_return(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no evidence
                Result(rows=[]),  # no catalysts
                Result(  # one active scenario with persisted return
                    rows=[
                        {
                            "name": "Base",
                            "probability": 0.5,
                            "expected_return": 0.4,
                        }
                    ]
                ),
                Result(rows=[]),  # no market bars; liquidity stays unknown
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
        expected_valuation = scenario_valuation(
            [Scenario.create(label="Base", probability=0.5, expected_return=0.4)]
        )
        self.assertEqual(
            result["valuation"]["expected_value"],
            expected_valuation.expected_value,
        )
        self.assertAlmostEqual(result["valuation"]["expected_value"], 0.2)
        session.commit.assert_not_called()

    def test_unknown_submetrics_persist_as_null_not_favorable_zero(self):
        # No directional evidence, no catalyst set, no attention/crowding
        # inputs: neglect, catalyst readiness, and confidence are unknown.
        # evaluate_thesis must persist them as NULL (migration 057) — never
        # as favorable zeros — while measured masses (0.0 support and
        # contradiction) and the gated opportunity score stay numeric.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no evidence
                Result(rows=[]),  # no catalysts
                Result(rows=[{"name": "Base", "probability": 1.0}]),  # one scenario
                Result(rows=[]),  # no market bars; liquidity stays unknown
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
        # The evaluation result itself carries the unknowns, not zeros.
        self.assertIsNone(result["neglect"]["neglect"])
        self.assertIsNone(result["catalyst"]["readiness"])
        self.assertIsNone(result["evidence"]["confidence"])
        update_params = session.calls[5][1]
        self.assertIsNone(update_params["neglect_score"])
        self.assertIsNone(update_params["catalyst_score"])
        self.assertIsNone(update_params["confidence_score"])
        # Measured values stay numeric: an empty directional set scores a
        # real 0.0 mass, and the failed gate yields a numeric 0.0
        # opportunity — both are evaluations, not unknowns.
        self.assertEqual(update_params["evidence_strength"], 0.0)
        self.assertEqual(update_params["contradiction_strength"], 0.0)
        self.assertEqual(update_params["opportunity_score"], 0.0)
        session.commit.assert_not_called()

    def test_replay_cutoff_bounds_every_persisted_input_query(self):
        # Re-evaluating an older accepted cutoff: later evidence, derived
        # versions, and backfilled bars must be excluded at the SQL
        # boundary, so no timestamp after `as_of` can reach the score.
        cutoff = NOW - timedelta(days=30)
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[evidence_row()]),
                Result(rows=self._confirmed_catalyst_rows()),
                Result(rows=self._complete_scenario_rows()),
                Result(rows=[{"close": 100.0, "volume": 1_000_000.0}]),
                Result(),  # UPDATE thesis score columns
            ]
        )
        evaluate_thesis(session, str(THESIS_ID), as_of=cutoff)
        evidence_sql, evidence_params = session.calls[1]
        self.assertIn("created_at <= :as_of", evidence_sql)
        self.assertIn("COALESCE(source_timestamp, created_at) <= :as_of", evidence_sql)
        self.assertIn(
            "COALESCE(available_at, source_timestamp, created_at) <= :as_of",
            evidence_sql,
        )
        self.assertEqual(evidence_params["as_of"], cutoff)
        catalyst_sql, catalyst_params = session.calls[2]
        self.assertIn("created_at <= :as_of", catalyst_sql)
        self.assertIn("updated_at <= :as_of", catalyst_sql)
        self.assertEqual(catalyst_params["as_of"], cutoff)
        scenario_sql, scenario_params = session.calls[3]
        self.assertIn("created_at <= :as_of", scenario_sql)
        self.assertIn("(superseded_at IS NULL OR superseded_at > :as_of)", scenario_sql)
        self.assertEqual(scenario_params["as_of"], cutoff)
        market_sql, market_params = session.calls[4]
        self.assertIn("m.timestamp <= :as_of", market_sql)
        self.assertIn("COALESCE(m.updated_at, m.created_at) <= :as_of", market_sql)
        self.assertEqual(market_params["as_of"], cutoff)
        session.commit.assert_not_called()

    def test_explicit_current_cycle_inputs_merge_with_cutoff_valid_rows(self):
        # The current cycle's derived legs/catalysts postdate the cutoff and
        # enter scoring explicitly.  A same-label persisted scenario is
        # replaced (the cycle's immutable upsert superseded it during the
        # run) while unrelated persisted rows stay; a same-description
        # persisted catalyst wins (the cycle insert was a no-op).
        cutoff = NOW - timedelta(days=30)
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no evidence
                Result(  # persisted catalysts valid at the cutoff
                    rows=[
                        {
                            "description": "Capex guide raise",
                            "state": "confirmed",
                            "expected_at": NOW - timedelta(days=31),
                        },
                        {
                            "description": "New trigger",
                            "state": "pending",
                            "expected_at": None,
                        },
                    ]
                ),
                Result(  # persisted scenarios valid at the cutoff
                    rows=[
                        {"name": "Base", "probability": 0.5, "expected_return": 0.9},
                        {"name": "Extra", "probability": 0.2, "expected_return": 0.3},
                    ]
                ),
                Result(rows=[]),  # no market bars; liquidity stays unknown
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=cutoff,
            current_scenarios=(
                Scenario.create(label="Base", probability=0.5, expected_return=0.2),
            ),
            current_catalysts=(
                CatalystSignal.create(
                    description="Capex guide raise", state="pending", expected_at=None
                ),
                CatalystSignal.create(
                    description="Quarterly disclosure",
                    state="pending",
                    expected_at=None,
                ),
            ),
        )
        # Base: the explicit 0.5*0.2 leg wins over the persisted 0.5*0.9;
        # Extra is preserved (0.2*0.3): expected value 0.10 + 0.06.
        expected_valuation = scenario_valuation(
            [
                Scenario.create(label="Extra", probability=0.2, expected_return=0.3),
                Scenario.create(label="Base", probability=0.5, expected_return=0.2),
            ]
        )
        self.assertEqual(
            result["valuation"]["expected_value"],
            expected_valuation.expected_value,
        )
        self.assertAlmostEqual(result["valuation"]["expected_value"], 0.16)
        # The same-description persisted catalyst (confirmed) wins over the
        # explicit pending duplicate; the new explicit catalyst is appended.
        expected_catalyst = catalyst_readiness(
            [
                CatalystSignal.create(
                    description="Capex guide raise",
                    state="confirmed",
                    expected_at=NOW - timedelta(days=31),
                ),
                CatalystSignal.create(
                    description="New trigger", state="pending", expected_at=None
                ),
                CatalystSignal.create(
                    description="Quarterly disclosure",
                    state="pending",
                    expected_at=None,
                ),
            ],
            as_of=cutoff,
        )
        self.assertEqual(result["catalyst"]["readiness"], expected_catalyst.readiness)
        session.commit.assert_not_called()

    def test_explicit_current_cycle_evidence_merges_with_cutoff_valid_rows(self):
        # The cycle's own cited evidence/contradictions postdate the cutoff
        # and enter scoring explicitly: a persisted cutoff-valid row wins on
        # an identical fingerprint (it keeps its stored scores), new
        # fingerprints are appended once, and duplicates inside the explicit
        # input collapse deterministically.
        cutoff = NOW - timedelta(days=30)
        persisted = self._strong_support_signal(
            "claim:a", "filings:nvda", {"statement": "Capex up."}
        )
        fresh = self._strong_support_signal(
            "claim:b", "customers:nvda", {"statement": "Orders up."}
        )
        duplicate = EvidenceSignal.create(
            evidence_id="claim:dup",
            evidence_type="source_claim",
            relationship="supports",
            source_name="filings",
            source_family="filings",
            independence_key="other:nvda",
            evidence_fingerprint=persisted.evidence_fingerprint,
            source_timestamp=cutoff,
            available_at=cutoff,
        )
        persisted_signal = EvidenceSignal.create(
            evidence_id=persisted.evidence_id,
            evidence_type="source_claim",
            relationship="supports",
            source_name="filings",
            source_family="filings",
            origin_key=persisted.origin_key,
            independence_key=persisted.independence_key,
            evidence_fingerprint=persisted.evidence_fingerprint,
            source_timestamp=cutoff - timedelta(days=1),
            available_at=cutoff - timedelta(days=1),
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
            effective_weight=1.0,
            provenance={
                "excerpt": "Verbatim disclosed evidence excerpt for the thesis claim.",
            },
        )
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(  # one persisted cutoff-valid row
                    rows=[
                        evidence_row(
                            evidence_type=persisted.evidence_type,
                            evidence_id=persisted.evidence_id,
                            relationship="supports",
                            source_family="filings",
                            origin_key=persisted.origin_key,
                            independence_key=persisted.independence_key,
                            evidence_fingerprint=persisted.evidence_fingerprint,
                            quality_score=0.9,
                            entailment_score=0.9,
                            freshness_score=0.8,
                        )
                    ]
                ),
                Result(rows=[]),  # no catalysts
                Result(rows=[]),  # no scenarios
                Result(rows=[]),  # no market bars; liquidity stays unknown
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=cutoff,
            current_evidence=(
                duplicate,  # same fingerprint as the persisted row: persisted wins
                fresh,  # new fingerprint: appended once
                duplicate,  # explicit input duplicate: still one signal
            ),
        )
        expected = assess_evidence([persisted_signal, fresh])
        self.assertEqual(result["evidence"]["support_count"], 2)
        self.assertEqual(result["evidence"]["support_mass"], expected.support_mass)
        # Attention derives from the merged signals, not just persisted rows.
        self.assertAlmostEqual(result["neglect"]["attention"], 2 / 10.0)
        session.commit.assert_not_called()

    def test_stale_evaluation_cannot_overwrite_newer_ranking_state(self):
        # A job finishing after a later evaluation may return its computed
        # result but must not regress current ranking columns: the UPDATE is
        # conditioned on the persisted last_evaluated_at and stamps the
        # accepted as_of reference, and the immutable opportunity snapshot
        # is still appended (history) even when the UPDATE is a no-op.
        cutoff = NOW - timedelta(days=30)
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no evidence
                Result(rows=[]),  # no catalysts
                Result(rows=[]),  # no scenarios
                Result(rows=[]),  # no market bars
                Result(),  # UPDATE thesis score columns (stale: no-op)
                Result(first={"present": 1}),  # snapshot: thesis exists
                Result(first=None),  # snapshot: no prior key
                Result(),  # snapshot INSERT
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=cutoff,
            snapshot_key="eval:2026-08-06",
        )
        update_sql, update_params = session.calls[5]
        self.assertTrue(update_sql.startswith("UPDATE investment_theses"))
        self.assertIn("last_evaluated_at = :as_of", update_sql)
        self.assertIn(
            "(last_evaluated_at IS NULL OR last_evaluated_at <= :as_of)", update_sql
        )
        self.assertEqual(update_params["as_of"], cutoff)
        self.assertEqual(result["as_of"], cutoff.isoformat())
        snapshot_sql, snapshot_params = session.calls[8]
        self.assertIn("INSERT INTO investment_opportunity_snapshots", snapshot_sql)
        self.assertEqual(snapshot_params["snapshot_key"], "eval:2026-08-06")
        self.assertIsNone(snapshot_params["neglect_score"])
        self.assertIsNone(snapshot_params["catalyst_score"])
        self.assertIsNone(snapshot_params["confidence_score"])
        self.assertEqual(snapshot_params["evidence_strength"], 0.0)
        self.assertEqual(snapshot_params["contradiction_strength"], 0.0)
        self.assertEqual(snapshot_params["opportunity_score"], 0.0)
        session.commit.assert_not_called()

    def test_future_mutated_legacy_catalyst_excluded_from_older_cutoff(self):
        # Migration 054 stamps pre-migration rows; a catalyst whose stored
        # mutation time (updated_at) lies after the cutoff must not reach an
        # older replay even when created_at predates it.
        cutoff = NOW - timedelta(days=30)
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no evidence
                # The queued fake never applies SQL predicates, so the
                # catalyst result models the DB filter (created_at <= as_of
                # AND updated_at <= as_of) excluding the later-mutated row;
                # the actual filtering is covered by the PG behavior tests.
                Result(rows=[]),  # no cutoff-valid catalysts
                Result(rows=[]),  # no scenarios
                Result(rows=[]),  # no market bars; liquidity stays unknown
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=cutoff)
        self.assertEqual(result["catalyst"]["catalyst_count"], 0)
        self.assertIsNone(result["catalyst"]["readiness"])
        catalyst_sql, catalyst_params = session.calls[2]
        self.assertIn("created_at <= :as_of", catalyst_sql)
        self.assertIn("updated_at <= :as_of", catalyst_sql)
        self.assertEqual(catalyst_params["as_of"], cutoff)
        session.commit.assert_not_called()

    def test_evaluate_with_snapshot_freezes_result(self):
        signal = self._strong_support_signal(
            "claim:a", "filings:nvda", {"statement": "Capex up."}
        )
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(
                    rows=[
                        evidence_row(evidence_fingerprint=signal.evidence_fingerprint)
                    ]
                ),
                Result(rows=[]),  # no catalysts
                Result(rows=[]),  # no scenarios
                Result(rows=[]),  # no market bars; liquidity stays unknown
                Result(),  # UPDATE thesis score columns
                Result(first={"present": 1}),  # snapshot: thesis exists
                Result(first=None),  # snapshot: no prior key
                Result(),  # snapshot INSERT
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=NOW,
            snapshot_key="eval:2026-08-06",
        )
        self.assertEqual(result["thesis_id"], str(THESIS_ID))
        snapshot_sql = session.calls[8][0]
        self.assertIn("INSERT INTO investment_opportunity_snapshots", snapshot_sql)
        snapshot_params = session.calls[8][1]
        self.assertEqual(snapshot_params["snapshot_key"], "eval:2026-08-06")
        self.assertEqual(
            snapshot_params["opportunity_score"],
            result["opportunity"]["opportunity"],
        )
        session.commit.assert_not_called()

    def test_evaluate_blocks_opportunity_without_directional_evidence(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(  # only context evidence
                    rows=[
                        evidence_row(
                            evidence_type="market_state",
                            evidence_id="market:level",
                            relationship="context",
                            source_family="market",
                            origin_key="market:level",
                            independence_key=None,
                            evidence_fingerprint="c" * 64,
                        )
                    ]
                ),
                Result(rows=[]),
                Result(rows=[]),
                Result(),
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=NOW,
            liquidity=0.9,
            downside=0.1,
        )
        self.assertIsNone(result["evidence"]["confidence"])
        self.assertEqual(result["opportunity"]["opportunity"], 0.0)
        self.assertIn("confidence", result["opportunity"]["blocked_by"])
        self.assertEqual(session.calls[4][1]["opportunity_score"], 0.0)
        session.commit.assert_not_called()

    def _two_strong_supports(self):
        first = self._strong_support_signal(
            "claim:a", "filings:nvda", {"statement": "Capex up.", "n": 1}
        )
        second = self._strong_support_signal(
            "claim:b", "customers:nvda", {"statement": "Orders up.", "n": 2}
        )
        return [
            evidence_row(
                evidence_type="source_claim",
                evidence_id=first.evidence_id,
                relationship="supports",
                source_family="filings",
                origin_key=first.origin_key,
                independence_key=first.independence_key,
                evidence_fingerprint=first.evidence_fingerprint,
                quality_score=0.9,
                entailment_score=0.9,
                freshness_score=0.8,
            ),
            evidence_row(
                evidence_type="source_claim",
                evidence_id=second.evidence_id,
                relationship="supports",
                source_family="filings",
                origin_key=second.origin_key,
                independence_key=second.independence_key,
                evidence_fingerprint=second.evidence_fingerprint,
                quality_score=0.9,
                entailment_score=0.9,
                freshness_score=0.8,
            ),
        ]

    def _confirmed_catalyst_rows(self):
        return [
            {
                "description": "Capex guide raise",
                "state": "confirmed",
                "expected_at": NOW,
            }
        ]

    def _complete_scenario_rows(self):
        return [
            {"name": "Bull", "probability": 0.3, "expected_return": 0.3},
            {"name": "Base", "probability": 0.5, "expected_return": 0.0},
            {"name": "Bear", "probability": 0.2, "expected_return": -0.2},
        ]

    def test_derived_attention_liquidity_and_downside_gate_opportunity(self):
        # No explicit score inputs: attention comes from unique evidence
        # density (6/10), liquidity from the median daily notional (20 bars
        # of close 100.0 * volume 1m = 1e8 -> 2/3), and downside from
        # expected_shortfall / 0.5 (bear 0.2 * -0.2 -> 0.08). All gates pass,
        # so the blend is nonzero and every derivation is visible.
        evidence_rows = [
            evidence_row(
                evidence_type="source_claim",
                evidence_id=f"claim:src-{i}",
                relationship="supports",
                source_family="filings",
                origin_key=f"sec:10q:nvda:{i}",
                independence_key=f"filings:nvda:{i}",
                evidence_fingerprint=f"{i:02d}" * 32,
                quality_score=0.9,
                entailment_score=0.9,
                freshness_score=0.8,
            )
            for i in range(6)
        ]
        market_rows = [{"close": 100.0, "volume": 1_000_000.0} for _ in range(20)]
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=evidence_rows),
                Result(rows=self._confirmed_catalyst_rows()),
                Result(rows=self._complete_scenario_rows()),
                Result(rows=market_rows),  # bounded market liquidity lookback
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
        expected_attention = 6 / 10.0
        expected_neglect = calculate_neglect(attention=expected_attention)
        expected_liquidity = (math.log10(1e8) - 6.0) / 3.0
        expected_downside = 0.04 / 0.5
        self.assertAlmostEqual(result["neglect"]["attention"], expected_attention)
        self.assertAlmostEqual(
            result["opportunity"]["neglect"], expected_neglect.neglect
        )
        self.assertAlmostEqual(result["opportunity"]["liquidity"], expected_liquidity)
        self.assertAlmostEqual(result["opportunity"]["downside"], expected_downside)
        self.assertEqual(result["opportunity"]["missing"], [])
        self.assertEqual(result["opportunity"]["blocked_by"], [])
        self.assertGreater(result["opportunity"]["opportunity"], 0.0)
        # The liquidity lookup is bounded to the configured lookback and
        # fails closed on bars whose row revision time postdates the
        # reference (COALESCE(updated_at, created_at) <= as_of).
        market_sql, market_params = session.calls[4]
        self.assertIn("JOIN market_data m", market_sql)
        self.assertIn("COALESCE(m.updated_at, m.created_at) <= :as_of", market_sql)
        self.assertEqual(market_params["id"], str(THESIS_ID))
        self.assertEqual(market_params["limit"], 20)
        self.assertEqual(
            session.calls[5][1]["opportunity_score"],
            result["opportunity"]["opportunity"],
        )
        session.commit.assert_not_called()

    def test_incomplete_or_empty_scenarios_block_derived_downside(self):
        # Scenarios that do not sum to one (or are absent) leave expected
        # shortfall unknown even when market data would satisfy liquidity.
        for scenario_rows in (
            [{"name": "Base", "probability": 0.5, "expected_return": 0.4}],
            [],
        ):
            session = Session(
                [
                    Result(first={"present": 1}),  # thesis exists
                    Result(rows=self._two_strong_supports()),
                    Result(rows=self._confirmed_catalyst_rows()),
                    Result(rows=scenario_rows),
                    Result(
                        rows=[
                            {"close": 100.0, "volume": 1_000_000.0} for _ in range(20)
                        ]
                    ),
                    Result(),  # UPDATE thesis score columns
                ]
            )
            result = evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
            self.assertIsNone(result["opportunity"]["downside"])
            self.assertEqual(result["opportunity"]["opportunity"], 0.0)
            self.assertIn("downside", result["opportunity"]["blocked_by"])
            self.assertIn("downside", result["opportunity"]["missing"])
            self.assertEqual(session.calls[5][1]["opportunity_score"], 0.0)
            session.commit.assert_not_called()

    def test_missing_market_data_blocks_derived_liquidity(self):
        # No market bars: liquidity stays unknown and the opportunity is
        # blocked even though scenarios are complete.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=self._two_strong_supports()),
                Result(rows=self._confirmed_catalyst_rows()),
                Result(rows=self._complete_scenario_rows()),
                Result(rows=[]),  # no market bars
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
        self.assertIsNone(result["opportunity"]["liquidity"])
        self.assertEqual(result["opportunity"]["opportunity"], 0.0)
        self.assertIn("liquidity", result["opportunity"]["blocked_by"])
        self.assertIn("liquidity", result["opportunity"]["missing"])
        self.assertEqual(session.calls[5][1]["opportunity_score"], 0.0)
        session.commit.assert_not_called()

    def test_market_liquidity_lookup_carries_the_revision_cutoff(self):
        # Liquidity scoring only consumes bars whose row revision time
        # (COALESCE(updated_at, created_at)) is at/before the reference, so
        # a bar revised after the accepted cutoff cannot change a historical
        # score even when its event timestamp predates the cutoff.  The DB
        # filter is authoritative (the fake mirrors it by returning only
        # cutoff-valid bars); this test pins the SQL contract.
        cutoff = NOW - timedelta(days=30)
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=self._two_strong_supports()),
                Result(rows=self._confirmed_catalyst_rows()),
                Result(rows=self._complete_scenario_rows()),
                Result(
                    rows=[{"close": 100.0, "volume": 1_000_000.0} for _ in range(20)]
                ),  # cutoff-valid bars
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=cutoff)
        self.assertIsNotNone(result["opportunity"]["liquidity"])
        market_sql, market_params = session.calls[4]
        self.assertIn("JOIN market_data m", market_sql)
        self.assertIn("m.timestamp <= :as_of", market_sql)
        self.assertIn("COALESCE(m.updated_at, m.created_at) <= :as_of", market_sql)
        self.assertNotIn("m.created_at <= :as_of", market_sql)
        self.assertEqual(market_params["as_of"], cutoff)
        self.assertEqual(market_params["limit"], 20)

    def test_absent_evidence_leaves_derived_attention_unknown(self):
        # No evidence rows: attention stays unknown instead of being
        # invented, so neglect is unknown and the opportunity is blocked.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=[]),  # no evidence
                Result(rows=[]),  # no catalysts
                Result(rows=[]),  # no scenarios
                Result(rows=[]),  # no market bars
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
        self.assertIsNone(result["neglect"]["attention"])
        self.assertIsNone(result["opportunity"]["neglect"])
        self.assertIn("neglect", result["opportunity"]["blocked_by"])
        session.commit.assert_not_called()

    def test_explicit_score_inputs_override_derivation(self):
        # Market bars and complete scenarios would derive liquidity ~2/3 and
        # downside 0.08; explicit values must win and skip the market query
        # entirely (the fake queue has no market result, so any derived
        # lookup would fail loudly).
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=self._two_strong_supports()),
                Result(rows=self._confirmed_catalyst_rows()),
                Result(rows=self._complete_scenario_rows()),
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=NOW,
            attention=0.1,
            crowding=0.2,
            liquidity=0.9,
            downside=0.1,
        )
        expected_neglect = calculate_neglect(attention=0.1, crowding=0.2)
        expected_opportunity = assess_opportunity(
            evidence_strength=result["evidence"]["support_mass"],
            confidence=result["evidence"]["confidence"],
            neglect=expected_neglect.neglect,
            catalyst_ready=result["catalyst"]["readiness"],
            liquidity=0.9,
            downside=0.1,
        )
        self.assertEqual(result["neglect"]["attention"], 0.1)
        self.assertEqual(result["opportunity"]["liquidity"], 0.9)
        self.assertEqual(result["opportunity"]["downside"], 0.1)
        self.assertEqual(
            result["opportunity"]["opportunity"], expected_opportunity.opportunity
        )
        self.assertGreater(result["opportunity"]["opportunity"], 0.0)
        # UPDATE is the fifth and final call; the market lookup never ran.
        self.assertEqual(len(session.calls), 5)
        self.assertTrue(session.calls[4][0].startswith("UPDATE investment_theses"))
        self.assertFalse(any("market_data" in call[0] for call in session.calls))
        session.commit.assert_not_called()

    def test_explicit_sub_gate_liquidity_wins_over_liquid_market(self):
        # An explicit sub-gate liquidity blocks the thesis even though its
        # market bars would have derived a passing liquidity.
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(rows=self._two_strong_supports()),
                Result(rows=self._confirmed_catalyst_rows()),
                Result(rows=self._complete_scenario_rows()),
                Result(),  # UPDATE thesis score columns
            ]
        )
        result = evaluate_thesis(
            session,
            str(THESIS_ID),
            as_of=NOW,
            attention=0.1,
            crowding=0.2,
            liquidity=0.1,
            downside=0.1,
        )
        self.assertEqual(result["opportunity"]["liquidity"], 0.1)
        self.assertEqual(result["opportunity"]["opportunity"], 0.0)
        self.assertIn("liquidity", result["opportunity"]["blocked_by"])
        self.assertEqual(len(session.calls), 5)
        self.assertFalse(any("market_data" in call[0] for call in session.calls))
        session.commit.assert_not_called()

    def test_evaluate_unknown_thesis_raises(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown thesis"):
            evaluate_thesis(session, str(THESIS_ID), as_of=NOW)
        session.commit.assert_not_called()


class RankedOpportunitiesTests(unittest.TestCase):
    def _eligible_row(self, **overrides):
        value = {
            "id": THESIS_ID,
            "theme_id": THEME_ID,
            "company": "Nvidia Corp",
            "symbol": "NVDA",
            "claim": "AI capex compounds.",
            "direction": "long",
            "mechanism": "AI capex compounds.",
            "horizon": "multi_year",
            "status": "active",
            "origin": "fusion",
            "trend_context": "measured trend",
            "valuation_context": "valuation context",
            "sentiment_context": "measured sentiment",
            "citation_map": {
                "claim": ["a"],
                "consensus": ["a"],
                "variant_perception": ["a"],
                "mechanism": ["a"],
                "catalyst": ["a"],
                "trend": ["b"],
                "valuation": ["a", "b"],
                "sentiment": ["c"],
            },
            "evidence_strength": 0.8,
            "contradiction_strength": 0.1,
            "neglect_score": 0.6,
            "catalyst_score": 0.5,
            "confidence_score": 0.65,
            "expected_value": 0.18,
            "expected_shortfall": 0.05,
            "opportunity_score": 0.72,
            "last_evaluated_at": NOW,
            "last_evidence_at": NOW,
            "group_id": None,
            "group_name": None,
            "eligibility_status": True,
            "eligibility_score": True,
            "eligibility_scenarios": True,
            "eligibility_risks": True,
            "eligibility_evidence": True,
            "eligibility_falsification": True,
            "eligibility_actionability": True,
            "eligibility_opposition": True,
        }
        value.update(overrides)
        return value

    def test_ranking_is_bounded_and_deterministic(self):
        session = Session([Result(rows=[self._eligible_row()])])
        rows = list_ranked_opportunities(session, limit=5, group_id=str(GROUP_ID))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], THESIS_ID)
        self.assertTrue(rows[0]["eligible"])
        self.assertEqual(rows[0]["blockers"], [])
        self.assertNotIn("eligibility_score", rows[0])
        sql = session.calls[0][0]
        # The loader evaluates every rank-eligibility gate against CURRENT
        # thesis and child rows: an investable status, active scenario legs,
        # existing structured risks, auditable supporting evidence, the
        # deterministic latest falsification run, actionable citation
        # coverage, and a complementary base-eligible long/short opponent.
        self.assertIn("WITH base_eligibility AS", sql)
        self.assertIn("t.status IN ('candidate', 'active') AS status_ok", sql)
        self.assertIn("s.superseded_at IS NULL", sql)
        self.assertIn("s.name IN ('bull', 'base', 'bear')", sql)
        self.assertIn("HAVING COUNT(*) = 3", sql)
        self.assertIn("ABS(SUM(legs.probability) - 1.0) < 1e-9", sql)
        self.assertIn("FROM investment_risks r", sql)
        self.assertIn("AND BTRIM(r.description) <> ''", sql)
        self.assertIn("AND e.relationship = 'supports'", sql)
        self.assertIn("AND e.quality_score > 0", sql)
        self.assertIn("AND e.entailment_score > 0", sql)
        self.assertIn("SELECT DISTINCT ON (f.thesis_id)", sql)
        self.assertIn("ORDER BY f.thesis_id, f.started_at DESC,", sql)
        self.assertIn("f.run_key", sql)
        self.assertIn("WHERE latest_run.status = 'not_falsified'", sql)
        self.assertIn("t.citation_map ?& ARRAY[", sql)
        self.assertIn("COUNT(DISTINCT LOWER(e.source_family))", sql)
        self.assertIn("JSONB_ARRAY_ELEMENTS_TEXT", sql)
        self.assertIn("e.evidence_type || ':' ||", sql)
        self.assertIn("FROM base_eligibility opponent", sql)
        self.assertIn("opponent.base_eligible", sql)
        self.assertIn("candidate.direction_key = 'long'", sql)
        self.assertIn("opponent.direction_key = 'short'", sql)
        # Default mode filters to eligible rows only and ranks them first,
        # then by expected value, opportunity score, confidence, catalyst,
        # neglect, recency, and id.
        self.assertIn("AND eligibility.eligible", sql)
        # Every DESC rank metric pins NULLS LAST: an unknown metric ranks
        # after every measured value, including zero.
        self.assertIn("ORDER BY (t.opportunity_score > 0) DESC NULLS LAST", sql)
        self.assertIn("eligibility.eligible DESC NULLS LAST", sql)
        self.assertIn("t.expected_value DESC NULLS LAST", sql)
        self.assertIn("t.opportunity_score DESC NULLS LAST", sql)
        self.assertIn("t.confidence_score DESC NULLS LAST", sql)
        self.assertIn("t.catalyst_score DESC NULLS LAST", sql)
        self.assertIn("t.neglect_score DESC NULLS LAST", sql)
        self.assertIn("t.last_evaluated_at DESC NULLS LAST, t.id", sql)
        self.assertIn("LIMIT :limit", sql)
        self.assertNotIn("t.group_id =", sql)
        session.commit.assert_not_called()

    def test_complete_not_falsified_thesis_ranks_normally(self):
        # A thesis passing every gate with a not-falsified latest run is
        # rank-eligible and appears in default results.
        row = self._eligible_row(id="nvda-complete")
        session = Session([Result(rows=[row])])
        ranked = list_ranked_opportunities(session)
        self.assertEqual([item["id"] for item in ranked], ["nvda-complete"])
        self.assertTrue(ranked[0]["eligible"])
        self.assertEqual(ranked[0]["blockers"], [])
        session.commit.assert_not_called()

    def test_default_hides_nebius_shape_despite_positive_score(self):
        # The audited incomplete shape — a positive 0.559 score but null
        # scenario probabilities, no structured risks, and an inconclusive
        # falsification run — must be absent from DEFAULT results.
        nebius = self._eligible_row(
            id="nebius",
            opportunity_score=0.559,
            eligibility_score=True,
            eligibility_scenarios=False,  # null probabilities / no legs
            eligibility_risks=False,  # no structured risk
            eligibility_evidence=True,
            eligibility_falsification=False,  # inconclusive run
        )
        session = Session([Result(rows=[nebius])])
        self.assertEqual(list_ranked_opportunities(session), [])
        # Only the explicit bounded opt-in exposes it, marked with blockers.
        included = list_ranked_opportunities(
            Session([Result(rows=[nebius])]), include_ineligible=True
        )
        self.assertEqual([row["id"] for row in included], ["nebius"])
        self.assertFalse(included[0]["eligible"])
        self.assertEqual(
            included[0]["blockers"], ["scenarios", "risks", "falsification"]
        )
        session.commit.assert_not_called()

    def test_each_eligibility_gate_is_enforced(self):
        # Every gate independently excludes a thesis from default results,
        # and the explicit opt-in reports the exact failing gate.
        cases = [
            ("status", {"status": "paused", "eligibility_status": False}),
            ("score", {"opportunity_score": 0.0, "eligibility_score": False}),
            ("scenarios", {"eligibility_scenarios": False}),
            ("risks", {"eligibility_risks": False}),
            ("evidence", {"eligibility_evidence": False}),
            ("falsification", {"eligibility_falsification": False}),
            ("actionability", {"eligibility_actionability": False}),
            ("opposition", {"eligibility_opposition": False}),
        ]
        for gate, overrides in cases:
            with self.subTest(gate=gate):
                row = self._eligible_row(id=f"blocked-{gate}", **overrides)
                default = list_ranked_opportunities(Session([Result(rows=[row])]))
                self.assertEqual(default, [])
                included = list_ranked_opportunities(
                    Session([Result(rows=[row])]), include_ineligible=True
                )
                self.assertEqual(len(included), 1)
                self.assertFalse(included[0]["eligible"])
                self.assertEqual(included[0]["blockers"], [gate])

    def test_zero_score_rows_require_explicit_opt_in(self):
        # Zero-score rows are reachable only through include_ineligible,
        # never through the default (even with the default 0.0 threshold).
        row = self._eligible_row(id="zero-score", opportunity_score=0.0)
        row["eligibility_score"] = False
        self.assertEqual(list_ranked_opportunities(Session([Result(rows=[row])])), [])
        included = list_ranked_opportunities(
            Session([Result(rows=[row])]), include_ineligible=True
        )
        self.assertEqual([item["id"] for item in included], ["zero-score"])
        self.assertFalse(included[0]["eligible"])
        self.assertEqual(included[0]["blockers"], ["score"])

    def test_null_score_rows_require_explicit_opt_in_and_never_rank_as_zero(self):
        # A never-evaluated thesis has a NULL opportunity score.  It is
        # absent from DEFAULT results, and the eligibility gate fails
        # truthfully ("score") instead of treating NULL as a favorable
        # zero — the opt-in reveals it marked ineligible.
        row = self._eligible_row(id="never-evaluated", opportunity_score=None)
        row["eligibility_score"] = False
        session = Session([Result(rows=[row])])
        self.assertEqual(list_ranked_opportunities(session), [])
        default_sql = session.calls[0][0]
        # The default filter admits only measured scores: NULL is never
        # coerced (no COALESCE) and never admitted as a zero.
        self.assertIn("t.opportunity_score >= :minimum_score", default_sql)
        self.assertNotIn("COALESCE(t.opportunity_score", default_sql)
        self.assertNotIn("OR t.opportunity_score IS NULL", default_sql)

        session = Session([Result(rows=[row])])
        included = list_ranked_opportunities(session, include_ineligible=True)
        self.assertEqual([item["id"] for item in included], ["never-evaluated"])
        self.assertFalse(included[0]["eligible"])
        self.assertEqual(included[0]["blockers"], ["score"])
        include_sql = session.calls[0][0]
        # The opt-in admits the NULL-score row explicitly, and every rank
        # metric pins NULLS LAST so an unknown always ranks after every
        # measured value, including zero.
        self.assertIn("OR t.opportunity_score IS NULL", include_sql)
        self.assertIn("ORDER BY (t.opportunity_score > 0) DESC NULLS LAST", include_sql)
        self.assertIn("t.expected_value DESC NULLS LAST", include_sql)
        self.assertIn("t.opportunity_score DESC NULLS LAST", include_sql)
        self.assertIn("t.confidence_score DESC NULLS LAST", include_sql)
        self.assertIn("t.catalyst_score DESC NULLS LAST", include_sql)
        self.assertIn("t.neglect_score DESC NULLS LAST", include_sql)
        session.commit.assert_not_called()

    def test_superseded_scenarios_cannot_satisfy_the_scenario_gate(self):
        # The scenario gate reads ACTIVE legs only (superseded_at IS NULL);
        # a stale complete scenario history can never make an incomplete
        # current thesis eligible.  The SQL restricts the gate to active
        # rows, and a row whose current-scenario gate fails is dropped by
        # default regardless of any other passing gate.
        stale_complete = self._eligible_row(
            id="stale",
            eligibility_scenarios=False,  # current legs incomplete
            eligibility_risks=True,
            eligibility_evidence=True,
            eligibility_falsification=True,
        )
        session = Session([Result(rows=[stale_complete])])
        self.assertEqual(list_ranked_opportunities(session), [])
        sql = session.calls[0][0]
        self.assertIn("AND s.superseded_at IS NULL", sql)
        self.assertIn("s.name IN ('bull', 'base', 'bear')", sql)
        session.commit.assert_not_called()

    def test_latest_falsification_gate_is_deterministic(self):
        # The falsification gate is the single latest run — most recently
        # started, tie-broken by run key — and only ``not_falsified``
        # passes; older runs or later non-not_falsified runs never qualify.
        row = self._eligible_row(id="latest-run")
        row["eligibility_falsification"] = False  # latest run inconclusive
        session = Session([Result(rows=[row])])
        self.assertEqual(list_ranked_opportunities(session), [])
        gate_sql = session.calls[0][0]
        self.assertIn("SELECT DISTINCT ON (f.thesis_id)", gate_sql)
        self.assertIn("ORDER BY f.thesis_id, f.started_at DESC,", gate_sql)
        self.assertIn("f.run_key", gate_sql)
        self.assertIn("WHERE latest_run.status = 'not_falsified'", gate_sql)
        session.commit.assert_not_called()

    def test_paused_closed_and_unopposed_theses_never_rank(self):
        cases = (
            ("paused", {"status": "paused", "eligibility_status": False}, ["status"]),
            ("closed", {"status": "closed", "eligibility_status": False}, ["status"]),
            (
                "unopposed",
                {"eligibility_opposition": False},
                ["opposition"],
            ),
        )
        for name, overrides, blockers in cases:
            with self.subTest(name=name):
                row = self._eligible_row(id=name, **overrides)
                self.assertEqual(
                    list_ranked_opportunities(Session([Result(rows=[row])])),
                    [],
                )
                included = list_ranked_opportunities(
                    Session([Result(rows=[row])]),
                    include_ineligible=True,
                )
                self.assertFalse(included[0]["eligible"])
                self.assertEqual(included[0]["blockers"], blockers)

    def test_include_ineligible_marks_every_row(self):
        # With the explicit opt-in, eligible and ineligible rows both come
        # back, each truthfully marked, and the WHERE filter is lifted.
        eligible = self._eligible_row(id="fine")
        blocked = self._eligible_row(
            id="blocked", symbol="AMD", direction="short", eligibility_evidence=False
        )
        session = Session([Result(rows=[eligible, blocked])])
        rows = list_ranked_opportunities(session, include_ineligible=True)
        self.assertEqual([row["id"] for row in rows], ["fine", "blocked"])
        self.assertTrue(rows[0]["eligible"])
        self.assertEqual(rows[0]["blockers"], [])
        self.assertFalse(rows[1]["eligible"])
        self.assertEqual(rows[1]["blockers"], ["evidence"])
        sql = session.calls[0][0]
        self.assertNotIn("AND eligibility.eligible", sql)
        session.commit.assert_not_called()

    def test_semantic_exposure_duplicates_collapse_but_opposition_remains(self):
        rows = [
            self._eligible_row(
                id="best-long",
                company="Nvidia Corp",
                symbol="NVDA",
                direction="long",
                horizon="months",
                opportunity_score=0.5,
            ),
            self._eligible_row(
                id="paraphrased-long",
                company="NVIDIA Corporation",
                symbol="nvda",
                direction="LONG",
                horizon="Months",
                opportunity_score=0.4,
            ),
            self._eligible_row(
                id="short-variant",
                company="Nvidia Corp",
                symbol="NVDA",
                direction="short",
                horizon="months",
                opportunity_score=0.3,
            ),
        ]
        session = Session([Result(rows=rows)])
        ranked = list_ranked_opportunities(session, limit=5)
        self.assertEqual([row["id"] for row in ranked], ["best-long", "short-variant"])
        self.assertEqual(session.calls[0][1]["limit"], 20)

    def test_group_ranking_uses_active_membership(self):
        # Group filtering must go through the versioned membership table:
        # a thesis whose investment_theses.group_id snapshot still points at
        # the group is excluded unless an ACTIVE membership row exists.
        session = Session([Result(rows=[self._eligible_row()])])
        rows = list_ranked_opportunities(session, limit=5, group_id=str(GROUP_ID))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], THESIS_ID)
        sql = session.calls[0][0]
        self.assertIn("investment_thesis_group_members m", sql)
        self.assertIn("m.group_id = CAST(:group_id AS UUID)", sql)
        self.assertIn("m.thesis_id = t.id", sql)
        self.assertIn("m.removed_at IS NULL", sql)
        self.assertIn("LIMIT :limit", sql)
        session.commit.assert_not_called()

    def test_limit_is_clamped(self):
        session = Session([Result(rows=[])])
        list_ranked_opportunities(session, limit=1000)
        self.assertEqual(session.calls[0][1]["limit"], 100)

    def test_ranking_group_identity_comes_from_active_membership(self):
        # Group identity must come from the versioned membership join, never
        # from the possibly stale investment_theses.group_id snapshot, so a
        # removed-and-re-added thesis ranks under its real group even when
        # the snapshot column lags or points elsewhere.
        session = Session(
            [
                Result(
                    rows=[
                        self._eligible_row(
                            group_id=GROUP_ID, group_name="NVDA bull vs bear"
                        )
                    ]
                )
            ]
        )
        rows = list_ranked_opportunities(session)
        self.assertEqual(rows[0]["group_id"], GROUP_ID)
        self.assertEqual(rows[0]["group_name"], "NVDA bull vs bear")
        sql = session.calls[0][0]
        self.assertIn("LEFT JOIN LATERAL", sql)
        self.assertIn("m.thesis_id = t.id", sql)
        self.assertIn("m.removed_at IS NULL", sql)
        self.assertNotIn("t.group_id", sql)
        self.assertNotIn("ON g.id = t.group_id", sql)
        session.commit.assert_not_called()


class GroupTournamentTests(unittest.TestCase):
    def test_load_group_tournament_shows_competing_theses(self):
        bull = {
            "id": THESIS_ID,
            "theme_id": THEME_ID,
            "company": "Nvidia Corp",
            "symbol": "NVDA",
            "claim": "AI capex compounds.",
            "variant_perception": None,
            "status": "active",
            "horizon": "multi_year",
            "direction": "long",
            "mechanism": "AI capex compounds.",
            "catalyst_summary": None,
            "confidence": 0.7,
            "origin": "fusion",
            "canonical_key": "k" * 64,
            "evidence_strength": 0.8,
            "contradiction_strength": 0.1,
            "neglect_score": 0.6,
            "catalyst_score": 0.5,
            "confidence_score": 0.65,
            "expected_value": 0.18,
            "expected_shortfall": 0.05,
            "opportunity_score": 0.72,
            "last_evaluated_at": NOW,
            "last_evidence_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
        bear = dict(bull)
        bear.update(
            {
                "id": BEAR_THESIS_ID,
                "claim": "AI capex peaks.",
                "direction": "short",
                "canonical_key": "m" * 64,
            }
        )
        session = Session(
            [
                Result(  # group row
                    first={
                        "id": GROUP_ID,
                        "name": "NVDA bull vs bear",
                        "description": None,
                        "status": "active",
                        "created_at": NOW,
                        "updated_at": NOW,
                    }
                ),
                Result(  # active members
                    rows=[
                        {"thesis_id": THESIS_ID, "added_at": NOW, "note": None},
                        {"thesis_id": BEAR_THESIS_ID, "added_at": NOW, "note": None},
                    ]
                ),
                Result(first=bull),  # bull thesis
                Result(rows=[{"relationship": "supports", "count": 2}]),
                Result(
                    first={
                        "version": 2,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.7,
                        "rationale": "Updated.",
                        "changed_by": "fusion",
                        "created_at": NOW,
                    }
                ),
                Result(
                    rows=[
                        {
                            "id": SCENARIO_ID,
                            "name": "Base",
                            "description": None,
                            "probability": 0.6,
                            "expected_return": 0.15,
                            "is_base_case": True,
                            "version": 1,
                            "created_at": NOW,
                        }
                    ]
                ),  # bull scenarios
                Result(rows=[]),  # bull forecasts
                Result(first=bear),  # bear thesis
                Result(rows=[]),
                Result(
                    first={
                        "version": 1,
                        "claim": "AI capex peaks.",
                        "variant_perception": None,
                        "confidence": None,
                        "rationale": "Initial candidate.",
                        "changed_by": "fusion",
                        "created_at": NOW,
                    }
                ),
                Result(rows=[]),  # bear scenarios
                Result(rows=[]),  # bear forecasts
                Result(rows=[]),  # outcomes
                Result(rows=[]),  # falsification runs
            ]
        )
        tournament = load_group_tournament(session, str(GROUP_ID))
        self.assertIsNotNone(tournament)
        self.assertEqual(tournament["group"]["name"], "NVDA bull vs bear")
        self.assertEqual(len(tournament["theses"]), 2)
        directions = {
            str(thesis["id"]): thesis["direction"] for thesis in tournament["theses"]
        }
        self.assertEqual(directions[str(THESIS_ID)], "long")
        self.assertEqual(directions[str(BEAR_THESIS_ID)], "short")
        bull_entry = tournament["theses"][0]
        self.assertEqual(bull_entry["evidence_counts"][0]["count"], 2)
        self.assertEqual(bull_entry["latest_version"]["version"], 2)
        # Scenario rows must carry the stored expected_return so point-in-time
        # valuation is stable at read time.
        self.assertEqual(bull_entry["scenarios"][0]["expected_return"], 0.15)
        self.assertEqual(bull_entry["scenarios"][0]["id"], SCENARIO_ID)
        scenario_sql = session.calls[5][0]
        self.assertIn("expected_return", scenario_sql)
        self.assertIn("superseded_at IS NULL", scenario_sql)
        self.assertIn("LIMIT :limit", scenario_sql)
        session.commit.assert_not_called()

    def test_missing_group_returns_none(self):
        session = Session([Result(first=None)])
        self.assertIsNone(load_group_tournament(session, str(GROUP_ID)))
        session.commit.assert_not_called()


class ListThesisGroupsTests(unittest.TestCase):
    def test_groups_carry_bounded_aggregates_and_deterministic_order(self):
        session = Session(
            [
                Result(
                    rows=[
                        {
                            "id": GROUP_ID,
                            "name": "NVDA bull vs bear",
                            "description": None,
                            "status": "active",
                            "created_at": NOW,
                            "updated_at": NOW,
                            "active_members": 2,
                            "long_count": 1,
                            "short_count": 1,
                            "neutral_count": 0,
                            "max_opportunity": 0.72,
                            "max_contradiction": 0.1,
                            "last_evaluation": NOW,
                        }
                    ]
                )
            ]
        )
        rows = list_thesis_groups(session, limit=25, status="active")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["active_members"], 2)
        self.assertEqual(row["long_count"], 1)
        self.assertEqual(row["short_count"], 1)
        self.assertEqual(row["max_opportunity"], 0.72)
        self.assertEqual(row["max_contradiction"], 0.1)
        self.assertEqual(row["last_evaluation"], NOW)
        sql, params = session.calls[0]
        self.assertIn("active_members", sql)
        self.assertIn("FILTER (WHERE t.direction = 'long')", sql)
        self.assertIn("MAX(t.opportunity_score)", sql)
        self.assertIn("MAX(t.contradiction_strength)", sql)
        self.assertIn("MAX(t.last_evaluated_at)", sql)
        self.assertIn("m.removed_at IS NULL", sql)
        self.assertIn("ORDER BY g.name, g.id", sql)
        self.assertIn("LIMIT :limit", sql)
        self.assertEqual(params["limit"], 25)
        self.assertEqual(params["status"], "active")
        session.commit.assert_not_called()

    def test_unsupported_status_rejected_and_limit_clamped(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported group status"):
            list_thesis_groups(session, status="deleted")
        session = Session([Result(rows=[])])
        list_thesis_groups(session, limit=1000)
        self.assertEqual(session.calls[0][1]["limit"], 100)
        session.commit.assert_not_called()


class ThesisDetailTests(unittest.TestCase):
    def _thesis_row(self):
        return {
            "id": THESIS_ID,
            "theme_id": THEME_ID,
            "company": "Nvidia Corp",
            "symbol": "NVDA",
            "claim": "AI capex compounds.",
            "variant_perception": None,
            "status": "active",
            "horizon": "multi_year",
            "direction": "long",
            "mechanism": "AI capex compounds.",
            "catalyst_summary": None,
            "confidence": 0.7,
            "origin": "fusion",
            "canonical_key": "k" * 64,
            "evidence_strength": 0.8,
            "contradiction_strength": 0.1,
            "neglect_score": 0.6,
            "catalyst_score": 0.5,
            "confidence_score": 0.65,
            "expected_value": 0.18,
            "expected_shortfall": 0.05,
            "opportunity_score": 0.72,
            "last_evaluated_at": NOW,
            "last_evidence_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }

    def test_detail_loads_all_bounded_children_with_expected_return(self):
        session = Session(
            [
                Result(first=self._thesis_row()),
                Result(rows=[{"version": 2, "claim": "AI capex compounds."}]),
                Result(
                    rows=[
                        {
                            "id": SCENARIO_ID,
                            "name": "Base",
                            "description": None,
                            "probability": 0.6,
                            "expected_return": 0.15,
                            "is_base_case": True,
                            "version": 1,
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "evidence_type": "source_claim",
                            "evidence_id": "claim:capex-2026",
                            "relationship": "supports",
                            "source_family": "filings",
                            "origin_key": "sec:10q:nvda",
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(rows=[{"id": uuid4(), "description": "Capex guide."}]),
                Result(rows=[{"id": uuid4(), "description": "Rates rise."}]),
                Result(
                    rows=[
                        {
                            "id": FORECAST_ID,
                            "forecast_key": "nvda-price-2027",
                            "as_of": NOW,
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "id": uuid4(),
                            "forecast_id": FORECAST_ID,
                            "forecast_key": "nvda-price-2027",
                            "status": "hit",
                            "measured_at": NOW,
                        }
                    ]
                ),
                Result(rows=[{"id": uuid4(), "snapshot_key": "eval-2026-08-06"}]),
                Result(rows=[{"id": RUN_ID, "run_key": "run-1", "status": "pending"}]),
                Result(rows=[{"id": GROUP_ID, "name": "NVDA bull vs bear"}]),
                Result(
                    rows=[
                        {
                            "position_id": POSITION_ID,
                            "link_type": "primary",
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(  # playbook versions
                    rows=[
                        {
                            "id": uuid4(),
                            "key": "nvda-capex-2027",
                            "version": 2,
                            "thesis_version": 1,
                            "catalyst": "Capex guidance update",
                            "horizon": "months",
                            "expected_at": NOW,
                            "event_types": ["macro_release"],
                            "trigger_conditions": ["Capex guide raised"],
                            "confirmation_conditions": [],
                            "invalidation_conditions": [],
                            "bull_scenario": None,
                            "base_scenario": None,
                            "bear_scenario": None,
                            "cited_evidence_refs": ["claim:capex-2026"],
                            "superseded_at": NOW,
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(  # playbook matches joined to market events
                    rows=[
                        {
                            "id": uuid4(),
                            "playbook_id": uuid4(),
                            "event_id": uuid4(),
                            "kind": "trigger",
                            "evidence_refs": ["claim:capex-2026"],
                            "assessment": {"confidence": 0.7},
                            "created_at": NOW,
                            "event_type": "macro_release",
                            "source": "fred",
                            "observed_at": NOW,
                        }
                    ]
                ),
            ]
        )
        detail = load_thesis_detail(session, str(THESIS_ID))
        self.assertIsNotNone(detail)
        self.assertEqual(detail["thesis"]["id"], THESIS_ID)
        self.assertEqual(detail["versions"][0]["version"], 2)
        self.assertEqual(detail["scenarios"][0]["expected_return"], 0.15)
        self.assertEqual(detail["evidence"][0]["relationship"], "supports")
        self.assertEqual(detail["catalysts"][0]["description"], "Capex guide.")
        self.assertEqual(detail["risks"][0]["description"], "Rates rise.")
        self.assertEqual(detail["forecasts"][0]["forecast_key"], "nvda-price-2027")
        self.assertEqual(detail["outcomes"][0]["status"], "hit")
        self.assertEqual(
            detail["opportunity_snapshots"][0]["snapshot_key"], "eval-2026-08-06"
        )
        self.assertEqual(detail["falsification_runs"][0]["run_key"], "run-1")
        self.assertEqual(detail["groups"][0]["name"], "NVDA bull vs bear")
        self.assertEqual(detail["positions"][0]["position_id"], POSITION_ID)
        self.assertEqual(detail["playbooks"][0]["key"], "nvda-capex-2027")
        self.assertEqual(detail["playbooks"][0]["catalyst"], "Capex guidance update")
        self.assertEqual(detail["playbook_matches"][0]["kind"], "trigger")
        self.assertEqual(detail["playbook_matches"][0]["event_type"], "macro_release")
        for index, sql in enumerate(call[0] for call in session.calls):
            if index == 0:
                continue  # core row is a single-row lookup
            self.assertIn("LIMIT :limit", sql)
            self.assertIn("CAST(:id AS UUID)", sql)
        self.assertIn("ORDER BY version DESC", session.calls[1][0])
        self.assertIn("expected_return", session.calls[2][0])
        self.assertIn("superseded_at IS NULL", session.calls[2][0])
        self.assertIn("ORDER BY o.measured_at DESC", session.calls[7][0])
        self.assertIn("ORDER BY captured_at DESC", session.calls[8][0])
        self.assertIn("removed_at IS NULL", session.calls[10][0])
        self.assertIn("removed_at IS NULL", session.calls[11][0])
        # Playbook children are bounded, deterministic, and never leak the
        # content fingerprint.
        playbook_sql = session.calls[12][0]
        self.assertIn("FROM investment_thesis_event_playbooks", playbook_sql)
        self.assertIn(
            "ORDER BY created_at DESC, playbook_key, version DESC", playbook_sql
        )
        self.assertNotIn("input_fingerprint", playbook_sql)
        match_sql = session.calls[13][0]
        self.assertIn("JOIN market_events e", match_sql)
        self.assertIn("ORDER BY m.created_at DESC, m.id", match_sql)
        self.assertEqual(session.calls[2][1]["limit"], 50)
        session.commit.assert_not_called()

    def test_missing_thesis_returns_none(self):
        session = Session([Result(first=None)])
        self.assertIsNone(load_thesis_detail(session, str(THESIS_ID)))
        session.commit.assert_not_called()


class ThesisDeskStatusTests(unittest.TestCase):
    def _results(
        self,
        *,
        outcome_rows=None,
        calibration_rows=None,
        transcript_rows=None,
        collection_rows=None,
        data_rows=None,
        job_rows=None,
        cost_row=None,
    ):
        data_rows = data_rows or [{"latest_timestamp": NOW, "acquired_at": NOW}] * 10
        if calibration_rows is None:
            calibration_rows = (
                []
                if outcome_rows == []
                else [
                    {
                        "bucket": 1,
                        "count": 1,
                        "mean_probability": 0.3,
                        "observed_hit_rate": 0.0,
                        "brier_score": 0.09,
                        "set_count": 1,
                        "overall_brier": 0.18,
                    },
                    {
                        "bucket": 3,
                        "count": 2,
                        "mean_probability": 0.7,
                        "observed_hit_rate": 1.0,
                        "brier_score": 0.09,
                        "set_count": 1,
                        "overall_brier": 0.18,
                    },
                ]
            )
        return [
            Result(first={"present": "investment_thesis_groups"}),
            Result(first={"total": 3}),  # thesis total
            Result(  # thesis status breakdown
                rows=[
                    {"status": "active", "count": 2},
                    {"status": "candidate", "count": 1},
                ]
            ),
            Result(first={"total": 1}),  # group total
            Result(rows=[{"status": "active", "count": 1}]),  # group status
            Result(first={"total": 2}),  # ranked theses
            Result(first={"total": 1}),  # linked theses
            Result(first={"total": 4}),  # evidence total
            Result(  # evidence relationships
                rows=[
                    {"relationship": "supports", "count": 3},
                    {"relationship": "contradicts", "count": 1},
                ]
            ),
            Result(first={"total": 5}),  # active forecasts
            Result(first={"total": 2}),  # matured forecasts
            Result(  # terminal outcome counts
                rows=outcome_rows
                if outcome_rows is not None
                else [
                    {"status": "hit", "count": 2},
                    {"status": "miss", "count": 1},
                    {"status": "inconclusive", "count": 1},
                ]
            ),
            Result(rows=calibration_rows),  # probability calibration bins
            Result(first={"latest": NOW}),  # latest evaluation
            Result(first={"latest": NOW}),  # latest falsification
            Result(  # autonomy model cost, UTC today only
                first=cost_row
                if cost_row is not None
                else {
                    "attempts": 4,
                    "known_cost_attempts": 3,
                    "unknown_cost_attempts": 1,
                    "today_usd": 0.06,
                    "latest_attempt_at": NOW,
                }
            ),
            Result(  # latest collection_log rows per allowlisted source
                rows=collection_rows
                if collection_rows is not None
                else [
                    {
                        "collector": "issuer_news",
                        "status": "success",
                        "completed_at": NOW,
                        "records_written": 12,
                    },
                    {
                        "collector": "sec_form4",
                        "status": "failed",
                        "completed_at": NOW,
                        "records_written": 0,
                    },
                    {
                        "collector": "filings",
                        "status": "success",
                        "completed_at": NOW,
                        "records_written": 2,
                    },
                ]
            ),
            *[Result(first=row) for row in data_rows[:2]],
            Result(  # transcript setup/timeout/available state counts
                rows=transcript_rows
                if transcript_rows is not None
                else [
                    {"state": "available", "count": 4},
                    {"state": "setup_required", "count": 1},
                    {"state": "timeout", "count": 2},
                ]
            ),
            *[Result(first=row) for row in data_rows[2:]],
            Result(  # autonomy jobs
                rows=job_rows
                if job_rows is not None
                else [
                    {
                        "id": uuid4(),
                        "job_type": "thesis_autonomy_run",
                        "state": "queued",
                        "priority": 80,
                        "dedupe_key": "thesis-autonomy:global",
                        "input_fingerprint": "f" * 64,
                        "not_before": NOW,
                        "attempt_count": 0,
                        "max_attempts": 3,
                        "correlation_id": uuid4(),
                        "created_at": NOW,
                        "started_at": None,
                        "completed_at": None,
                        "result_ref": None,
                        "payload": {"force": False},
                    }
                ]
            ),
        ]

    def test_status_reports_unavailable_when_schema_missing(self):
        session = Session([Result(first={"present": None})])
        status = thesis_desk_status(session)
        self.assertFalse(status["available"])
        self.assertEqual(status["theses"]["total"], 0)
        self.assertEqual(status["groups"]["by_status"], {})
        self.assertEqual(status["ranked_theses"], 0)
        self.assertEqual(status["evidence"]["by_relationship"], {})
        self.assertEqual(status["forecasts"], {"active": 0, "matured": 0})
        self.assertEqual(status["outcomes"], {"hit": 0, "miss": 0, "inconclusive": 0})
        self.assertIsNone(status["hit_rate"])
        self.assertEqual(
            status["calibration"],
            {"resolved_with_probability": 0, "brier_score": None, "bins": []},
        )
        self.assertEqual(status["model_cost"]["today_usd"], None)
        self.assertIsNone(status["latest_evaluation_at"])
        self.assertEqual(status["autonomy_jobs"], [])
        # Every allowlisted source reports an explicit unavailable state.
        self.assertEqual(
            set(status["sources"]),
            {
                "issuer_news",
                "issuer_transcripts",
                "public_equities",
                "cftc",
                "sec_form4",
                "finra_short_volume",
                "cboe_options",
                "company_expectations",
                "fred",
                "filings",
            },
        )
        self.assertEqual(
            status["sources"]["issuer_news"]["collection"]["status"],
            "unavailable",
        )
        self.assertFalse(status["sources"]["issuer_news"]["data"]["available"])
        self.assertEqual(
            status["sources"]["issuer_transcripts"]["transcript_states"], {}
        )
        session.commit.assert_not_called()

    def test_status_reports_counts_calibration_and_bounded_autonomy_jobs(self):
        session = Session(self._results())
        status = thesis_desk_status(session, limit=5)
        self.assertTrue(status["available"])
        self.assertEqual(status["theses"]["total"], 3)
        self.assertEqual(status["theses"]["by_status"], {"active": 2, "candidate": 1})
        self.assertEqual(status["groups"]["total"], 1)
        self.assertEqual(status["groups"]["by_status"], {"active": 1})
        self.assertEqual(status["ranked_theses"], 2)
        self.assertEqual(status["linked_theses"], 1)
        self.assertEqual(status["evidence"]["total"], 4)
        self.assertEqual(
            status["evidence"]["by_relationship"],
            {"supports": 3, "contradicts": 1},
        )
        self.assertEqual(status["forecasts"], {"active": 5, "matured": 2})
        self.assertEqual(status["outcomes"], {"hit": 2, "miss": 1, "inconclusive": 1})
        self.assertEqual(status["hit_rate"], 2 / 3)
        self.assertEqual(status["calibration"]["resolved_with_probability"], 1)
        self.assertAlmostEqual(status["calibration"]["brier_score"], 0.18)
        self.assertEqual(len(status["calibration"]["bins"]), 2)
        self.assertEqual(status["calibration"]["bins"][0]["lower"], 0.2)
        self.assertEqual(status["calibration"]["bins"][1]["upper"], 0.8)
        self.assertEqual(
            status["model_cost"],
            {
                "attempts": 4,
                "known_cost_attempts": 3,
                "unknown_cost_attempts": 1,
                "today_usd": 0.06,
                "latest_attempt_at": NOW,
            },
        )
        self.assertEqual(status["latest_evaluation_at"], NOW)
        self.assertEqual(status["latest_falsification_at"], NOW)
        # Collection status per source, with safe derived error classes and
        # explicit never_run for sources without any logged run.
        self.assertEqual(
            status["sources"]["issuer_news"]["collection"],
            {
                "status": "success",
                "finished_at": NOW,
                "records_written": 12,
                "error_class": None,
            },
        )
        self.assertEqual(
            status["sources"]["sec_form4"]["collection"]["status"], "failed"
        )
        self.assertEqual(
            status["sources"]["sec_form4"]["collection"]["error_class"], "error"
        )
        self.assertEqual(status["sources"]["fred"]["collection"]["status"], "never_run")
        self.assertEqual(
            status["sources"]["filings"]["collection"],
            {
                "status": "success",
                "finished_at": NOW,
                "records_written": 2,
                "error_class": None,
            },
        )
        self.assertTrue(status["sources"]["issuer_news"]["data"]["available"])
        self.assertEqual(
            status["sources"]["issuer_news"]["data"]["latest_timestamp"], NOW
        )
        self.assertEqual(
            status["sources"]["issuer_transcripts"]["transcript_states"],
            {"available": 4, "setup_required": 1, "timeout": 2},
        )
        self.assertEqual(len(status["autonomy_jobs"]), 1)
        self.assertEqual(status["autonomy_jobs"][0]["job_type"], "thesis_autonomy_run")
        self.assertFalse(status["autonomy_jobs"][0]["failed"])
        sql, params = session.calls[-1]
        self.assertIn("job_type = :job_type", sql)
        self.assertIn("ORDER BY created_at DESC, id DESC", sql)
        self.assertIn("LIMIT :limit", sql)
        self.assertEqual(params["job_type"], "thesis_autonomy_run")
        self.assertEqual(params["limit"], 5)
        # The autonomy job select never carries raw error text.
        self.assertNotIn("last_error", sql)
        schema_sql = session.calls[0][0]
        active_forecast_sql = session.calls[9][0]
        matured_forecast_sql = session.calls[10][0]
        self.assertIn("NOT EXISTS", active_forecast_sql)
        self.assertIn("investment_forecast_outcomes", active_forecast_sql)
        self.assertIn("EXISTS", matured_forecast_sql)
        self.assertIn("investment_forecast_outcomes", matured_forecast_sql)
        self.assertIn("to_regclass(:name)", schema_sql)
        calibration_sql = session.calls[12][0]
        self.assertIn("POWER(probability - actual, 2)", calibration_sql)
        self.assertIn("GROUP BY bucket", calibration_sql)
        cost_sql, cost_params = session.calls[15]
        self.assertIn("FROM generation_attempts", cost_sql)
        self.assertIn("processor = :processor", cost_sql)
        self.assertIn("created_at >= :today_start", cost_sql)
        self.assertNotIn("prompt_text", cost_sql)
        self.assertNotIn("raw_response", cost_sql)
        self.assertEqual(cost_params["processor"], "thesis_autonomy")
        collection_sql = session.calls[16][0]
        self.assertIn("DISTINCT ON (collector)", collection_sql)
        self.assertIn("FROM cycle_runs", collection_sql)
        self.assertIn("run_kind = 'filings'", collection_sql)
        self.assertIn("requested_component = 'investment_filings'", collection_sql)
        self.assertIn("JSONB_TYPEOF(summary->'ingested') = 'number'", collection_sql)
        self.assertIn("records_written", collection_sql)
        self.assertNotIn("error_message", collection_sql)
        session.commit.assert_not_called()

    def test_status_does_not_report_an_in_progress_filing_run_as_an_error(self):
        session = Session(
            self._results(
                collection_rows=[
                    {
                        "collector": "filings",
                        "status": "running",
                        "completed_at": None,
                        "records_written": 0,
                    }
                ]
            )
        )

        status = thesis_desk_status(session)

        self.assertEqual(
            status["sources"]["filings"]["collection"],
            {
                "status": "running",
                "finished_at": None,
                "records_written": 0,
                "error_class": None,
            },
        )
        session.commit.assert_not_called()

    def test_status_marks_empty_sources_and_null_hit_rate(self):
        session = Session(
            self._results(
                outcome_rows=[],
                transcript_rows=[],
                cost_row={
                    "attempts": 0,
                    "known_cost_attempts": 0,
                    "unknown_cost_attempts": 0,
                    "today_usd": None,
                    "latest_attempt_at": None,
                },
                data_rows=[
                    {"latest_timestamp": NOW, "acquired_at": NOW},
                    {"latest_timestamp": NOW, "acquired_at": NOW},
                    {"latest_timestamp": NOW, "acquired_at": NOW},
                    {"latest_timestamp": None, "acquired_at": None},
                    {"latest_timestamp": None, "acquired_at": None},
                    {"latest_timestamp": None, "acquired_at": None},
                    {"latest_timestamp": None, "acquired_at": None},
                    {"latest_timestamp": None, "acquired_at": None},
                    {"latest_timestamp": None, "acquired_at": None},
                    {"latest_timestamp": None, "acquired_at": None},
                ],
            )
        )
        status = thesis_desk_status(session)
        # No terminal hit/miss yet: the hit rate is null, never a fake zero.
        self.assertIsNone(status["hit_rate"])
        self.assertEqual(status["outcomes"], {"hit": 0, "miss": 0, "inconclusive": 0})
        # No terminal hit/miss yet: the hit rate is null, never a fake zero.
        self.assertIsNone(status["hit_rate"])
        self.assertEqual(status["calibration"]["resolved_with_probability"], 0)
        self.assertIsNone(status["calibration"]["brier_score"])
        # No known-cost attempts today: today_usd is null, never invented 0.
        self.assertEqual(
            status["model_cost"],
            {
                "attempts": 0,
                "known_cost_attempts": 0,
                "unknown_cost_attempts": 0,
                "today_usd": None,
                "latest_attempt_at": None,
            },
        )
        # Empty source tables are explicit, not silently missing.
        self.assertFalse(status["sources"]["cboe_options"]["data"]["available"])
        self.assertIsNone(status["sources"]["cboe_options"]["data"]["latest_timestamp"])
        self.assertTrue(status["sources"]["issuer_news"]["data"]["available"])
        self.assertEqual(
            status["sources"]["issuer_transcripts"]["transcript_states"], {}
        )
        session.commit.assert_not_called()

    def test_status_flags_failed_autonomy_jobs_without_error_text(self):
        session = Session(
            self._results(
                job_rows=[
                    {
                        "id": uuid4(),
                        "job_type": "thesis_autonomy_run",
                        "state": "failed_terminal",
                        "priority": 90,
                        "dedupe_key": "thesis-autonomy:global",
                        "input_fingerprint": "f" * 64,
                        "not_before": NOW,
                        "attempt_count": 3,
                        "max_attempts": 3,
                        "correlation_id": uuid4(),
                        "created_at": NOW,
                        "started_at": NOW,
                        "completed_at": NOW,
                        "result_ref": {"status": "failed"},
                        "payload": {"force": True},
                    }
                ]
            )
        )
        status = thesis_desk_status(session)
        self.assertTrue(status["autonomy_jobs"][0]["failed"])
        self.assertEqual(status["autonomy_jobs"][0]["result_ref"], {"status": "failed"})


if __name__ == "__main__":
    unittest.main()
