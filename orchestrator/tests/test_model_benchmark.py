import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model_benchmark
from model_benchmark import (
    FixtureError,
    build_request_body,
    evaluate_output,
    load_suite,
    parse_model_list,
    run_benchmark,
    run_case_with_repair,
    summarize_case_runs,
)


def _case(case_id="case-1", expectations=None):
    return {
        "case_id": case_id,
        "suite": "core",
        "fixture_version": 1,
        "task": "macro_release_interpretation",
        "prompt_version": "event_impact_v1",
        "request_profile": {"max_output_tokens": 800, "temperature": 0.2},
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "EVIDENCE: [e1] fact"},
        ],
        "response_schema": {"type": "object"},
        "expectations": expectations
        or {
            "required_fields": ["observation", "evidence_ids"],
            "allowed_evidence_ids": ["e1", "e2"],
            "forbidden_phrases": ["buy"],
        },
    }


class FixtureLoadingTests(unittest.TestCase):
    def test_core_suite_loads_with_required_fields(self):
        cases = load_suite("core")
        self.assertGreaterEqual(len(cases), 2)
        for case in cases:
            for field in model_benchmark.REQUIRED_CASE_FIELDS:
                self.assertIn(field, case)

    def test_missing_suite_raises(self):
        with self.assertRaises(FixtureError):
            load_suite("does-not-exist")

    def test_case_missing_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "core"
            (suite / "cases").mkdir(parents=True)
            (suite / "manifest.json").write_text(json.dumps({"suite": "core"}))
            incomplete = _case()
            del incomplete["expectations"]
            (suite / "cases" / "a.json").write_text(json.dumps(incomplete))
            with self.assertRaises(FixtureError):
                load_suite("core", fixtures_dir=Path(tmp))

    def test_nested_object_schema_must_be_strict(self):
        case = _case()
        case["response_schema"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["nested"],
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                }
            },
        }
        with self.assertRaisesRegex(FixtureError, "additionalProperties"):
            model_benchmark._validate_case(case, Path("case.json"), "core")

    def test_suite_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "core"
            (suite / "cases").mkdir(parents=True)
            bad = _case()
            bad["suite"] = "adversarial"
            (suite / "cases" / "a.json").write_text(json.dumps(bad))
            with self.assertRaises(FixtureError):
                load_suite("core", fixtures_dir=Path(tmp))

    def test_duplicate_case_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "core"
            (suite / "cases").mkdir(parents=True)
            (suite / "cases" / "a.json").write_text(json.dumps(_case()))
            (suite / "cases" / "b.json").write_text(json.dumps(_case()))
            with self.assertRaises(FixtureError):
                load_suite("core", fixtures_dir=Path(tmp))


class FairRequestTests(unittest.TestCase):
    def test_request_bodies_identical_except_model(self):
        case = _case()
        body_a = build_request_body(case, "deepseek/deepseek-v4-flash-0731")
        body_b = build_request_body(case, "openai/gpt-5.6-luna")
        self.assertEqual(
            {k: v for k, v in body_a.items() if k != "model"},
            {k: v for k, v in body_b.items() if k != "model"},
        )
        self.assertNotEqual(body_a["model"], body_b["model"])
        self.assertEqual(body_a["max_output_tokens"], 800)

        body_a = build_request_body(
            case, "deepseek/deepseek-v4-flash-0731", include_temperature=False
        )
        body_b = build_request_body(
            case, "openai/gpt-5.6-luna", include_temperature=False
        )
        self.assertEqual(
            {k: v for k, v in body_a.items() if k != "model"},
            {k: v for k, v in body_b.items() if k != "model"},
        )
        self.assertNotIn("temperature", body_a)
        self.assertNotIn("temperature", body_b)


class EvaluationTests(unittest.TestCase):
    def test_valid_output_passes_all_checks(self):
        case = _case()
        parsed = {
            "observation": "payrolls missed",
            "interpretation": "rates repricing",
            "evidence_ids": ["e1"],
        }
        metrics = evaluate_output(case, parsed, json.dumps(parsed))
        self.assertTrue(metrics["schema_valid"])
        self.assertEqual(metrics["missing_fields"], [])
        self.assertEqual(metrics["invalid_evidence_ids"], [])
        self.assertEqual(metrics["forbidden_phrase_hits"], [])

    def test_unparseable_output_flags_missing_fields(self):
        case = _case()
        metrics = evaluate_output(case, None, "not json")
        self.assertFalse(metrics["schema_valid"])
        self.assertIn("observation", metrics["missing_fields"])

    def test_unsupported_evidence_ids_detected(self):
        case = _case()
        parsed = {
            "observation": "x",
            "evidence_ids": ["e1", "e99"],
        }
        metrics = evaluate_output(case, parsed, json.dumps(parsed))
        self.assertEqual(metrics["invalid_evidence_ids"], ["e99"])

    def test_forbidden_phrase_detected_case_insensitively(self):
        case = _case()
        parsed = {"observation": "BUY now", "evidence_ids": ["e1"]}
        metrics = evaluate_output(case, parsed, json.dumps(parsed))
        self.assertEqual(metrics["forbidden_phrase_hits"], ["buy"])

    def test_policy_phrases_do_not_match_inside_market_context_words(self):
        case = _case(
            expectations={
                "required_fields": ["observation", "evidence_ids"],
                "allowed_evidence_ids": ["e1"],
                "forbidden_phrases": ["buy", "sell", "go long", "go short"],
            }
        )
        parsed = {
            "observation": "Buybacks face selling pressure over a longer horizon.",
            "evidence_ids": ["e1"],
        }
        metrics = evaluate_output(case, parsed, json.dumps(parsed))
        self.assertEqual(metrics["forbidden_phrase_hits"], [])

        parsed["observation"] = "The instruction says GO LONG."
        metrics = evaluate_output(case, parsed, json.dumps(parsed))
        self.assertEqual(metrics["forbidden_phrase_hits"], ["go long"])

    def test_repair_attempt_accounting_includes_both_attempts(self):
        first = {
            "schema_valid": False,
            "missing_fields": ["observation"],
            "invalid_evidence_ids": [],
            "forbidden_phrase_hits": [],
            "output_length_chars": 4,
            "latency_ms": 10,
            "tokens_input": 10,
            "tokens_output": 2,
            "tokens_reasoning": 1,
            "tokens_cached": 0,
            "cost_usd": 0.01,
            "retry_count": 0,
        }
        second = {
            "schema_valid": True,
            "missing_fields": [],
            "invalid_evidence_ids": [],
            "forbidden_phrase_hits": [],
            "output_length_chars": 20,
            "latency_ms": 30,
            "tokens_input": 12,
            "tokens_output": 6,
            "tokens_reasoning": 2,
            "tokens_cached": 1,
            "cost_usd": 0.02,
            "retry_count": 0,
        }
        with patch.object(
            model_benchmark, "run_case_once", side_effect=[first, second]
        ):
            result = run_case_with_repair(
                {},
                _case(),
                "a/one",
                budget_context=model_benchmark.BudgetContext(),
            )
        self.assertEqual(result["attempts_used"], 2)
        self.assertEqual(result["latency_ms"], 40)
        self.assertEqual(result["tokens_input"], 22)
        self.assertAlmostEqual(result["cost_usd"], 0.03)
        self.assertTrue(result["schema_valid_after_repair"])


class SummaryTests(unittest.TestCase):
    def test_summarize_rates_and_costs(self):
        runs = [
            {
                "http_ok": True,
                "schema_valid_first_pass": True,
                "schema_valid_after_repair": True,
                "invalid_evidence_ids": [],
                "missing_fields": [],
                "forbidden_phrase_hits": [],
                "latency_ms": 100,
                "cost_usd": 0.01,
                "tokens_input": 10,
                "tokens_output": 5,
                "output_length_chars": 50,
            },
            {
                "http_ok": True,
                "schema_valid_first_pass": False,
                "schema_valid_after_repair": True,
                "invalid_evidence_ids": ["e9"],
                "missing_fields": [],
                "forbidden_phrase_hits": ["buy"],
                "latency_ms": 300,
                "cost_usd": 0.03,
                "tokens_input": 20,
                "tokens_output": 15,
                "output_length_chars": 150,
            },
        ]
        summary = summarize_case_runs(runs)
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["http_success_rate"], 1.0)
        self.assertEqual(summary["schema_valid_first_pass_rate"], 0.5)
        self.assertEqual(summary["schema_valid_after_repair_rate"], 1.0)
        self.assertEqual(summary["evidence_valid_rate"], 0.5)
        self.assertEqual(summary["policy_violations"], 1)
        self.assertEqual(summary["invalid_evidence_ids_total"], 1)
        self.assertEqual(summary["evidence_fabrication_runs"], 1)
        self.assertEqual(summary["policy_violation_runs"], 1)
        self.assertEqual(summary["mean_latency_ms"], 200)
        self.assertAlmostEqual(summary["mean_cost_usd"], 0.02)
        self.assertEqual(summary["total_tokens_input"], 30)
        self.assertEqual(summary["p95_latency_ms"], 300)

    def test_empty_runs(self):
        self.assertEqual(summarize_case_runs([]), {"runs": 0})


class ParseModelListTests(unittest.TestCase):
    def test_parses_two_pinned_slugs(self):
        self.assertEqual(
            parse_model_list("deepseek/deepseek-v4-flash-0731,openai/gpt-5.6-luna"),
            ["deepseek/deepseek-v4-flash-0731", "openai/gpt-5.6-luna"],
        )

    def test_accepts_openrouter_variant_suffix(self):
        self.assertEqual(
            parse_model_list("google/gemini-3.5-flash-lite:batch"),
            ["google/gemini-3.5-flash-lite:batch"],
        )

    def test_rejects_invalid_slug(self):
        with self.assertRaises(FixtureError):
            parse_model_list("Latest Model!")

    def test_rejects_duplicates(self):
        with self.assertRaises(FixtureError):
            parse_model_list("a/b,a/b")

    def test_rejects_empty_model_list(self):
        with self.assertRaises(FixtureError):
            parse_model_list("")


class BenchmarkRunTests(unittest.TestCase):
    def test_run_benchmark_records_metrics_and_artifacts(self):
        canned = {
            "content": json.dumps(
                {
                    "observation": "miss",
                    "interpretation": "repricing",
                    "scenario": "cut",
                    "unknowns": [],
                    "confidence": 0.6,
                    "confidence_components": {},
                    "invalidation_conditions": ["payrolls rebound"],
                    "evidence_ids": ["e1"],
                }
            ),
            "model": "resolved/model",
            "requested_model": "pinned/model",
            "provider": "DeepSeek",
            "generation_id": "gen-1",
            "tokens_input": 100,
            "tokens_output": 50,
            "tokens_reasoning": 0,
            "tokens_cached": 0,
            "cost_usd": 0.001,
            "duration_ms": 25,
            "retry_count": 0,
        }
        config = {"llm": {"api_key": "k", "models": {"default": "pinned/model"}}}
        with (
            patch.object(model_benchmark, "call_llm", return_value=canned) as call_llm,
            patch.object(model_benchmark, "load_suite", return_value=[_case()]),
            tempfile.TemporaryDirectory() as tmp,
        ):
            summary = run_benchmark(
                config,
                models=["a/one", "b/two"],
                suite="core",
                runs=2,
                output_dir=tmp,
            )

            self.assertEqual(summary["suite"], "core")
            self.assertEqual(set(summary["models"]), {"a/one", "b/two"})
            for metrics in summary["models"].values():
                self.assertEqual(metrics["runs"], 2)
                self.assertEqual(metrics["schema_valid_first_pass_rate"], 1.0)
                self.assertEqual(metrics["mean_latency_ms"], 25)
            output = Path(tmp)
            self.assertTrue((output / "manifest.json").exists())
            self.assertTrue((output / "summary.json").exists())
            self.assertTrue((output / "case-results.jsonl").exists())
            self.assertTrue((output / "summary.md").exists())
            raw_runs = list((output / "raw" / "case-1").rglob("run-*.json"))
            self.assertEqual(len(raw_runs), 4)
            self.assertTrue((output / "raw" / "case-1" / "a__one").is_dir())
            response_schema = call_llm.call_args.kwargs["response_schema"]
            self.assertEqual(response_schema["name"], "case-1")
            self.assertTrue(response_schema["strict"])
            self.assertEqual(response_schema["schema"], _case()["response_schema"])

    def test_dry_run_makes_no_model_calls(self):
        config = {"llm": {"models": {"default": "pinned/model"}}}
        with (
            patch.object(
                model_benchmark, "call_llm", side_effect=AssertionError("called")
            ),
            patch.object(model_benchmark, "load_suite", return_value=[_case()]),
            tempfile.TemporaryDirectory() as tmp,
        ):
            summary = run_benchmark(
                config,
                models=["a/one"],
                suite="core",
                runs=1,
                output_dir=tmp,
                dry_run=True,
            )
            self.assertTrue(summary["dry_run"])
            request_files = list(Path(tmp).rglob("request.json"))
            self.assertEqual(len(request_files), 1)

    def test_live_run_can_uniformly_omit_temperature(self):
        canned = {
            "content": json.dumps({"observation": "x", "evidence_ids": ["e1"]}),
            "duration_ms": 10,
            "tokens_input": 10,
            "tokens_output": 5,
            "cost_usd": 0.001,
        }
        config = {"llm": {"api_key": "k", "models": {"default": "pinned/model"}}}
        with (
            patch.object(model_benchmark, "call_llm", return_value=canned) as call_llm,
            patch.object(model_benchmark, "load_suite", return_value=[_case()]),
            tempfile.TemporaryDirectory() as tmp,
        ):
            run_benchmark(
                config,
                models=["a/one"],
                suite="core",
                runs=1,
                output_dir=tmp,
                include_temperature=False,
            )

            self.assertFalse(call_llm.call_args.kwargs["include_temperature"])
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertFalse(manifest["include_temperature"])
            raw_path = next(Path(tmp).rglob("run-1.json"))
            raw = json.loads(raw_path.read_text())
            self.assertNotIn("temperature", raw["request_body"])


if __name__ == "__main__":
    unittest.main()
