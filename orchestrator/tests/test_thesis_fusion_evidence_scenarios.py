"""Tests for evidence attachment, scenarios, forecasts, outcomes, runs, and position links."""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

ORCH_ROOT = Path(__file__).resolve().parents[1]
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from support_thesis_fusion import (  # noqa: E402
    BEAR_THESIS_ID,
    FORECAST_ID,
    LINK_ID,
    NOW,
    POSITION_ID,
    RUN_ID,
    SCENARIO_ID,
    THESIS_ID,
    Result,
    Session,
    desk_evidence_item,
)

from research_intelligence.contracts import EvidenceSignal  # noqa: E402
from thesis_fusion import (  # noqa: E402
    attach_evidence,
    freeze_forecast,
    link_position,
    record_falsification_run,
    record_forecast_outcome,
    unlink_position,
    update_falsification_run,
    upsert_scenario,
)


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
