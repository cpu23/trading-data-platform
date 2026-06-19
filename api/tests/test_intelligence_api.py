import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.json import assets, evidence  # noqa: E402


class IntelligenceApiTests(unittest.TestCase):
    @patch("routes.json.assets.load_config", return_value={})
    @patch("routes.json.assets.query_one", return_value=None)
    def test_current_intelligence_has_explicit_unavailable_state(
        self, _query_one, _load_config
    ):
        self.assertEqual(
            assets.get_current_intelligence(),
            {"available": False, "intelligence": None},
        )

    @patch("routes.json.assets.load_config", return_value={})
    @patch("routes.json.assets.query_one")
    def test_current_intelligence_exposes_publication_and_baseline(
        self, query_one, _load_config
    ):
        opinion_id = UUID("11111111-1111-1111-1111-111111111111")
        correlation_id = UUID("22222222-2222-2222-2222-222222222222")
        baseline_id = UUID("33333333-3333-3333-3333-333333333333")
        query_one.return_value = {
            "opinion_id": opinion_id,
            "correlation_id": correlation_id,
            "baseline_opinion_id": baseline_id,
            "payload": {"summary": "Economic context."},
        }
        result = assets.get_current_intelligence()
        self.assertTrue(result["available"])
        self.assertEqual(result["intelligence"]["opinion_id"], str(opinion_id))
        self.assertEqual(
            result["intelligence"]["baseline_opinion_id"], str(baseline_id)
        )

    @patch("routes.json.evidence.load_config", return_value={})
    @patch("routes.json.evidence.query_many", return_value=[])
    @patch("routes.json.evidence.query_one")
    def test_evidence_looks_up_every_processing_output_id(
        self, query_one, _query_many, _load_config
    ):
        opinion_id = "11111111-1111-1111-1111-111111111111"
        query_one.side_effect = [
            {
                "opinion_id": opinion_id,
                "correlation_id": None,
                "data_inputs": {},
            },
            {"processor": "market_intelligence"},
        ]
        result = evidence.get_evidence(opinion_id)
        processing_sql = query_one.call_args_list[1].args[0]
        self.assertIn("ANY(COALESCE(output_ids", processing_sql)
        self.assertEqual(result["processing"]["processor"], "market_intelligence")

    @patch("routes.json.evidence.load_config", return_value={})
    @patch("routes.json.evidence.query_many")
    @patch("routes.json.evidence.query_one")
    def test_evidence_includes_generation_attempt_diagnostics(
        self, query_one, query_many, _load_config
    ):
        opinion_id = "11111111-1111-1111-1111-111111111111"
        correlation_id = "22222222-2222-2222-2222-222222222222"
        query_one.side_effect = [
            {
                "opinion_id": opinion_id,
                "correlation_id": correlation_id,
                "data_inputs": {},
            },
            None,
        ]
        query_many.return_value = [
            {
                "stage": "editor",
                "attempt_number": 2,
                "status": "validation_failed",
            }
        ]
        result = evidence.get_evidence(opinion_id)
        self.assertEqual(
            result["generation_attempts"][0]["status"], "validation_failed"
        )


if __name__ == "__main__":
    unittest.main()
