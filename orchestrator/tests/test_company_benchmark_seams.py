"""Company-benchmark fixture seam coverage: producer/evaluator splitting,
leak rejection, pure dispatch/finalization replay, recursive immutability,
and temporal key validation.
"""

import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import investment_service as service  # noqa: E402
from company_benchmark_support import (  # noqa: E402
    EXCERPT,
    NEWS_ITEM,
    _frozen_structure_problems,
    _iter_mutation_attempts,
    _put,
    evaluator_raw,
    narrative_payload,
    producer_raw,
    write_yaml,
)
from research_intelligence import company_benchmarks as cb  # noqa: E402
from research_intelligence.contracts import canonical_fingerprint  # noqa: E402


class CompanyBenchmarkSeamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def _producer_file(self, raw, name="producer.yaml"):
        return write_yaml(self.directory, name, raw)

    def _load_producer(self, **overrides):
        raw = producer_raw()
        raw.update(overrides)
        return cb.load_producer_case(self._producer_file(raw))

    def _load_evaluator(self, producer, **overrides):
        raw = evaluator_raw(producer.fingerprint, **overrides)
        return cb.load_evaluator_case(
            self._producer_file(raw, "evaluator.yaml"), producer=producer
        )

    def test_valid_producer_loads_with_stable_fingerprint(self):
        case = self._load_producer()
        self.assertEqual(case.case_id, "mu.fy25.q3")
        self.assertEqual(case.as_of, datetime(2026, 3, 31, tzinfo=UTC))
        self.assertIsInstance(case.document, MappingProxyType)
        self.assertRegex(case.fingerprint, r"[a-f0-9]{64}")
        # Identity is the canonical normalized payload digest, not a hash
        # of whatever spelling the loader happened to accept.
        self.assertEqual(
            case.fingerprint,
            canonical_fingerprint(cb.canonical_producer_fingerprint_payload(case)),
        )
        reordered = cb.load_producer_case(
            self._producer_file(dict(reversed(list(producer_raw().items()))))
        )
        self.assertEqual(reordered.fingerprint, case.fingerprint)

    def test_evaluator_pairs_only_with_matching_producer(self):
        producer = self._load_producer()
        self._load_evaluator(producer)
        mismatches = [
            ({"producer_fingerprint": "f" * 64}, "does not match the producer case"),
            ({"producer_fingerprint": "nothex"}, "SHA-256 hex"),
            ({"case_id": "other.case"}, "case_id does not match"),
            ({"fixture_version": 2}, "fixture_version does not match"),
        ]
        for overrides, fragment in mismatches:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError) as ctx:
                    self._load_evaluator(producer, **overrides)
                self.assertIn(fragment, str(ctx.exception))

    def test_nested_evaluator_only_keys_are_rejected(self):
        nested_cases = [
            ({"extraction": {"later_outcomes": []}}, "$.extraction.later_outcomes"),
            (
                {"deterministic_current": {"notes": {"known_traps": []}}},
                "deterministic_current.notes.known_traps",
            ),
        ]
        for overrides, path in nested_cases:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    self._load_producer(**overrides)
                self.assertIn("evaluator-only field", str(ctx.exception))
                self.assertIn(path.split(".")[-1], str(ctx.exception))

    def test_future_naive_or_missing_pit_timestamps_are_rejected(self):
        base_document = producer_raw()["document"]
        future_document = dict(base_document, available_at="2026-04-01T00:00:00Z")
        naive_document = dict(base_document, available_at="2026-03-30T00:00:00")
        missing_document = {
            key: value for key, value in base_document.items() if key != "available_at"
        }
        future_news = [
            dict(NEWS_ITEM, published_at="2026-04-01T00:00:00Z"),
        ]
        missing_news = [
            {key: value for key, value in NEWS_ITEM.items() if key != "published_at"},
        ]
        rejections = [
            ({"document": future_document}, "after as_of"),
            ({"document": naive_document}, "timezone-aware"),
            ({"document": missing_document}, "missing required keys"),
            ({"news_items": future_news}, "after as_of"),
            ({"news_items": missing_news}, "at or before as_of"),
        ]
        for overrides, fragment in rejections:
            with self.subTest(fragment=fragment):
                with self.assertRaises(ValueError) as ctx:
                    self._load_producer(**overrides)
                self.assertIn(fragment, str(ctx.exception))

    def test_overlong_strings_are_rejected_not_truncated(self):
        with self.assertRaises(ValueError) as ctx:
            self._load_producer(excerpt="x" * (cb._MAX_EXCERPT_CHARS + 1))
        self.assertIn("bounded text", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self._load_producer(case_id="a" * 121)
        self.assertIn("exceeds 120 characters", str(ctx.exception))
        producer = self._load_producer()
        with self.assertRaises(ValueError) as ctx:
            self._load_evaluator(
                producer,
                expected_material_observations=["y" * 501],
            )
        self.assertIn("exceeds 500 characters", str(ctx.exception))

    def test_forbidden_claim_rows_are_strictly_validated(self):
        producer = self._load_producer()
        valid_row = {
            "claim_id": "capex_q1",
            "metric_aliases": ["capex"],
            "value": 20,
            "period_aliases": ["Q1 FY2025"],
            "available_after": "2026-06-01T00:00:00Z",
        }
        self._load_evaluator(producer, forbidden_hindsight=[valid_row])
        rejections = [
            ("extra key", [dict(valid_row, source_url="https://x")]),
            (
                "missing key",
                [{k: v for k, v in valid_row.items() if k != "value"}],
            ),
            (
                "duplicate claim_id",
                [
                    valid_row,
                    dict(valid_row, metric_aliases=["capital expenditures"]),
                ],
            ),
            ("empty aliases", [dict(valid_row, metric_aliases=[])]),
            ("nonnumeric value", [dict(valid_row, value="twenty billion")]),
            ("nonfinite value", [dict(valid_row, value=float("inf"))]),
            ("bool value", [dict(valid_row, value=True)]),
            (
                "naive available_after",
                [dict(valid_row, available_after="2026-06-01T00:00:00")],
            ),
        ]
        for label, rows in rejections:
            with self.subTest(rejection=label):
                with self.assertRaises(ValueError) as ctx:
                    self._load_evaluator(producer, forbidden_hindsight=rows)
                if label == "extra key" or label == "missing key":
                    self.assertIn(
                        "forbidden company claim row is invalid",
                        str(ctx.exception),
                    )
                elif label == "duplicate claim_id":
                    self.assertIn(
                        "duplicate forbidden company claim_id", str(ctx.exception)
                    )
                elif label == "nonfinite value":
                    self.assertIn("must be finite", str(ctx.exception))
                elif label == "bool value":
                    self.assertIn("must be numeric", str(ctx.exception))
                else:
                    self.assertIn("company benchmark", str(ctx.exception))

    def test_prepare_company_run_carries_only_producer_data(self):
        raw = producer_raw()
        case = self._load_producer()
        request = cb.prepare_company_run(case)
        direct = service.build_investment_analysis_request(
            raw["document"],
            raw["excerpt"],
            raw["news_items"],
            raw["deterministic_current"],
            raw["deterministic_prior"],
        )
        self.assertEqual(request.schema_name, "investment_report_narrative_v7")
        self.assertTrue(request.strict)
        self.assertEqual(request.fingerprint, direct.fingerprint)
        self.assertEqual(request.relationship_facts, direct.relationship_facts)
        self.assertEqual(
            request.material_relationships,
            direct.material_relationships,
        )
        self.assertIn(EXCERPT, request.prompt)
        self.assertIn("Micron Technology", request.prompt)

    def test_v7_schema_exposes_only_epistemic_risk_and_catalyst_keys(self):
        request = cb.prepare_company_run(self._load_producer())
        self.assertEqual(service.INVESTMENT_ANALYSIS_RULE_VERSION, "7")
        self.assertEqual(request.schema_name, "investment_report_narrative_v7")
        properties = request.schema["properties"]
        self.assertIn("relationship_reconciliations", properties)
        self.assertIn("counter_thesis", properties)
        self.assertIn("counter_thesis", request.schema["required"])
        self.assertEqual(properties["counter_thesis"]["type"], "string")
        defs = request.schema.get("$defs", {})
        materiality_props = defs["MaterialityTopicItem"]["properties"]
        for topic in (
            "status",
            "observation",
            "implication",
            "evidence",
        ):
            self.assertIn(topic, materiality_props)
        risk = defs["RiskItem"]
        catalyst = defs["CatalystItem"]
        self.assertEqual(
            set(risk["required"]),
            {
                "sourced_observation",
                "inference",
                "epistemic_state",
                "uncertainty",
                "likelihood",
                "impact",
                "mitigation",
            },
        )
        self.assertIn("evidence", risk["properties"])
        self.assertEqual(
            set(catalyst["required"]),
            {
                "trigger",
                "expected_outcome",
                "horizon",
                "epistemic_state",
                "uncertainty",
            },
        )
        self.assertIn("evidence", catalyst["properties"])
        for legacy_key in (
            "risk",
            "catalyst",
            "description",
            "probability",
            "timeframe",
        ):
            self.assertNotIn(legacy_key, risk["properties"])
            self.assertNotIn(legacy_key, catalyst["properties"])

    def test_recorded_payload_finalizes_through_production_seams(self):
        case = self._load_producer()
        payload = narrative_payload()
        recorded = cb.recorded_executor_output(
            json.dumps(payload),
            {"model": "recorded-model", "tokens_total": 120},
        )
        self.assertIsInstance(recorded.provenance, MappingProxyType)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        real_validate = service._validated_investment_facts
        real_finalize = service.finalize_investment_analysis
        request = cb.prepare_company_run(case)
        expected_normalized = real_validate(
            canonical,
            excerpt=EXCERPT,
            document_metadata=dict(case.document),
            news_items=[dict(NEWS_ITEM)],
            deterministic_current={},
            deterministic_prior={},
            relationship_facts=request.relationship_facts,
            material_relationships=request.material_relationships,
        )
        with (
            patch.object(
                service,
                "_validated_investment_facts",
                side_effect=lambda *args, **kwargs: real_validate(*args, **kwargs),
            ) as validate_mock,
            patch.object(
                service,
                "finalize_investment_analysis",
                side_effect=lambda *args, **kwargs: real_finalize(*args, **kwargs),
            ) as finalize_mock,
        ):
            result = cb.finalize_recorded_company_run(recorded, case)

        validate_mock.assert_called_once_with(
            canonical,
            excerpt=EXCERPT,
            document_metadata=dict(case.document),
            news_items=[dict(NEWS_ITEM)],
            deterministic_current={},
            deterministic_prior={},
            relationship_facts=request.relationship_facts,
            material_relationships=request.material_relationships,
        )
        finalize_mock.assert_called_once_with(
            expected_normalized,
            document=dict(case.document),
            deterministic_current={},
            deterministic_prior={},
            market_inputs={},
            relationship_facts=request.relationship_facts,
            material_relationships=request.material_relationships,
            stored_previous_facts={},
            previous_state=None,
            prior_count=0,
            news_items=[dict(NEWS_ITEM)],
            extraction={"report_text_source": "stored_document"},
        )
        self.assertIsInstance(result, service.InvestmentFinalizedAnalysis)
        self.assertEqual(result.analysis["summary"], payload["summary"])
        self.assertEqual(
            result.analysis["news_context"][0]["title"], NEWS_ITEM["title"]
        )

    def test_recorded_quarterly_fcf_replay_stays_ineligible_after_public_revaluation(
        self,
    ):
        def reported_metric(value, period):
            return {
                "value": value,
                "unit": "usd_millions",
                "period": period,
                "evidence": ["reported result"],
            }

        case = self._load_producer(
            deterministic_current={
                "free_cash_flow": reported_metric(120, "FY2025-Q3"),
                "revenue": reported_metric(1_200, "FY2025-Q3"),
            },
            deterministic_prior={
                "revenue": reported_metric(1_000, "FY2024-Q3"),
            },
        )
        recorded = cb.recorded_executor_output(json.dumps(narrative_payload()))

        replayed = cb.finalize_recorded_company_run(recorded, case)

        self.assertEqual(replayed.analysis["metrics"]["fcf"]["value"], 120)
        self.assertEqual(replayed.analysis["metrics"]["fcf"]["period"], "FY2025-Q3")
        dcf = replayed.analysis["valuation"]["dcf"]
        self.assertEqual(dcf["status"], "enterprise_value_only")
        self.assertIsNone(dcf.get("reason"))
        self.assertIsNotNone(dcf["assumptions"]["inferred_growth"])
        self.assertEqual(len(dcf["forecast"]), 5)
        self.assertIsNotNone(dcf["enterprise_value"])
        self.assertIsNone(dcf["equity_value"])
        self.assertIsNone(dcf["per_share"])
        self.assertEqual(dcf["sensitivity"]["status"], "enterprise_value_only")
        self.assertNotEqual(dcf["sensitivity"]["wacc_terminal_grid"], [])
        self.assertTrue(dcf["sensitivity"]["drivers"])
        self.assertIsNotNone(dcf["sensitivity"]["range"]["enterprise_value_min"])

        now = datetime.now(UTC)
        public_payload = dict(
            replayed.analysis,
            public_price_timestamp=now,
            public_price_created_at=now,
            public_price_close=50,
            public_price_source="public_equities",
            public_price_metadata={"currency": "USD"},
        )
        revalued = service._attach_public_market_data(public_payload, replayed.facts)

        public_dcf = revalued["valuation"]["dcf"]
        self.assertEqual(public_dcf["status"], "enterprise_value_only")
        self.assertIsNone(public_dcf.get("reason"))
        self.assertEqual(public_dcf["assumptions"]["inferred_growth"], 0.05)
        self.assertEqual(len(public_dcf["forecast"]), 5)
        self.assertIsNotNone(public_dcf["enterprise_value"])
        self.assertEqual(public_dcf["sensitivity"]["status"], "enterprise_value_only")
        self.assertNotEqual(public_dcf["sensitivity"]["wacc_terminal_grid"], [])
        self.assertIsNotNone(public_dcf["sensitivity"]["range"]["enterprise_value_min"])

    def test_recorded_annual_and_ttm_fcf_replays_produce_valuation_outputs(self):
        def reported_metric(value, period, unit="usd_millions"):
            return {
                "value": value,
                "unit": unit,
                "period": period,
                "evidence": ["reported result"],
            }

        cases = (
            ("annual", "FY2025", "FY2024", "annual", "annual_fcf"),
            ("ttm", "TTM-2025", "TTM-2024", "ttm", "ttm_fcf"),
        )
        for label, current_period, prior_period, _fcf_basis, _growth_basis in cases:
            with self.subTest(basis=label):
                producer = self._load_producer(
                    deterministic_current={
                        "free_cash_flow": reported_metric(120, current_period),
                        "shares_outstanding": reported_metric(
                            10, current_period, "million shares"
                        ),
                        "net_debt": reported_metric(20, current_period),
                    },
                    deterministic_prior={
                        "free_cash_flow": reported_metric(100, prior_period),
                    },
                    market_inputs={
                        "discount_rate": 0.10,
                        "terminal_growth": 0.03,
                    },
                )
                recorded = cb.recorded_executor_output(json.dumps(narrative_payload()))

                replayed = cb.finalize_recorded_company_run(recorded, producer)

                dcf = replayed.analysis["valuation"]["dcf"]
                self.assertEqual(dcf["status"], "calculated")
                self.assertIsNone(dcf.get("reason"))
                self.assertEqual(dcf["assumptions"]["inferred_growth"], 0.20)
                self.assertEqual(len(dcf["forecast"]), 5)
                self.assertIsNotNone(dcf["enterprise_value"])
                self.assertIsNotNone(dcf["equity_value"])
                self.assertIsNotNone(dcf["per_share"])
                self.assertEqual(dcf["sensitivity"]["status"], "calculated")
                self.assertNotEqual(dcf["sensitivity"]["wacc_terminal_grid"], [])
                self.assertIsNotNone(
                    dcf["sensitivity"]["range"]["enterprise_value_min"]
                )

    def test_recorded_finalizer_rejects_unresolved_frozen_fact_path(self):
        case = self._load_producer(
            deterministic_current={
                "operating_cash_flow": {
                    "value": 13.9,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                }
            }
        )
        self.assertIsInstance(case.deterministic_current, MappingProxyType)
        self.assertIsInstance(
            case.deterministic_current["operating_cash_flow"],
            MappingProxyType,
        )
        payload = narrative_payload()
        payload["summary"] = "Free cash flow was $13.9 billion in FY2024 Q4."
        payload["numeric_claims"] = [
            {
                "claim_id": "missing_free_cash_flow",
                "path": "summary",
                "value": "not-a-number",
                "metric": "free cash flow",
                "period": "FY2024 Q4",
                "unit": "usd_billions",
                "currency": "USD",
                "source_kind": "fact",
                "fact_path": "deterministic_current.free_cash_flow.value",
            }
        ]
        recorded = cb.recorded_executor_output(json.dumps(payload))

        with self.assertRaises(service.InvestmentValidationError) as raised:
            cb.finalize_recorded_company_run(recorded, case)

        self.assertEqual(raised.exception.category, service.VALIDATION_JSON_SCHEMA)
        self.assertTrue(
            any(
                "value: must be a finite number" in p for p in raised.exception.problems
            ),
            raised.exception.problems,
        )

    def test_recorded_finalizer_accepts_exact_frozen_fact_row(self):
        case = self._load_producer(
            deterministic_current={
                "operating_cash_flow": {
                    "value": 13.9,
                    "unit": "usd_billions",
                    "currency": "USD",
                    "period": "FY2024-Q4",
                }
            }
        )
        payload = narrative_payload()
        payload["summary"] = "Operating cash flow was $13.9 billion in FY2024 Q4."
        row = {
            "claim_id": "operating_cash_flow",
            "path": "summary",
            "value": 13.9,
            "metric": "operating cash flow",
            "period": "FY2024 Q4",
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "fact",
            "fact_path": "deterministic_current.operating_cash_flow.value",
        }
        payload["numeric_claims"] = [row]
        recorded = cb.recorded_executor_output(json.dumps(payload))

        result = cb.finalize_recorded_company_run(recorded, case)

        self.assertEqual(result.facts["numeric_claims"], [row])
        self.assertEqual(
            case.deterministic_current["operating_cash_flow"]["value"], 13.9
        )

    def test_cash_paid_for_property_cannot_ground_operating_cash_flow(self):
        cash_paid_quote = (
            "Cash paid for property and equipment was $13.9 billion in FY2024 Q4"
        )
        producer = self._load_producer(excerpt=f"{EXCERPT} {cash_paid_quote}.")
        payload = narrative_payload()
        payload["qualitative"]["ai_demand"]["evidence"] = (
            "ungrounded fabricated evidence"
        )
        recorded = cb.recorded_executor_output(json.dumps(payload))

        with self.assertRaises(service.InvestmentValidationError) as raised:
            cb.finalize_recorded_company_run(recorded, producer)

        self.assertEqual(raised.exception.category, service.VALIDATION_JSON_SCHEMA)
        self.assertTrue(
            any(
                "qualitative.ai_demand: evidence is not grounded" in p
                for p in raised.exception.problems
            ),
            raised.exception.problems,
        )

    def test_fingerprint_is_derived_and_case_stays_frozen(self):
        case = self._load_producer()
        self.assertEqual(
            case.fingerprint,
            canonical_fingerprint(cb.canonical_producer_fingerprint_payload(case)),
        )
        original = case.fingerprint
        # Identity is a derived digest over the validated fixture content,
        # not free-form stored data.
        self.assertEqual(case.fingerprint, cb.canonical_producer_fingerprint(case))
        # The frozen case refuses direct mutation of its identity field.
        with self.assertRaises(FrozenInstanceError):
            case.fingerprint = "1" * 64
        self.assertEqual(case.fingerprint, original)


class CompanyBenchmarkImmutabilityTests(unittest.TestCase):
    """Recursive freezing plus point-in-time and hindsight gate regressions."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def _load_producer(self, **overrides):
        raw = producer_raw()
        raw.update(overrides)
        return cb.load_producer_case(write_yaml(self.directory, "producer.yaml", raw))

    def _load_evaluator(self, producer, **overrides):
        raw = evaluator_raw(producer.fingerprint, **overrides)
        return cb.load_evaluator_case(
            write_yaml(self.directory, "evaluator.yaml", raw), producer=producer
        )

    def test_nested_structures_are_recursively_immutable(self):
        producer = self._load_producer()
        evaluator = self._load_evaluator(
            producer,
            deterministic_checks=[
                {"metric": "revenue", "comparator": "equals", "value": 10}
            ],
            known_traps=["narrating post-cutoff guidance", {"pattern": "H2 orders"}],
            later_outcomes=[{"outcome": "demand reversed"}],
            forbidden_hindsight=[
                {
                    "claim_id": "capex_q1",
                    "metric_aliases": ["capex"],
                    "value": 20,
                    "period_aliases": ["Q1 FY2025"],
                    "available_after": "2026-06-01T00:00:00Z",
                }
            ],
        )
        packets = [
            ("document", producer.document),
            ("deterministic_current", producer.deterministic_current),
            ("deterministic_prior", producer.deterministic_prior),
            ("market_inputs", producer.market_inputs),
            ("prior_facts", producer.prior_facts),
            ("extraction", producer.extraction),
            ("news_items", producer.news_items),
            (
                "expected_material_observations",
                evaluator.expected_material_observations,
            ),
            ("deterministic_checks", evaluator.deterministic_checks),
            ("known_traps", evaluator.known_traps),
            ("later_outcomes", evaluator.later_outcomes),
        ]
        for name, packet in packets:
            with self.subTest(packet=name):
                self.assertEqual(_frozen_structure_problems(packet), [])
                attempts = 0
                for attempt in _iter_mutation_attempts(packet):
                    with self.assertRaises(TypeError):
                        attempt()
                    attempts += 1
                self.assertGreaterEqual(attempts, 1)
        self.assertIsInstance(producer.news_items[0], MappingProxyType)
        self.assertIsInstance(evaluator.deterministic_checks[0], MappingProxyType)
        with self.assertRaises(FrozenInstanceError):
            evaluator.forbidden_hindsight[0].value = 21

    def test_deeply_nested_freeze_copies_are_independent_of_the_source(self):
        nested_metrics = {
            "segments": [
                {
                    "name": "datacenter",
                    "trend": {"direction": "up", "checks": [{"ok": True}]},
                }
            ]
        }
        case = self._load_producer(deterministic_current=nested_metrics)
        frozen = case.deterministic_current
        self.assertIsInstance(frozen, MappingProxyType)
        self.assertEqual(_frozen_structure_problems(frozen), [])
        baseline = repr(frozen)
        for attempt in _iter_mutation_attempts(frozen):
            with self.assertRaises(TypeError):
                attempt()
        nested_metrics["segments"][0]["trend"]["direction"] = "poisoned"
        nested_metrics["segments"].append({"name": "late-arrival"})
        self.assertEqual(repr(frozen), baseline)
        self.assertEqual(_frozen_structure_problems(frozen), [])

    def test_nested_pit_timestamp_fields_fail_closed_when_invalid(self):
        base_document = producer_raw()["document"]
        rejections = [
            (
                {"document": dict(base_document, released_at="   ")},
                "producer.document.released_at",
                "must parse",
            ),
            (
                {
                    "extraction": {
                        "report_text_source": "stored_document",
                        "source_timestamp": "not-a-timestamp",
                    }
                },
                "producer.extraction.source_timestamp",
                "must parse",
            ),
            (
                {"deterministic_current": {"revenue": {"value": 10, "checked_at": ""}}},
                "producer.deterministic_current.revenue.checked_at",
                "must parse",
            ),
            (
                {"document": dict(base_document, observed_at="not-a-timestamp")},
                "producer.document.observed_at",
                "must parse",
            ),
            (
                {
                    "deterministic_prior": {
                        "revenue": {"value": 9, "observed_at": "2026-04-01T00:00:00Z"}
                    }
                },
                "producer.deterministic_prior.revenue.observed_at",
                "after as_of",
            ),
            (
                {"document": dict(base_document, released_at="2026-03-15T12:00:00")},
                "producer.document.released_at",
                "timezone-aware",
            ),
            (
                {
                    "market_inputs": {
                        "session": {"target_at": datetime(2026, 3, 2, 12, 0)}
                    }
                },
                "producer.market_inputs.session.target_at",
                "timezone-aware",
            ),
            (
                {"prior_facts": {"guidance": {"valid_from": "2026-04-02T00:00:00Z"}}},
                "producer.prior_facts.guidance.valid_from",
                "after as_of",
            ),
            (
                {
                    "news_items": [
                        dict(NEWS_ITEM, source_timestamp="2026-04-01T12:00:00Z")
                    ]
                },
                "producer.news_items[0].source_timestamp",
                "after as_of",
            ),
        ]
        for overrides, path, fragment in rejections:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    self._load_producer(**overrides)
                self.assertIn(path, str(ctx.exception))
                self.assertIn(fragment, str(ctx.exception))

    def test_consensus_availability_checked_at_is_a_gated_pit_timestamp(self):
        def consensus_block(stamp):
            return {
                "consensus_estimates": {
                    "status": "unavailable_as_of_producer_cutoff",
                    "consensus_availability_checked_at": stamp,
                }
            }

        loaded = self._load_producer(
            market_inputs=consensus_block("2026-03-31T00:00:00Z")
        )
        self.assertEqual(
            loaded.market_inputs["consensus_estimates"][
                "consensus_availability_checked_at"
            ],
            "2026-03-31T00:00:00Z",
        )
        rejections = [
            ("2026-03-31T00:00:01Z", "after as_of"),
            ("2026-04-05T00:00:00Z", "after as_of"),
            ("not-a-timestamp", "must parse"),
            ("", "must parse"),
            ("2026-03-31T00:00:00", "timezone-aware"),
        ]
        for stamp, fragment in rejections:
            with self.subTest(stamp=stamp):
                with self.assertRaises(ValueError) as ctx:
                    self._load_producer(market_inputs=consensus_block(stamp))
                self.assertIn("consensus_availability_checked_at", str(ctx.exception))
                self.assertIn(fragment, str(ctx.exception))

    def test_prepare_output_stays_byte_and_fingerprint_stable_after_mutations(self):
        case = self._load_producer()
        request = cb.prepare_company_run(case)
        baseline_prompt = request.prompt
        baseline_fingerprint = request.fingerprint
        attempts = 0
        for packet in (
            case.document,
            case.deterministic_current,
            case.deterministic_prior,
            case.market_inputs,
            case.prior_facts,
            case.extraction,
            case.news_items,
        ):
            for attempt in _iter_mutation_attempts(packet):
                with self.assertRaises(TypeError):
                    attempt()
                attempts += 1
        self.assertGreaterEqual(attempts, 7)
        self.assertEqual(request.prompt, baseline_prompt)
        self.assertEqual(request.schema_name, "investment_report_narrative_v7")
        replayed = cb.prepare_company_run(self._load_producer())
        self.assertEqual(replayed.prompt, baseline_prompt)
        self.assertEqual(replayed.fingerprint, baseline_fingerprint)

    def test_forbidden_available_after_must_strictly_follow_producer_as_of(self):
        producer = self._load_producer()
        valid_row = {
            "claim_id": "capex_q1",
            "metric_aliases": ["capex"],
            "value": 20,
            "period_aliases": ["Q1 FY2025"],
            "available_after": "2026-06-01T00:00:00Z",
        }
        loaded = self._load_evaluator(producer, forbidden_hindsight=[valid_row])
        self.assertEqual(
            loaded.forbidden_hindsight[0].available_after,
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        rejections = [
            (
                "equal to as_of",
                dict(
                    valid_row,
                    claim_id="row_equal",
                    available_after="2026-03-31T00:00:00Z",
                ),
            ),
            (
                "earlier than as_of",
                dict(
                    valid_row,
                    claim_id="row_before",
                    available_after="2026-03-30T23:59:59Z",
                ),
            ),
            (
                "equal to as_of in another zone",
                dict(
                    valid_row,
                    claim_id="row_offset",
                    available_after="2026-03-31T03:00:00+03:00",
                ),
            ),
        ]
        for label, row in rejections:
            with self.subTest(rejection=label):
                with self.assertRaises(ValueError) as ctx:
                    self._load_evaluator(producer, forbidden_hindsight=[row])
                self.assertIn(
                    "must be available strictly after as_of", str(ctx.exception)
                )
        boundary = self._load_evaluator(
            producer,
            forbidden_hindsight=[
                dict(valid_row, available_after="2026-03-31T00:00:00.000001Z")
            ],
        )
        self.assertEqual(len(boundary.forbidden_hindsight), 1)


class ProducerCaseTemporalKeyTests(unittest.TestCase):
    """Table-driven point-in-time enforcement over every temporal key class.

    Declared source/availability/provenance times (instants and date-only
    historical fields) must parse, fail closed when invalid or naive, and
    stay at or before ``as_of`` — at any nesting depth. Forecast/reference
    boundaries (fiscal period ends, guidance target periods, validity
    windows) may legally exceed ``as_of`` but must still parse. Every
    undeclared timestamp-like key (``*_at``/``*_date``/``*_timestamp``/
    ``*_until``) is rejected even when its value is harmless, so unsigned
    provenance channels cannot silently bypass point-in-time enforcement;
    the suffix rule stays narrow enough to leave non-temporal keys alone.
    """

    DECLARED_CURRENT_ROWS = (
        ("document", "release_date", "2026-03-10"),
        ("document", "announced_date", "2026-02-20"),
        ("document", "filing_date", "2026-03-05"),
        ("document", "report_date", "2026-03-31"),
        ("extraction", "transcript_created_at", "2026-03-18T09:00:00Z"),
        ("market_inputs", "event_ended_at", "2026-03-19T21:30:00Z"),
        ("deterministic_current", "observed_at", "2026-03-31T00:00:00Z"),
        ("prior_facts", "checked_at", "2026-03-11T12:00:00Z"),
    )

    DECLARED_FUTURE_ROWS = (
        ("document", "release_date", "2026-04-30"),
        ("document", "announced_date", "2026-04-15"),
        ("document", "filing_date", "2026-04-02"),
        ("extraction", "transcript_created_at", "2026-04-05T00:00:00Z"),
        ("market_inputs", "event_ended_at", "2026-04-01T00:00:00Z"),
        ("deterministic_current", "observed_at", "2026-04-01T00:00:00Z"),
        ("prior_facts", "checked_at", "2026-04-01T00:00:00Z"),
    )

    DECLARED_INVALID_ROWS = (
        ("document", "release_date", "not-a-date", "must be a valid date"),
        ("document", "announced_date", None, "must be a valid date"),
        ("document", "filing_date", "2026-03-05T12:00:00", "must be timezone-aware"),
        (
            "extraction",
            "transcript_created_at",
            "not-a-timestamp",
            "must parse as a timezone-aware timestamp",
        ),
        (
            "extraction",
            "transcript_created_at",
            "2026-03-18T09:00:00",
            "must be timezone-aware",
        ),
        (
            "market_inputs",
            "event_ended_at",
            "",
            "must parse as a timezone-aware timestamp",
        ),
        (
            "deterministic_current",
            "observed_at",
            None,
            "present point-in-time timestamp field",
        ),
        (
            "prior_facts",
            "checked_at",
            ["2026-03-11"],
            "must parse as a timezone-aware timestamp",
        ),
    )

    UNKNOWN_TIMESTAMP_LIKE_ROWS = (
        ("extraction", "mystery_at", "2020-01-01T00:00:00Z"),
        ("document", "mystery_date", "2020-01-01"),
        ("market_inputs", "mystery_timestamp", "2020-01-01T00:00:00Z"),
        ("prior_facts", "archived_until", "2020-01-01"),
        ("deterministic_current", "snapshot_taken_at", 1710000000),
    )

    FORECAST_WINDOW = {
        "guidance_note": "management targets through fiscal 2027",
        "valid_to": "2027-12-31",
        "valid_until": "2027-06-30T23:59:59Z",
        "fiscal_period_end": "2026-09-30",
        "guidance_target_period_end": "2027-06-30",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def _load(self, mutate):
        raw = producer_raw()
        mutate(raw)
        return cb.load_producer_case(write_yaml(self.directory, "producer.yaml", raw))

    def test_declared_source_and_availability_times_accept_current_values(self):
        for container, path, value in self.DECLARED_CURRENT_ROWS:
            with self.subTest(path=path):
                case = self._load(_put(container, path, value))
                node = cb.plain_copy(getattr(case, container))
                for step in path.split("."):
                    node = node[step]
                self.assertEqual(node, value)

    def test_future_source_availability_or_provenance_times_are_rejected_by_exact_path(
        self,
    ):
        for container, path, value in self.DECLARED_FUTURE_ROWS:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    self._load(_put(container, path, value))
                message = str(ctx.exception)
                self.assertIn(f"producer.{container}.{path}", message)
                self.assertIn("after as_of", message)
                self.assertNotIn("undeclared timestamp-like field", message)

    def test_invalid_declared_temporal_values_fail_closed_by_exact_path(self):
        for container, path, value, fragment in self.DECLARED_INVALID_ROWS:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    self._load(_put(container, path, value))
                message = str(ctx.exception)
                self.assertIn(f"producer.{container}.{path}", message)
                self.assertIn(fragment, message)

    def test_nested_temporal_variants_enforce_the_same_rules_by_exact_path(self):
        def with_nested_instants(raw):
            raw["extraction"] = {
                "report_text_source": "stored_document",
                "webcast": {
                    "event_ended_at": "2026-03-19T21:30:00Z",
                    "transcript_created_at": "2026-03-20T09:00:00Z",
                },
            }

        case = self._load(with_nested_instants)
        webcast = cb.plain_copy(case.extraction)["webcast"]
        self.assertEqual(webcast["event_ended_at"], "2026-03-19T21:30:00Z")
        self.assertEqual(webcast["transcript_created_at"], "2026-03-20T09:00:00Z")

        accepted = self._load(
            _put("document", "press_kit.first_look.release_date", "2026-01-15")
        )
        self.assertEqual(
            cb.plain_copy(accepted.document)["press_kit"]["first_look"]["release_date"],
            "2026-01-15",
        )

        rejections = [
            (
                _put("document", "filings.latest.filing_date", "2026-04-02"),
                "producer.document.filings.latest.filing_date",
            ),
            (
                _put("market_inputs", "consensus.checked_at", "2026-04-01T00:00:00Z"),
                "producer.market_inputs.consensus.checked_at",
            ),
        ]
        for mutate, path in rejections:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    self._load(mutate)
                message = str(ctx.exception)
                self.assertIn(path, message)
                self.assertIn("after as_of", message)

        def with_release_list(raw):
            raw["prior_facts"] = {
                "guidance": {
                    "releases": [
                        {"note": "earlier cycle"},
                        {"announced_date": "2026-04-10"},
                    ]
                }
            }

        with self.assertRaises(ValueError) as ctx:
            self._load(with_release_list)
        message = str(ctx.exception)
        self.assertIn(
            "producer.prior_facts.guidance.releases[1].announced_date", message
        )
        self.assertIn("after as_of", message)

    def test_unknown_timestamp_like_keys_are_rejected_even_with_harmless_values(self):
        for container, path, value in self.UNKNOWN_TIMESTAMP_LIKE_ROWS:
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as ctx:
                    self._load(_put(container, path, value))
                message = str(ctx.exception)
                self.assertIn(f"producer.{container}.{path}", message)
                self.assertIn("undeclared timestamp-like field", message)
                self.assertNotIn("after as_of", message)

        def with_nested_mystery(raw):
            raw["document"] = dict(
                raw["document"], provenance={"mystery_at": "2020-01-01T00:00:00Z"}
            )

        with self.assertRaises(ValueError) as ctx:
            self._load(with_nested_mystery)
        self.assertIn("producer.document.provenance.mystery_at", str(ctx.exception))
        self.assertIn("undeclared timestamp-like field", str(ctx.exception))

        # The suffix rule must stay narrow: non-temporal keys that merely
        # relate to time or contain the substrings are not timestamp-like.
        near_miss = {
            "attribution": "press release desk",
            "update_policy": "append-only",
            "validated": True,
            "dates_covered_note": "fiscal 2026 quarters",
        }
        tolerated = self._load(_put("document", "presentation_metadata", near_miss))
        self.assertEqual(
            cb.plain_copy(tolerated.document)["presentation_metadata"], near_miss
        )

    def test_forecast_reference_periods_may_exceed_as_of_but_still_must_parse(self):
        case = self._load(
            _put("prior_facts", "guidance_window", dict(self.FORECAST_WINDOW))
        )
        window = cb.plain_copy(case.prior_facts)["guidance_window"]
        self.assertEqual(
            window,
            {
                "guidance_note": "management targets through fiscal 2027",
                "valid_to": "2027-12-31",
                "valid_until": "2027-06-30T23:59:59Z",
                "fiscal_period_end": "2026-09-30",
                "guidance_target_period_end": "2027-06-30",
            },
        )

        dated = self._load(_put("market_inputs", "guidance.valid_to", "2028-01-01"))
        self.assertEqual(
            cb.plain_copy(dated.market_inputs)["guidance"]["valid_to"], "2028-01-01"
        )

        invalid_windows = (
            ({"valid_to": "not-a-date"}, "must be a valid date"),
            ({"valid_to": "2027-01-01T00:00:00"}, "must be timezone-aware"),
            ({"valid_to": None}, "must be a valid date"),
        )
        for payload, fragment in invalid_windows:
            with self.subTest(fragment=fragment):
                with self.assertRaises(ValueError) as ctx:
                    self._load(_put("prior_facts", "guidance_window", payload))
                message = str(ctx.exception)
                self.assertIn("producer.prior_facts.guidance_window.valid_to", message)
                self.assertIn(fragment, message)
                self.assertNotIn("undeclared timestamp-like field", message)

    def test_null_report_date_is_accepted_while_other_null_dates_fail_closed(self):
        case = self._load(_put("document", "report_date", None))
        self.assertIsNone(cb.plain_copy(case.document)["report_date"])

        for key in ("release_date", "filing_date"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    self._load(_put("document", key, None))
                message = str(ctx.exception)
                self.assertIn(f"producer.document.{key}", message)
                self.assertIn("must be a valid date", message)
