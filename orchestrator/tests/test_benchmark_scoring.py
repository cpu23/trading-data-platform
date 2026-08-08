import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_benchmark import (
    BLIND_REVIEW_CRITERIA,
    apply_blind_review_scores,
    evaluate_output,
    render_blind_review,
    run_benchmark,
    score_models,
    summarize_case_runs,
)


def _case():
    return {
        "case_id": "core-900-scoring",
        "suite": "core",
        "fixture_version": 1,
        "task": "macro_release_interpretation",
        "prompt_version": "v1",
        "request_profile": {"max_output_tokens": 500, "temperature": 0.2},
        "messages": [
            {"role": "system", "content": "Use only supplied evidence."},
            {
                "role": "user",
                "content": "EVIDENCE:\n[e1] CPI printed 2.9% versus 2.7% consensus.\nTASK: interpret.",
            },
        ],
        "response_schema": {"type": "object"},
        "expectations": {
            "required_fields": ["interpretation", "evidence_ids"],
            "allowed_evidence_ids": ["e1"],
            "forbidden_phrases": ["buy", "sell"],
            "allowed_numbers": ["2.9%", "2.7%"],
            "contradiction_checks": [
                {"phrase": "inflation accelerated", "evidence_marker": "2.9%"}
            ],
        },
    }


class UnsupportedClaimTests(unittest.TestCase):
    def test_unsupported_numbers_are_flagged(self):
        parsed = {
            "interpretation": "Inflation at 3.4% is accelerating.",
            "evidence_ids": ["e1"],
        }
        metrics = evaluate_output(_case(), parsed, json.dumps(parsed))
        self.assertIn("3.4%", metrics["unsupported_numerical_claims"])
        self.assertNotIn("2.9%", metrics["unsupported_numerical_claims"])

    def test_unit_interval_scores_are_not_unsupported_factual_claims(self):
        parsed = {
            "interpretation": "Confidence is represented separately.",
            "confidence": 0.85,
            "evidence_ids": ["e1"],
        }
        metrics = evaluate_output(_case(), parsed, json.dumps(parsed))
        self.assertNotIn("0.85", metrics["unsupported_numerical_claims"])

        parsed["interpretation"] = "Inflation printed 0.85%."
        metrics = evaluate_output(_case(), parsed, json.dumps(parsed))
        self.assertIn("0.85%", metrics["unsupported_numerical_claims"])

    def test_contradiction_check_hits_only_with_marker(self):
        parsed = {
            "interpretation": "Inflation accelerated (2.9% print).",
            "evidence_ids": ["e1"],
        }
        metrics = evaluate_output(_case(), parsed, json.dumps(parsed))
        self.assertEqual(metrics["contradiction_hits"], ["inflation accelerated"])

    def test_stability_counts_identical_parsed_outputs(self):
        def run(parsed):
            return {
                "http_ok": True,
                "schema_valid": True,
                "schema_valid_first_pass": True,
                "schema_valid_after_repair": True,
                "parsed": parsed,
                "latency_ms": 10,
            }

        runs = [run({"a": 1}), run({"a": 1}), run({"a": 2})]
        summary = summarize_case_runs(runs)
        self.assertAlmostEqual(summary["output_stability"], 2 / 3)


class DecisionScoreTests(unittest.TestCase):
    def _summary(self, after_repair=1.0, violations=0, evidence_rate=1.0):
        return {
            "suite": "core",
            "models": {
                "deepseek/deepseek-v4-flash-0731": {
                    "runs": 3,
                    "http_success_rate": 1.0,
                    "schema_valid_after_repair_rate": after_repair,
                    "schema_valid_first_pass_rate": after_repair,
                    "evidence_valid_rate": evidence_rate,
                    "completeness_rate": 1.0,
                    "policy_violations": violations,
                    "mean_latency_ms": 900,
                    "mean_cost_usd": 0.002,
                    "output_stability": 1.0,
                    "invalid_evidence_ids_total": 0,
                },
                "openai/gpt-5.6-luna": {
                    "runs": 3,
                    "http_success_rate": 1.0,
                    "schema_valid_after_repair_rate": 1.0,
                    "schema_valid_first_pass_rate": 0.95,
                    "evidence_valid_rate": 1.0,
                    "completeness_rate": 1.0,
                    "policy_violations": 0,
                    "mean_latency_ms": 1500,
                    "mean_cost_usd": 0.01,
                    "output_stability": 0.66,
                    "invalid_evidence_ids_total": 0,
                },
            },
        }

    def _review(self):
        return {
            "complete": True,
            "models": {
                "deepseek/deepseek-v4-flash-0731": {
                    "criteria_mean": [5, 5, 5, 5, 5, 5, 5, 5]
                },
                "openai/gpt-5.6-luna": {"criteria_mean": [5, 5, 5, 5, 5, 5, 5, 5]},
            },
        }

    def test_core_schema_below_threshold_disqualifies(self):
        decision = score_models(
            self._summary(after_repair=0.9), blind_review=self._review()
        )
        self.assertIn(
            "schema_valid_after_repair_below_threshold",
            decision["disqualified"]["deepseek/deepseek-v4-flash-0731"],
        )
        self.assertEqual(decision["recommended"], "openai/gpt-5.6-luna")

    def test_eligible_model_with_best_score_recommended(self):
        decision = score_models(self._summary(), blind_review=self._review())
        self.assertEqual(decision["disqualified"], {})
        self.assertEqual(decision["recommended"], "deepseek/deepseek-v4-flash-0731")
        self.assertGreater(
            decision["scores"]["deepseek/deepseek-v4-flash-0731"],
            decision["scores"]["openai/gpt-5.6-luna"],
        )

    def test_repeated_policy_violations_disqualify(self):
        decision = score_models(
            self._summary(violations=2), blind_review=self._review()
        )
        self.assertIn(
            "repeated_policy_violations",
            decision["disqualified"]["deepseek/deepseek-v4-flash-0731"],
        )

    def test_single_evidence_failure_is_not_persistent_fabrication(self):
        summary = self._summary(evidence_rate=0.9)
        metrics = summary["models"]["deepseek/deepseek-v4-flash-0731"]
        metrics["invalid_evidence_ids_total"] = 1
        metrics["evidence_fabrication_runs"] = 1
        decision = score_models(summary, blind_review=self._review())
        self.assertNotIn("deepseek/deepseek-v4-flash-0731", decision["disqualified"])

    def test_repeated_evidence_fabrication_disqualifies(self):
        summary = self._summary(evidence_rate=0.9)
        metrics = summary["models"]["deepseek/deepseek-v4-flash-0731"]
        metrics["invalid_evidence_ids_total"] = 2
        metrics["evidence_fabrication_runs"] = 2
        decision = score_models(summary, blind_review=self._review())
        self.assertIn(
            "persistent_evidence_fabrication",
            decision["disqualified"]["deepseek/deepseek-v4-flash-0731"],
        )

    def test_recommendation_remains_pending_without_blind_review(self):
        decision = score_models(self._summary())
        self.assertFalse(decision["blind_review_complete"])
        self.assertIsNone(decision["recommended"])


class BlindReviewTests(unittest.TestCase):
    def test_blind_review_anonymizes_and_writes_separate_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            key_path = Path(tmp) / "blind-review-key.json"
            for case in ("case-a",):
                for model in ("model__one", "model__two"):
                    run_dir = raw / case / model
                    run_dir.mkdir(parents=True)
                    (run_dir / "run-1.json").write_text(
                        json.dumps({"parsed": {"interpretation": f"{model} text"}})
                    )
            summary = {
                "run_id": "core-x",
                "models": {"model/one": {}, "model/two": {}},
            }
            html = render_blind_review(summary, raw, key_path=key_path)
            key = json.loads(key_path.read_text())
        self.assertNotIn("model__one", html)
        self.assertNotIn("model__two", html)
        self.assertNotIn("model/one", html)
        self.assertIn("Model A", html)
        self.assertIn("Model B", html)
        self.assertIn("Factual faithfulness", html)
        self.assertIn("reviewer rationale", html)
        self.assertIn("blind-review-scores.json", html)
        self.assertEqual(
            set(key["cases"]["case-a"].values()), {"model/one", "model/two"}
        )

    def test_completed_review_finalizes_decision_and_saves_rationale(self):
        model = "model/one"
        metrics = {
            "runs": 3,
            "http_success_rate": 1.0,
            "schema_valid_after_repair_rate": 1.0,
            "schema_valid_first_pass_rate": 1.0,
            "evidence_valid_rate": 1.0,
            "completeness_rate": 1.0,
            "policy_violations": 0,
            "mean_latency_ms": 100,
            "mean_cost_usd": 0.001,
            "output_stability": 1.0,
            "invalid_evidence_ids_total": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            raw_dir = output / "raw" / "case-a" / "model__one"
            raw_dir.mkdir(parents=True)
            (raw_dir / "run-1.json").write_text(json.dumps({"parsed": {"ok": True}}))
            summary = {
                "run_id": "core-x",
                "suite": "core",
                "models": {model: metrics},
            }
            (output / "summary.json").write_text(json.dumps(summary))
            (output / "manifest.json").write_text(
                json.dumps({"suite": "core", "runs_per_case": 3})
            )
            render_blind_review(
                summary,
                output / "raw",
                key_path=output / "blind-review-key.json",
            )
            key = json.loads((output / "blind-review-key.json").read_text())
            label = next(iter(key["cases"]["case-a"]))
            review = {
                "run_id": "core-x",
                "criteria": list(BLIND_REVIEW_CRITERIA),
                "entries": [
                    {
                        "case_id": "case-a",
                        "blind_label": label,
                        "scores": [5] * len(BLIND_REVIEW_CRITERIA),
                        "rationale": "Evidence-bound and concise.",
                    }
                ],
            }
            review_path = output / "blind-review-scores.json"
            review_path.write_text(json.dumps(review))
            completed = apply_blind_review_scores(output, review_path)

        self.assertTrue(completed["decision"]["blind_review_complete"])
        self.assertEqual(completed["decision"]["recommended"], model)
        self.assertEqual(
            completed["blind_review"]["models"][model]["rationales"][0]["rationale"],
            "Evidence-bound and concise.",
        )


class ForceBudgetTests(unittest.TestCase):
    def test_force_uses_trusted_manual_budget_context(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("model_benchmark.load_suite", return_value=[_case()]),
        ):
            summary = run_benchmark(
                {},
                models=["a/one"],
                output_dir=tmp,
                force=True,
                dry_run=True,
            )
        self.assertEqual(summary["cases"], 1)


if __name__ == "__main__":
    unittest.main()
