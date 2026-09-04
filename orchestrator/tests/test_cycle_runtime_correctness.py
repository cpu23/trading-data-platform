import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import (
    _aggregate_stage_status,
    _resolve_and_run_processors,
)


class CycleRuntimeCorrectnessTests(unittest.TestCase):
    def test_stage_aggregation_requires_complete_success_to_publish(self):
        self.assertEqual(
            _aggregate_stage_status(
                {
                    "fred": {"status": "success"},
                    "macro": {"status": "success"},
                }
            ),
            "success",
        )
        self.assertEqual(
            _aggregate_stage_status(
                {
                    "fred": {"status": "success"},
                    "macro": {"status": "validation_failed"},
                }
            ),
            "partial",
        )
        self.assertEqual(
            _aggregate_stage_status(
                {
                    "fred": {"status": "success", "blocking": True},
                    "oecd": {"status": "no_data", "blocking": False},
                    "macro": {"status": "success", "blocking": True},
                }
            ),
            "success",
        )

    @patch("orchestrator.get_all_processors")
    def test_unmet_dependencies_are_explicitly_recorded_as_skipped(
        self, get_all_processors
    ):
        processor = Mock()
        processor.get_depends_on.return_value = ["fred"]
        get_all_processors.return_value = {"macro_regime": processor}

        results = _resolve_and_run_processors(
            config={"processors": {"macro_regime": {"enabled": True}}},
            correlation_id="cycle-id",
            successful_collectors=set(),
        )

        self.assertEqual(results["macro_regime"]["status"], "skipped")
        self.assertIn("fred", results["macro_regime"]["reason"])


if __name__ == "__main__":
    unittest.main()
