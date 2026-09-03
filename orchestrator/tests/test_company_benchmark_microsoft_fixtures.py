"""Company-benchmark fixture regressions: Microsoft FY24-Q4 capacity economics,
quiet period negative control semantics, and finalized metric preservation.
"""

import json
import re
import sys
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402
from company_benchmark_support import (  # noqa: E402
    _finalized_for,
    judge_payload,
    narrative_payload,
)

import investment_service as service  # noqa: E402
from research_intelligence import company_benchmarks as cb  # noqa: E402
from research_intelligence import company_judging as judging  # noqa: E402
from research_intelligence import company_quality as cq  # noqa: E402
from research_intelligence.contracts import canonical_fingerprint  # noqa: E402

_QUIET_TRANSCRIPT_OUTCOME_BOUNDARY = datetime(2024, 7, 30, 23, 59, 59, tzinfo=UTC)

MSFT_EPISODE = (
    Path(__file__).resolve().parents[1]
    / "research_intelligence"
    / "company_episodes"
    / "msft_fy2024_q4_capacity_economics"
)
_AZURE_CC_CLAIM_ID = "msft_fy2025_q1_azure_growth_constant_currency"
_AI_POINTS_CLAIM_ID = "msft_fy2025_q1_azure_growth_from_ai_services_points"
# The FY25 Q1 call had completed by this conservative boundary; nothing about
# its content may be visible any earlier (full-call completion, not start).
_MSFT_FY25_Q1_AVAILABLE_AFTER = "2024-10-30T23:59:59Z"
# Transcript-only stamps must be conservative: the webcast ran past its
# 21:30Z press release, so transcript availability is end-of-day UTC.
_MSFT_TRANSCRIPT_AVAILABLE_AT = "2024-07-30T23:59:59Z"
_MSFT_PRESS_RELEASE_AVAILABLE_AT = "2024-07-30T21:30:00Z"


class MicrosoftFixtureRegressionTests(unittest.TestCase):
    """Reviewed MSFT FY24-Q4 fixture invariants, loaded through real seams.

    Every check reads semantic fields and relationships from the shipped
    ``producer.yaml``/``evaluator.yaml`` pair — never source line numbers —
    and each one fails on the pre-fix fixture.
    """

    @classmethod
    def setUpClass(cls):
        cls.producer = cb.load_producer_case(MSFT_EPISODE / "producer.yaml")
        cls.evaluator = cb.load_evaluator_case(
            MSFT_EPISODE / "evaluator.yaml", producer=cls.producer
        )

    def _outcome_row(self, outcome_id="msft_fy2025_q1_reported_results"):
        """The single matching frozen outcome mapping."""
        matches = [
            row
            for row in self.evaluator.later_outcomes
            if isinstance(row, Mapping) and row.get("outcome_id") == outcome_id
        ]
        self.assertEqual(len(matches), 1, f"missing later outcome {outcome_id}")
        return matches[0]

    def _observation(self, fragment):
        matches = [
            text
            for text in self.evaluator.expected_material_observations
            if fragment.casefold() in text.casefold()
        ]
        self.assertEqual(
            len(matches), 1, f"expected exactly one observation containing {fragment!r}"
        )
        return matches[0]

    def _claim(self, claim_id):
        matches = [
            claim
            for claim in self.evaluator.forbidden_hindsight
            if claim.claim_id == claim_id
        ]
        self.assertEqual(len(matches), 1, f"missing forbidden hindsight {claim_id}")
        return matches[0]

    def _transcript_cited_metrics(self):
        """Metrics whose provenance cites the earnings webcast transcript."""
        return {
            name: metric
            for name, metric in self.producer.deterministic_current.items()
            if isinstance(metric, Mapping)
            and str(metric.get("source_location", "")).lower().startswith("transcript")
        }

    # -- exact fixture loads ------------------------------------------------

    def test_shipped_fixture_pair_loads_through_production_seams(self):
        producer_raw = yaml.safe_load(
            (MSFT_EPISODE / "producer.yaml").read_text(encoding="utf-8")
        )
        evaluator_raw = yaml.safe_load(
            (MSFT_EPISODE / "evaluator.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(producer_raw["case_id"], "msft.fy2024.q4.capacity_economics")
        self.assertEqual(self.producer.case_id, "msft.fy2024.q4.capacity_economics")
        self.assertEqual(self.producer.document["symbol"], "MSFT")
        # Identity is the canonical normalized payload digest — never a
        # hash of the raw loader mapping, whose spelling is not part of
        # the producer identity contract.
        self.assertEqual(
            self.producer.fingerprint,
            canonical_fingerprint(
                cb.canonical_producer_fingerprint_payload(self.producer)
            ),
        )
        self.assertEqual(
            evaluator_raw["producer_fingerprint"], self.producer.fingerprint
        )
        request = cb.prepare_company_run(self.producer)
        self.assertIn("AMY HOOD", request.prompt)

    # -- expanded-basis Azure outcome identity ------------------------------

    def test_later_outcome_declares_expanded_basis_and_recast_comparator(self):
        outcome = self._outcome_row()
        metrics = outcome["metrics"]
        # The FY2025 metric is an expanded Azure definition with recast
        # history; the identity must be carried structurally, not only in
        # prose, or graders can silently score the old guide against it.
        self.assertEqual(
            metrics.get("azure_metric_basis"),
            "expanded_FY2025_azure_and_other_cloud_services",
        )
        # Official recast FY24-Q4 comparators under the FY2025 basis.
        self.assertEqual(
            metrics.get("recast_fy2024_q4_azure_growth_yoy_gaap_percent"), 34
        )
        self.assertEqual(
            metrics.get("recast_fy2024_q4_azure_growth_yoy_constant_currency_percent"),
            35,
        )
        basis_text = (
            f"{outcome.get('metric_basis', '')} {outcome.get('basis_warning', '')}"
        ).lower()
        self.assertIn("recast", basis_text)
        self.assertIn("expanded", basis_text)
        self.assertIn("28%", basis_text)
        self.assertIn("29%", basis_text)
        self.assertIn(
            "never",
            basis_text,
            "basis warning must prohibit direct old-guide scoring",
        )
        self.assertIn("34%", basis_text)
        self.assertIn("35%", basis_text)
        description = str(outcome.get("description", ""))
        for forbidden_old_guide in ("beat", "exceeded", "met guidance", "missed"):
            self.assertNotIn(
                forbidden_old_guide,
                description.lower(),
                "later outcome must present results, not old-basis realization",
            )

    def test_new_hindsight_guard_is_enforced_by_the_hard_gate(self):
        import investment_service as service

        qualitative = {
            name: {"present": False, "strength": "none", "evidence": ""}
            for name in service.QUALITATIVE_NAMES
        }
        base_payload = narrative_payload(
            summary="",
            thesis=(
                "Holds while Azure growth stays within the guided range; "
                "below-guide growth or delayed capacity additions invalidate it."
            ),
            counter_thesis=(
                "The thesis fails if growth misses guidance or capacity slips."
            ),
            document_type="earnings_transcript",
            industry="Software, Cloud & Communications",
            qualitative=qualitative,
        )

        baseline = _finalized_for(self.producer, base_payload)
        # Isolate the post-finalization hard gate: live replay correctly rejects
        # these unbound numbers before a finalized result can exist.

        def gate_report(summary):
            facts = dict(baseline.facts)
            facts["summary"] = summary
            finalized = baseline._replace(facts=facts)
            return cq.run_company_hard_gates(
                self.producer, self.evaluator, finalized
            )

        leaks = {
            "azure_cc_34": "Looking to Q1 FY2025, Azure growth in constant currency should be about 34%.",
            "ai_points_12": "In Q1 FY2025 Azure growth from AI services is roughly 12 percentage points.",
        }
        for label, leak in leaks.items():
            with self.subTest(leak=label):
                report = gate_report(leak)
                codes = {failure.code for failure in report.failures}
                self.assertIn("forbidden_company_claim_present", codes)
                self.assertFalse(report.passed)
        clean_summary = (
            "In FY2024 Q4, Azure and other cloud services revenue grew 29%; "
            "in FY2024 Q4, AI services contributed 8 percentage points to "
            "Azure growth while demand remained higher than available capacity. "
            "Q4 consumption trends continue through the first half."
        )
        # Every authored material number outside evidence quotes needs an
        # exact per-token, per-target, per-source ledger row. The allowed
        # 29%/8-point pair is NOT exempt from the numeric-claims contract:
        # both rows bind to transcript-provenance deterministic facts.
        payload = dict(base_payload, summary=clean_summary)
        payload["numeric_claims"] = [
            {
                "claim_id": "azure_growth_gaap",
                "path": "summary",
                "value": "29%",
                "metric": "Azure and other cloud services revenue growth",
                "period": "FY2024-Q4",
                "unit": "percent",
                "currency": None,
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current."
                    "azure_and_other_cloud_services_growth_gaap_percent"
                ),
            },
            {
                "claim_id": "azure_ai_points",
                "path": "summary",
                "value": "8",
                "metric": "Azure growth from AI services contribution",
                "period": "FY2024-Q4",
                "unit": "percentage_points",
                "currency": None,
                "source_kind": "fact",
                "fact_path": (
                    "deterministic_current.azure_growth_from_ai_services_points"
                ),
            },
        ]
        finalized = _finalized_for(self.producer, payload)
        clean_report = cq.run_company_hard_gates(
            self.producer, self.evaluator, finalized
        )
        self.assertTrue(clean_report.passed, clean_report.failures)
        cc_claim = self._claim(_AZURE_CC_CLAIM_ID)
        ai_claim = self._claim(_AI_POINTS_CLAIM_ID)
        self.assertEqual(cc_claim.value, 34)
        self.assertEqual(ai_claim.value, 12)
        folded_aliases = lambda aliases: " | ".join(aliases).casefold()  # noqa: E731
        self.assertIn("constant currency", folded_aliases(cc_claim.metric_aliases))
        self.assertIn("ai services", folded_aliases(ai_claim.metric_aliases))
        for claim in (cc_claim, ai_claim):
            periods = folded_aliases(claim.period_aliases)
            self.assertIn("q1 fy2025", periods)
            self.assertIn("next quarter", periods)
            self.assertIn("september quarter", periods)
        expected_boundary = datetime.fromisoformat(_MSFT_FY25_Q1_AVAILABLE_AFTER)
        existing_ids = {
            "msft_fy2025_q1_azure_revenue_growth",
            "msft_fy2025_q1_cloud_gross_margin",
            "msft_fy2025_q1_capex_including_finance_leases",
            _AZURE_CC_CLAIM_ID,
            _AI_POINTS_CLAIM_ID,
        }
        self.assertEqual(
            {claim.claim_id for claim in self.evaluator.forbidden_hindsight},
            existing_ids,
        )
        for claim in self.evaluator.forbidden_hindsight:
            self.assertEqual(
                claim.available_after,
                expected_boundary,
                "every hindsight row shares the conservative full-call boundary",
            )
        outcome = self._outcome_row()
        self.assertEqual(
            datetime.fromisoformat(outcome["available_after"]), expected_boundary
        )
        # Conservative means strictly after the event start, never at it.
        self.assertGreater(
            expected_boundary,
            datetime.fromisoformat("2024-10-30T21:30:00+00:00"),
        )
        # The FY25-Q1 boundary shares the conservative end-of-call shape of
        # the quiet control's transcript-outcome boundary (23:59:59Z).
        self.assertEqual(
            expected_boundary,
            _QUIET_TRANSCRIPT_OUTCOME_BOUNDARY.replace(
                year=expected_boundary.year, month=10
            ),
        )

    # -- transcript availability conservatism --------------------------------

    def test_transcript_availability_is_conservative_end_of_day_not_event_start(self):
        document_available = datetime.fromisoformat(
            str(self.producer.document["available_at"])
        )
        as_of = self.producer.as_of
        press_release_time = datetime.fromisoformat(
            f"{self.producer.document['release_date']}T21:30:00+00:00"
        )
        # Full-call completion: after the press release, still before as_of.
        self.assertGreater(document_available, press_release_time)
        self.assertLessEqual(document_available, as_of)
        self.assertEqual(
            document_available,
            datetime.fromisoformat(_MSFT_TRANSCRIPT_AVAILABLE_AT),
        )
        transcript_metrics = self._transcript_cited_metrics()
        self.assertTrue(transcript_metrics)
        stamps = {}
        for name, metric in transcript_metrics.items():
            stamp = datetime.fromisoformat(str(metric["available_at"]))
            self.assertGreater(
                stamp,
                press_release_time,
                f"{name} cites the transcript but carries a pre-call stamp",
            )
            self.assertLessEqual(stamp, as_of)
            stamps[name] = stamp
        self.assertEqual(
            set(stamps.values()),
            {datetime.fromisoformat(_MSFT_TRANSCRIPT_AVAILABLE_AT)},
            "every transcript-cited metric shares one conservative stamp",
        )
        press_release_metrics = [
            name
            for name, metric in self.producer.deterministic_current.items()
            if isinstance(metric, Mapping)
            and str(metric.get("source_location", "")).lower().startswith("press release")
        ]
        self.assertTrue(press_release_metrics)
        for name in press_release_metrics:
            stamp = datetime.fromisoformat(
                str(self.producer.deterministic_current[name]["available_at"])
            )
            self.assertEqual(
                stamp,
                datetime.fromisoformat(_MSFT_PRESS_RELEASE_AVAILABLE_AT),
                f"{name} is a press-release fact and keeps the release stamp",
            )

    # -- Activision single-contributor semantics ------------------------------

    def test_activision_observation_names_distinct_contributors_without_sole_cause(self):
        growth_quality = self._observation("Growth-quality divergence")
        distortion = self._observation("Activision distortion")
        combined = f"{growth_quality} {distortion}"
        # Distinct segment contributors are named explicitly.
        self.assertIn("more personal computing", combined.lower())
        self.assertIn("gaming", combined.lower())
        self.assertIn("opex", combined.lower())
        self.assertRegex(distortion, r"48/58")
        self.assertIn("3 points", combined)
        self.assertRegex(
            re.sub(r"[\s\-]+", "", combined),
            r"\$0\.06",
        )
        sole_cause_patterns = (
            r"\ball\b[^.]*\bbecause\b[^.]*activision",
            r"\bsolely\b",
            r"\bentirely\b[^.]*activision",
            r"\bexplained entirely\b",
            r"\bonly cause\b",
        )
        for pattern in sole_cause_patterns:
            self.assertIsNone(
                re.search(pattern, combined, re.IGNORECASE),
                f"Activision must not be framed as the sole cause: {pattern}",
            )
        self.assertTrue(
            re.search(r"not quantified|unquantified", combined, re.IGNORECASE),
            "residual growth drivers must be marked as not quantified",
        )

    # -- capex basis separation ----------------------------------------------

    def test_capex_observation_separates_cash_and_finance_lease_bases(self):
        capex_observation = self._observation("Capex and cash-flow divergence")
        lowered = capex_observation.lower()
        self.assertIn("including finance leases", lowered)
        self.assertIn("$19b", lowered.replace(" ", ""))
        self.assertIn("$13.9b", lowered.replace(" ", ""))
        self.assertIn("$23.3b", lowered.replace(" ", ""))
        inference_marked = [
            text
            for text in self.evaluator.expected_material_observations
            if "inference:" in text.lower()
        ]
        self.assertEqual(
            len(inference_marked),
            1,
            "exactly the interpretive capex observation carries Inference:",
        )
        self.assertIs(inference_marked[0], capex_observation)
        for authored in (
            *self.evaluator.expected_material_observations,
            self.evaluator.strongest_counter_thesis,
            *(str(trap) for trap in self.evaluator.known_traps),
        ):
            self.assertNotIn("cash conversion", authored.lower())
            self.assertNotIn("earnings conversion", authored.lower())
        # The monetization-timing observation is interpretive but phrased as
        # an established unknown rather than a marked inference; only the
        # capex divergence carries the Inference: marker.
        monetization = self._observation("Monetization uncertainty")
        self.assertNotIn("inference:", monetization.lower())
        self.assertIn("not established", monetization.lower())

        current = self.producer.deterministic_current
        capex_metric = current["capital_expenditures_including_finance_leases"]
        cash_metric = current["cash_paid_for_property_and_equipment"]
        self.assertEqual(capex_metric["value"], 19.0)
        self.assertEqual(cash_metric["value"], 13.9)
        self.assertNotEqual(capex_metric["value"], cash_metric["value"])
        traps_blob = json.dumps(cb.plain_copy(self.evaluator.known_traps)).lower()
        self.assertIn("19 billion", traps_blob)
        self.assertIn("fiscal-year totals", traps_blob)

    def test_cross_basis_azure_trap_withholds_future_result_details(self):
        outcome = self._outcome_row()
        metrics = outcome["metrics"]
        reported_growth = (
            metrics["azure_growth_yoy_gaap_percent"],
            metrics["azure_growth_yoy_constant_currency_percent"],
        )
        self.assertEqual(reported_growth, (33, 34))
        self.assertGreater(
            datetime.fromisoformat(outcome["available_after"]),
            self.producer.as_of,
            "reported growth must remain a dated post-cutoff outcome",
        )
        self.assertEqual(
            (
                metrics["recast_fy2024_q4_azure_growth_yoy_gaap_percent"],
                metrics[
                    "recast_fy2024_q4_azure_growth_yoy_constant_currency_percent"
                ],
            ),
            (34, 35),
            "same-basis recast details belong in the dated outcome",
        )
        outcome_blob = json.dumps(cb.plain_copy(outcome), sort_keys=True).casefold()
        for retained_detail in (
            "33%",
            "34%",
            "expanded fy2025",
            "recast fy2024-q4",
        ):
            self.assertIn(
                retained_detail,
                outcome_blob,
                "dated later_outcomes must retain the withheld result context",
            )

        traps_blob = json.dumps(
            cb.plain_copy(self.evaluator.known_traps), sort_keys=True
        ).casefold()
        self.assertIn("same official metric definition", traps_blob)
        self.assertIn("same-basis comparator", traps_blob)
        self.assertIn("28%", traps_blob)
        self.assertIn("29%", traps_blob)
        for reported_value in reported_growth:
            with self.subTest(reported_value=reported_value):
                self.assertIsNone(
                    re.search(
                        rf"(?<![\d.]){reported_value}(?:\.0)?\s*"
                        r"(?:%|percent)?(?![\d.])",
                        traps_blob,
                    ),
                    "a static known trap must not reveal a post-cutoff result",
                )
        for future_detail in (
            outcome["outcome_id"],
            "expanded_fy2025_azure_and_other_cloud_services",
            "expanded fy2025",
            "recast fy2024-q4",
            "recast fy2024 q4",
            "12 points from ai services",
            "71%",
            "$20 billion",
            "$14.9 billion",
        ):
            self.assertNotIn(
                str(future_detail).casefold(),
                traps_blob,
                "future result and recast specifics belong only in later_outcomes",
            )

    def test_blind_judge_rubric_withholds_future_evaluator_knowledge(self):
        qualitative = {
            name: {"present": False, "strength": "none", "evidence": ""}
            for name in service.QUALITATIVE_NAMES
        }
        payload = narrative_payload(
            summary=(
                "Capacity constraints keep the timing of demand conversion "
                "uncertain."
            ),
            thesis=(
                "Demand remains supply constrained; weakening demand invalidates "
                "the thesis."
            ),
            counter_thesis="Weakening demand would invalidate the thesis.",
            document_type="earnings_transcript",
            industry="Software, Cloud & Communications",
            qualitative=qualitative,
        )
        finalized = _finalized_for(self.producer, payload)
        requests = judging.build_blind_judge_requests(
            self.producer,
            self.evaluator,
            finalized,
            "msft-hindsight-isolation-salt",
        )
        self.assertEqual(len(requests), len(judging.JUDGE_ROLES))

        outcome = self._outcome_row()
        reported_growth = {
            outcome["metrics"]["azure_growth_yoy_gaap_percent"],
            outcome["metrics"]["azure_growth_yoy_constant_currency_percent"],
        }
        self.assertEqual(reported_growth, {33, 34})
        forbidden_claim_ids = {
            claim.claim_id for claim in self.evaluator.forbidden_hindsight
        }
        self.assertTrue(forbidden_claim_ids)

        for request in requests:
            with self.subTest(role=request.role):
                packet_json = (
                    request.prompt.split("<case_packet", 1)[1]
                    .split(">\n", 1)[1]
                    .split("\n</case_packet>", 1)[0]
                )
                packet = json.loads(packet_json)
                rubric = packet["evaluation_rubric"]
                rubric_blob = json.dumps(rubric, sort_keys=True).casefold()

                def nested_keys(node):
                    if isinstance(node, Mapping):
                        for key, value in node.items():
                            yield str(key)
                            yield from nested_keys(value)
                    elif isinstance(node, list):
                        for value in node:
                            yield from nested_keys(value)

                self.assertTrue(
                    {"later_outcomes", "forbidden_hindsight"}.isdisjoint(
                        nested_keys(rubric)
                    )
                )
                self.assertEqual(
                    rubric["known_traps"],
                    cb.plain_copy(self.evaluator.known_traps),
                    "judges receive only the sanitized static trap rubric",
                )
                self.assertNotIn(str(outcome["outcome_id"]).casefold(), rubric_blob)
                for claim_id in forbidden_claim_ids:
                    self.assertNotIn(claim_id.casefold(), rubric_blob)
                for reported_value in reported_growth:
                    self.assertIsNone(
                        re.search(
                            rf"(?<![\d.]){reported_value}(?:\.0)?\s*"
                            r"(?:%|percent)?(?![\d.])",
                            rubric_blob,
                        ),
                        "serialized judge rubric disclosed a post-cutoff result",
                    )
                for future_detail in (
                    "expanded_fy2025_azure_and_other_cloud_services",
                    "expanded fy2025",
                    "recast fy2024-q4",
                    "recast fy2024 q4",
                    "12 points from ai services",
                    "71%",
                    "$20 billion",
                    "$14.9 billion",
                ):
                    self.assertNotIn(future_detail, rubric_blob)


QUIET_EPISODE = (
    Path(__file__).resolve().parents[1]
    / "research_intelligence"
    / "company_episodes"
    / "msft_pre_fy2024_q4_quiet_period"
)
# Q4 facts became public in two waves on 2024-07-30: the press release
# (21:30Z) made every reported result knowable, while the transcript-only
# remarks (Amy Hood's prepared remarks) completed at end of day (23:59:59Z).
_QUIET_PRESS_RELEASE_BOUNDARY = datetime(2024, 7, 30, 21, 30, 0, tzinfo=UTC)

_QUIET_AS_OF = datetime(2024, 7, 29, 23, 59, 59, tzinfo=UTC)
_QUIET_RELEASE_CHAIN_START = datetime(2024, 7, 30, tzinfo=UTC)
_QUIET_NOTICE_STAMP = datetime(2024, 7, 16, 7, 0, 0, tzinfo=UTC)
# Earliest instant any Q4 artifact is treated as available anywhere in the
# paired episode family (sibling press-release stamp).
_QUIET_SIBLING_Q4_AVAILABLE_AT = datetime(2024, 7, 30, 21, 30, 0, tzinfo=UTC)
# Core FY24-Q4 result tuples: the hindsight rows must encode exactly these
# values, and none of them may be reachable from the producer half.
_QUIET_CORE_RESULT_TUPLES = {
    "msft_q4_revenue_billions": 64.7,
    "msft_q4_diluted_eps": 2.95,
    "msft_q4_azure_growth_gaap_percent": 29,
    "msft_q4_azure_growth_constant_currency_percent": 30,
    "msft_q4_cloud_gross_margin_percent": 69,
    "msft_q4_capex_including_finance_leases_billions": 19,
    "msft_q4_operating_cash_flow_billions": 37.2,
    "msft_q4_free_cash_flow_billions": 23.3,
}
# Outcome-only phrases: none may appear anywhere in the producer packet.
# Guidance-range numbers (the standing 30%-31% CC range) are deliberately not
# listed: they were officially knowable before the cutoff; Q4 outcomes are
# what must never appear.
_QUIET_PRODUCER_LEAK_PHRASES = (
    "gross margin",
    "european",
    "low end",
    "activision",
    "useful life",
    "accounting estimate",
    "higher than available capacity",
    "points from ai services",
)
_QUIET_PRODUCER_LEAK_NUMBERS = frozenset(
    {"64.7", "64727", "2.95", "69", "19", "37.2", "23.3", "13.9"}
)
_QUIET_NOTICE_URL_SUFFIX = (
    "2024/07/16/microsoft-announces-quarterly-earnings-release-date-60/"
)


class MicrosoftQuietPeriodNegativeControlTests(unittest.TestCase):
    """Pre-FY2024-Q4 quiet-period negative-control semantics.

    The control pairs with ``msft.fy2024.q4.capacity_economics`` but inverts
    its cut: the SAME July-30 release documents are producer evidence there
    and evaluator-only hindsight here. A correct producer synthesis is a
    concise insufficient-evidence/no-material-change response with explicit
    unknowns — never invented economics, and never confused with a judge
    abstention, because the producer contract has no abstention field while
    evaluators stay capable of scoring it.
    """

    @classmethod
    def setUpClass(cls):
        cls.producer = cb.load_producer_case(QUIET_EPISODE / "producer.yaml")
        cls.evaluator = cb.load_evaluator_case(
            QUIET_EPISODE / "evaluator.yaml", producer=cls.producer
        )
        cls.producer_raw = yaml.safe_load(
            (QUIET_EPISODE / "producer.yaml").read_text(encoding="utf-8")
        )

    @staticmethod
    def _quiet_payload(summary, thesis=(
        "No material change is supported by the window; uncertainty stays "
        "explicit until the scheduled results are released."
    )):
        """Compliant quiet-period response: empty optional arrays, no digits."""
        qualitative = {
            name: {"present": False, "strength": "none", "evidence": ""}
            for name in service.QUALITATIVE_NAMES
        }
        return narrative_payload(
            summary=summary,
            thesis=thesis,
            counter_thesis="New material disclosures could change the thesis.",
            document_type="other",
            industry="Software, Cloud & Communications",
            qualitative=qualitative,
        )

    def _finalized_summary(self, summary):
        baseline_payload = self._quiet_payload(
            "Insufficient evidence for material change: the producer window "
            "contains only scheduling and routine logistics notices, so no "
            "financial, operating, capacity, or guidance outcome is knowable "
            "as of the cutoff."
        )
        recorded = cb.recorded_executor_output(
            json.dumps(baseline_payload),
            {"model": "recorded-model", "tokens_total": 10},
        )
        baseline = cb.finalize_recorded_company_run(recorded, self.producer)
        # Keep finalization and its deterministic analysis valid, then vary only
        # the authored fact surface inspected by the hindsight hard gate.
        facts = dict(baseline.facts)
        facts["summary"] = summary
        return baseline._replace(facts=facts)

    def _pit_stamps(self):
        """Every declared PIT timestamp in the shipped producer payload."""
        stamps = {}

        def recurse(node, path):
            if isinstance(node, Mapping):
                for key, item in node.items():
                    child = f"{path}.{key}"
                    if key in cb._PIT_TIMESTAMP_KEYS and item is not None:
                        parsed = datetime.fromisoformat(str(item))
                        stamps[child] = parsed
                    else:
                        recurse(item, child)
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    recurse(item, f"{path}[{index}]")

        recurse(self.producer_raw, "producer")
        return stamps

    def _producer_strings(self):
        strings = []

        def recurse(node):
            if isinstance(node, Mapping):
                for item in node.values():
                    recurse(item)
            elif isinstance(node, list):
                for item in node:
                    recurse(item)
            elif isinstance(node, str):
                strings.append(node)

        recurse(self.producer_raw)
        return strings

    def _gate_report_for_summary(self, summary):
        finalized = self._finalized_summary(summary)
        return cq.run_company_hard_gates(self.producer, self.evaluator, finalized)

    # -- discovery and strict loading ---------------------------------------

    def test_shipped_negative_control_pair_loads_through_production_seams(self):
        raw = self.producer_raw
        self.assertEqual(raw["case_id"], "msft.pre_fy2024_q4.quiet_period_no_change")
        self.assertEqual(self.producer.case_id, "msft.pre_fy2024_q4.quiet_period_no_change")
        self.assertNotEqual(self.producer.case_id, "msft.fy2024.q4.capacity_economics")
        self.assertEqual(self.producer.document["symbol"], "MSFT")
        self.assertEqual(self.producer.as_of, _QUIET_AS_OF)
        self.assertEqual(self.evaluator.fixture_version, self.producer.fixture_version)
        self.assertEqual(
            self.producer.fingerprint,
            canonical_fingerprint(
                cb.canonical_producer_fingerprint_payload(self.producer)
            ),
        )
        self.assertEqual(
            raw["market_inputs"]["consensus_estimates"]["status"],
            "unavailable_as_of_producer_cutoff",
        )

    # -- the cut: as_of precedes every Q4 artifact ----------------------------

    def test_as_of_strictly_precedes_every_q4_artifact_instant(self):
        as_of = self.producer.as_of
        self.assertLess(as_of, _QUIET_RELEASE_CHAIN_START)
        self.assertLess(as_of, _QUIET_SIBLING_Q4_AVAILABLE_AT)
        outcomes = [
            row
            for row in self.evaluator.later_outcomes
            if isinstance(row, Mapping)
            and row.get("outcome_id") == "msft_fy2024_q4_reported_results"
        ]
        self.assertEqual(len(outcomes), 1)
        outcome = outcomes[0]
        self.assertEqual(
            datetime.fromisoformat(str(outcome["available_after"])),
            _QUIET_TRANSCRIPT_OUTCOME_BOUNDARY,
        )
        self.assertGreater(
            datetime.fromisoformat(str(outcome["available_after"])),
            _QUIET_RELEASE_CHAIN_START,
        )
        # The transcript-completed outcome is never earlier than the
        # press-release instant: knowledge arrives in two waves, not one.
        self.assertGreater(
            _QUIET_TRANSCRIPT_OUTCOME_BOUNDARY, _QUIET_PRESS_RELEASE_BOUNDARY
        )
        stamps = self._pit_stamps()
        self.assertTrue(stamps, "producer packet declares no point-in-time stamps")
        for path, stamp in stamps.items():
            with self.subTest(path=path):
                self.assertIsNotNone(stamp.utcoffset(), f"{path} is naive")
                self.assertLessEqual(stamp, as_of, f"{path} carries future knowledge")
        # The scheduling notice itself must survive: it is the one legitimate
        # superficial update that proves the window is not perfectly inert.
        self.assertIn(_QUIET_NOTICE_STAMP, set(stamps.values()))

    # -- scheduling trap present, Q4 results absent ---------------------------

    def test_producer_packet_holds_scheduling_trap_without_q4_results(self):
        normalized_excerpt = service._normalize_grounding_text(self.producer.excerpt)
        self.assertIn(
            "will publish fiscal year 2024 fourth-quarter financial results "
            "after the close of the market",
            normalized_excerpt,
        )
        self.assertIn("july 30, 2024", normalized_excerpt)
        self.assertIn("2:30 p.m. pacific time", normalized_excerpt)
        notice_items = [
            item
            for item in self.producer.news_items
            if str(item.get("url", "")).endswith(_QUIET_NOTICE_URL_SUFFIX)
        ]
        self.assertEqual(len(notice_items), 1)
        notice = notice_items[0]
        self.assertEqual(
            datetime.fromisoformat(str(notice["published_at"])),
            _QUIET_NOTICE_STAMP,
        )
        self.assertIn("scheduling", str(notice["summary"]).casefold())
        self.assertEqual(len(self.producer.news_items), 3)
        # Inertness is structural: no deterministic crutches exist to lean on.
        self.assertEqual(dict(self.producer.deterministic_current), {})
        self.assertEqual(dict(self.producer.deterministic_prior), {})
        self.assertEqual(dict(self.producer.prior_facts), {})
        self.assertEqual(self.producer.prior_count, 0)
        self.assertIsNone(self.producer.previous_state)
        # No realized Q4 value or outcome remark anywhere in the producer half.
        blob = service._normalize_grounding_text(" ".join(self._producer_strings()))
        for phrase in _QUIET_PRODUCER_LEAK_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, blob)
        allowed_numbers = set(cq._numeric_tokens(blob))
        leaked = _QUIET_PRODUCER_LEAK_NUMBERS & allowed_numbers
        self.assertEqual(leaked, set(), f"Q4 result values leaked to producer: {leaked}")

    # -- consensus policy ------------------------------------------------------

    def test_consensus_unavailability_is_declared_with_checked_at_bound(self):
        consensus = self.producer.market_inputs["consensus_estimates"]
        self.assertEqual(consensus["status"], "unavailable_as_of_producer_cutoff")
        self.assertEqual(
            set(consensus), {"status", "note", "consensus_availability_checked_at"}
        )
        # No expectation-gap framing and no embedded estimate values anywhere
        # in the consensus block: unavailable means no numbers and no beats.
        for _, text in cq._iter_strings(cb.plain_copy(self.producer.market_inputs)):
            self.assertNotIn("beat", text.casefold())
            self.assertNotIn("miss", text.casefold())
            self.assertNotIn("%", text)

    # -- evaluator-only hindsight covers the core result tuples ---------------

    def test_hindsight_rows_cover_core_result_tuples_as_evaluator_only(self):
        claims = {claim.claim_id: claim for claim in self.evaluator.forbidden_hindsight}
        self.assertEqual(set(claims), set(_QUIET_CORE_RESULT_TUPLES))
        for claim_id, value in _QUIET_CORE_RESULT_TUPLES.items():
            with self.subTest(claim_id=claim_id):
                claim = claims[claim_id]
                self.assertEqual(claim.value, value)
                self.assertEqual(claim.available_after, _QUIET_PRESS_RELEASE_BOUNDARY)
                self.assertGreater(claim.available_after, self.producer.as_of)
                self.assertIn(
                    "fy2024-q4",
                    [alias.casefold() for alias in claim.period_aliases],
                )
        self.assertEqual(self.evaluator.required_material_evidence, ())
        observations_blob = " ".join(
            self.evaluator.expected_material_observations
        ).casefold()
        self.assertIn("no material update", observations_blob)
        self.assertIn("insufficient-evidence", observations_blob)
        unknowns_blob = " ".join(self.evaluator.expected_unknowns).casefold()
        self.assertIn("30%-31%", unknowns_blob)
        self.assertIn("capital expenditures including finance leases", unknowns_blob)
        # Outcome-only remarks live exclusively on the evaluator side.
        outcome = [
            row
            for row in self.evaluator.later_outcomes
            if isinstance(row, Mapping)
            and row.get("outcome_id") == "msft_fy2024_q4_reported_results"
        ][0]
        metrics = outcome["metrics"]
        for name, expected in (
            ("revenue_usd_billions", 64.7),
            ("diluted_eps_usd", 2.95),
            ("capex_including_finance_leases_usd_billions", 19),
            ("microsoft_cloud_gross_margin_percent", 69),
        ):
            self.assertEqual(metrics.get(name), expected)
        outcome_blob = json.dumps(cb.plain_copy([outcome])).casefold()
        self.assertIn("european", outcome_blob)
        self.assertIn("demand higher than available capacity", outcome_blob)

    # -- insufficiency synthesis clears every hard gate ------------------------

    def test_insufficient_evidence_synthesis_clears_all_hard_gates(self):
        report = self._gate_report_for_summary(
            "Insufficient evidence for material change: the producer window "
            "contains only scheduling and routine logistics notices, so no "
            "financial, operating, capacity, or guidance outcome is knowable "
            "as of the cutoff."
        )
        self.assertTrue(report.passed, f"unexpected gate failures: {report.failures}")
        self.assertEqual(report.failures, ())

    # -- leaked Q4 tuples are caught deterministically -------------------------

    def test_leaked_q4_result_tuples_fail_the_hindsight_gate(self):
        leaks = {
            "revenue": (
                "Revenue was $64.7 billion for fiscal year 2024 fourth quarter."
            ),
            "diluted_eps": (
                "Earnings per share was $2.95 in fiscal year 2024 fourth quarter."
            ),
            "azure_gaap": (
                "Azure revenue growth was 29% in fiscal year 2024 fourth quarter."
            ),
            "azure_constant_currency": (
                "Azure growth in constant currency was 30% in fiscal year 2024 "
                "fourth quarter."
            ),
            "cloud_gross_margin": (
                "Microsoft Cloud gross margin was 69% in fiscal year 2024 "
                "fourth quarter."
            ),
            "capex_including_finance_leases": (
                "Capital expenditures including finance leases were $19 billion "
                "in fiscal year 2024 fourth quarter."
            ),
            "operating_cash_flow": (
                "Cash flow from operations was $37.2 billion in fiscal year 2024 "
                "fourth quarter."
            ),
            "free_cash_flow": (
                "Free cash flow was $23.3 billion in fiscal year 2024 fourth "
                "quarter."
            ),
        }
        for label, sentence in leaks.items():
            with self.subTest(leak=label):
                report = self._gate_report_for_summary(sentence)
                codes = {failure.code for failure in report.failures}
                self.assertIn("forbidden_company_claim_present", codes)
                self.assertFalse(report.passed)

    # -- producer insufficiency is scored, never a judge abstention ------------

    def test_producer_insufficiency_requires_scores_not_judge_abstention(self):
        finalized = self._finalized_summary(
            "Insufficient evidence for material change: only scheduling and "
            "logistics notices are available before the cutoff."
        )
        requests = judging.build_blind_judge_requests(
            self.producer, self.evaluator, finalized, "company-run-blind-salt"
        )
        self.assertEqual(len(requests), len(judging.JUDGE_ROLES))
        results = [
            judging.parse_judge_result(request, judge_payload(request))
            for request in requests
        ]
        for result in results:
            self.assertFalse(result.abstained)
            self.assertIsNone(result.abstention_reason)
        gate_report = cq.run_company_hard_gates(
            self.producer, self.evaluator, finalized
        )
        panel = judging.aggregate_judge_panel(requests, results, gate_report)
        criteria = {criterion.criterion: criterion for criterion in panel.criteria}
        self.assertTrue(criteria["no_abstentions"].passed)
        self.assertEqual(panel.abstained_roles, ())
        self.assertTrue(panel.passed)
        # Judges grade against as_of knowledge alone: unknowns travel into the
        # packet, hindsight fields and outcomes never do.
        prompts = "\n".join(request.prompt for request in requests)
        self.assertIn('"expected_unknowns"', prompts)
        self.assertIn(self.producer.as_of.isoformat(), prompts)
        for withheld in (
            "64.7",
            "$19 billion",
            "European geos",
            '"later_outcomes"',
            '"forbidden_hindsight"',
        ):
            self.assertNotIn(withheld, prompts)
        # The judge surface stays abstention-capable even though this control
        # expects scored verdicts: producer insufficiency is graded, not a
        # reason to refuse scoring.
        schema_blob = json.dumps(cb.plain_copy(requests[0].schema))
        self.assertIn('"abstained"', schema_blob)
        self.assertIn('"abstention_reason"', schema_blob)

    # -- dispatch preparation cannot expose evaluator fields -------------------

    def test_prepare_company_run_request_exposes_no_evaluator_fields(self):
        request = cb.prepare_company_run(self.producer)
        blob = (
            request.prompt
            + json.dumps(cb.plain_copy(request.schema))
            + request.fingerprint
        )
        for field in sorted(cb._EVALUATOR_ONLY_KEYS):
            self.assertNotIn(field, blob)
        for value in (
            "64.7",
            "$19 billion",
            "European geos",
            "69%",
            "37.2",
            "23.3",
            "capacity_economics",
        ):
            self.assertNotIn(value, blob)
        # Positive anchors: the dispatch request really is built from this
        # case, and the scheduling notice is the visible producer evidence.
        self.assertIn("Microsoft Corporation", request.prompt)
        self.assertIn("after the close of the market", request.prompt)
        self.assertEqual(request.schema_name, "investment_report_narrative_v7")
        self.assertTrue(request.strict)


class MicrosoftFinalizedMetricsRegressionTests(unittest.TestCase):
    """The supplied Microsoft deterministic metrics must survive production
    finalization verbatim: never rewritten to null/missing, cash PP&E kept
    distinct from finance-lease-inclusive capex, explicit FCF authoritative,
    segment-scoped metrics retained with provenance."""

    @classmethod
    def setUpClass(cls):
        cls.producer = cb.load_producer_case(MSFT_EPISODE / "producer.yaml")

    def _finalize(self):
        import investment_service as service

        qualitative = {
            name: {"present": False, "strength": "none", "evidence": ""}
            for name in service.QUALITATIVE_NAMES
        }
        payload = narrative_payload(
            summary="Cloud strength drove fourth quarter results.",
            thesis="Holds while Azure growth stays within the guided range.",
            counter_thesis="Below-guide growth would invalidate the thesis.",
            document_type="earnings_transcript",
            industry="Software, Cloud & Communications",
            qualitative=qualitative,
        )
        return _finalized_for(self.producer, payload)

    def test_supplied_metrics_are_not_rewritten_to_null_or_missing(self):
        finalized = self._finalize()
        analysis_metrics = finalized.analysis["metrics"]
        fact_metrics = finalized.facts["metrics"]
        # Raw facts must survive verbatim under their own names. Analysis
        # metrics surface cash PP&E through its canonical capex slot: alias
        # names resolve into the canonical fact instead of duplicating keys,
        # while supplemental facts keep their own names.
        supplied_values = {
            "revenue": 64_727,
            "operating_cash_flow": 37.2,
            "cash_paid_for_property_and_equipment": 13.9,
            "capital_expenditures_including_finance_leases": 19.0,
            "free_cash_flow": 23.3,
            "microsoft_cloud_revenue": 36.8,
        }
        canonical_slot = {"cash_paid_for_property_and_equipment": "capex"}
        for name, expected in supplied_values.items():
            with self.subTest(metric=name):
                self.assertIsNotNone(fact_metrics[name]["value"])
                self.assertAlmostEqual(
                    float(fact_metrics[name]["value"]), float(expected), places=6
                )
                analysis_name = canonical_slot.get(name, name)
                self.assertIsNotNone(analysis_metrics[analysis_name])
                self.assertAlmostEqual(
                    float(analysis_metrics[analysis_name]["value"]),
                    float(expected),
                    places=6,
                )

    def test_cash_capex_and_finance_lease_capex_stay_distinct(self):
        metrics = self._finalize().analysis["metrics"]
        cash_capex = metrics["capex"]["value"]
        lease_capex = metrics[
            "capital_expenditures_including_finance_leases"
        ]["value"]
        self.assertAlmostEqual(float(cash_capex), 13.9, places=6)
        self.assertAlmostEqual(float(lease_capex), 19.0, places=6)
        self.assertNotEqual(cash_capex, lease_capex)

    def test_reported_free_cash_flow_is_authoritative_and_segment_scopes_hold(self):
        metrics = self._finalize().analysis["metrics"]
        # Reported $23.3B FCF (OCF $37.2B minus CASH PP&E $13.9B); the
        # finance-lease-inclusive figure must never enter this arithmetic.
        self.assertAlmostEqual(float(metrics["fcf"]["value"]), 23.3, places=6)
        cloud_gm = metrics["microsoft_cloud_gross_margin_percent"]
        self.assertAlmostEqual(float(cloud_gm["value"]), 69.0, places=6)
        self.assertEqual(cloud_gm["unit"], "percent")
        ai_points = metrics["azure_growth_from_ai_services_points"]
        self.assertAlmostEqual(float(ai_points["value"]), 8.0, places=6)
        self.assertTrue(str(ai_points.get("source_location", "")).startswith("transcript"))
