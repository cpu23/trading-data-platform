"""Tests for ranked opportunities, group tournaments, thesis detail, and desk status."""

import sys
import unittest
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
    GROUP_ID,
    NOW,
    POSITION_ID,
    RUN_ID,
    SCENARIO_ID,
    THEME_ID,
    THESIS_ID,
    Result,
    Session,
)
from thesis_fusion import (  # noqa: E402
    list_ranked_opportunities,
    list_thesis_groups,
    load_group_tournament,
    load_thesis_detail,
    thesis_desk_status,
)


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
