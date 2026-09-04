import json
import unittest
from datetime import datetime

from scripts.benchmark_api import percentile, run_benchmark


class BenchmarkPercentileTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self):
        samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(percentile(samples, 50), 30.0)
        self.assertEqual(percentile(samples, 95), 48.0)

    def test_empty_percentile_is_missing(self):
        self.assertIsNone(percentile([], 95))


class BenchmarkOutputContractTests(unittest.TestCase):
    def test_report_contains_cold_warm_dataset_and_failure_contract(self):
        responses = iter([(200, b'{"items":[1,2]}')] * 3)

        def request(_url, _timeout, _headers):
            return next(responses)

        report = run_benchmark(
            "http://fixture.invalid/api/investment/dashboard",
            warm_count=2,
            request_fn=request,
            fixture=True,
            fixture_items=2,
            dataset_cardinality=2,
            dataset_cardinality_source="fixture_items",
            scenario=None,
        )

        # Round-trip the same object shape written by the CLI.
        encoded = json.dumps(report)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema_version"], 1)
        self.assertEqual(decoded["scenario"], "api-investment-dashboard")
        self.assertTrue(decoded["parameters"]["fixture"])
        self.assertEqual(decoded["parameters"]["warm_count_requested"], 2)
        self.assertTrue(decoded["cold"]["ok"])
        self.assertIsInstance(decoded["cold"]["latency_ms"], float)
        self.assertEqual(decoded["cold"]["response_size_bytes"], 15)
        self.assertEqual(decoded["dataset_cardinality"], 2)
        self.assertEqual(decoded["dataset_cardinality_source"], "fixture_items")
        self.assertEqual(decoded["response_size_bytes"], 15)
        self.assertEqual(len(decoded["warm"]["samples_ms"]), 2)
        self.assertIsNotNone(decoded["warm"]["p50_ms"])
        self.assertIsNotNone(decoded["warm"]["p95_ms"])
        self.assertEqual(decoded["failures"], [])
        datetime.fromisoformat(decoded["timestamp"])
        for key in ("python", "platform", "machine", "hostname"):
            self.assertIn(key, decoded["environment"])

    def test_http_failure_is_recorded_separately_from_warm_samples(self):
        responses = iter([(503, b"busy"), (200, b"{}")])

        def request(_url, _timeout, _headers):
            return next(responses)

        report = run_benchmark(
            "http://fixture.invalid/api/investment/dashboard",
            warm_count=1,
            request_fn=request,
        )
        self.assertFalse(report["cold"]["ok"])
        self.assertEqual(report["cold"]["status"], 503)
        self.assertEqual(report["warm"]["count_successful"], 1)
        self.assertEqual(report["failures"][0]["phase"], "cold")
        self.assertEqual(report["failures"][0]["kind"], "http_status")


if __name__ == "__main__":
    unittest.main()
