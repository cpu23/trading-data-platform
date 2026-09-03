"""Tests for thesis evaluation, opportunity snapshotting, and scoring models."""

import math
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from support_thesis_fusion import (  # noqa: E402
    LINK_ID,
    NOW,
    THESIS_ID,
    Result,
    Session,
    evidence_row,
)

from research_intelligence.contracts import (  # noqa: E402
    EvidenceSignal,
    Scenario,
)
from thesis_fusion import (  # noqa: E402
    append_opportunity_snapshot,
    evaluate_thesis,
)
from thesis_scoring import (  # noqa: E402
    CatalystSignal,
    assess_evidence,
    assess_opportunity,
    calculate_neglect,
    catalyst_readiness,
    scenario_valuation,
)


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

    def test_explicit_zero_submetrics_stay_numeric_in_snapshot(self):
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
            expected_value=0.0,
            expected_shortfall=0.0,
            confidence_score=0.0,
            neglect_score=0.0,
            catalyst_score=0.0,
            evidence_strength=0.0,
            contradiction_strength=0.0,
        )
        self.assertTrue(appended)
        params = session.calls[2][1]
        self.assertEqual(params["confidence_score"], 0.0)
        self.assertEqual(params["neglect_score"], 0.0)
        self.assertEqual(params["catalyst_score"], 0.0)
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

    def test_evaluate_preserves_legitimate_numeric_zero_submetrics(self):
        signal = self._strong_support_signal(
            "claim:a", "filings:nvda", {"statement": "Capex up."}
        )
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(
                    rows=[
                        evidence_row(
                            evidence_fingerprint=signal.evidence_fingerprint
                        )
                    ]
                ),
                Result(
                    rows=[
                        {
                            "description": "Capex guide raise",
                            "state": "missed",
                            "expected_at": NOW,
                        }
                    ]
                ),
                Result(rows=self._complete_scenario_rows()),
                Result(rows=[]),  # no market bars
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
            attention=1.0,
            crowding=1.0,
        )
        self.assertEqual(result["neglect"]["neglect"], 0.0)
        self.assertEqual(result["catalyst"]["readiness"], 0.0)
        self.assertIsNotNone(result["evidence"]["confidence"])
        update_params = session.calls[5][1]
        self.assertEqual(update_params["neglect_score"], 0.0)
        self.assertEqual(update_params["catalyst_score"], 0.0)
        self.assertIsNotNone(update_params["confidence_score"])
        snapshot_params = session.calls[8][1]
        self.assertEqual(snapshot_params["neglect_score"], 0.0)
        self.assertEqual(snapshot_params["catalyst_score"], 0.0)
        self.assertIsNotNone(snapshot_params["confidence_score"])
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
