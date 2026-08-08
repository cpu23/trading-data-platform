import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filing_deltas import compute_filing_delta

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
PREVIOUS_TEXT = """
OUTLOOK: We expect revenue growth of 10% and margin expansion.
Risk Factors: supply chain disruption remains a key risk.
SEGMENT REPORTING: Cloud revenue grew 20%.
"""
CURRENT_TEXT = """
OUTLOOK: We expect revenue growth of 12% and margin compression.
Risk Factors: supply chain disruption remains a key risk.
CAPITAL EXPENDITURE: planned capex of 4% of revenue.
"""


class Result:
    def __init__(self, *, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.results:
            return Result()
        return self.results.pop(0)


class FilingDeltaTests(unittest.TestCase):
    def test_delta_classifies_new_changed_and_removed_categories(self):
        doc_id, prev_id = uuid4(), uuid4()
        session = Session(
            [
                Result(
                    first={
                        "document_id": doc_id,
                        "company": "Acme",
                        "document_type": "annual_report",
                        "extracted_text": CURRENT_TEXT,
                        "created_at": NOW,
                    }
                ),
                Result(
                    first={
                        "document_id": prev_id,
                        "extracted_text": PREVIOUS_TEXT,
                    }
                ),
            ]
        )
        summary = compute_filing_delta({}, str(doc_id), session=session)
        self.assertEqual(summary["status"], "computed")
        self.assertEqual(summary["previous_document_id"], str(prev_id))
        inserts = [
            params
            for statement, params in session.calls
            if "INSERT INTO investment_filing_deltas" in statement
        ]
        kinds = {
            params["category"]: params.get("change_kind", "removed")
            for params in inserts
        }
        self.assertEqual(kinds.get("guidance"), "changed")
        self.assertEqual(kinds.get("risk_language"), "unchanged")
        self.assertEqual(kinds.get("capex"), "new")
        self.assertEqual(kinds.get("segments"), "removed")
        guidance = next(
            params for params in inserts if params["category"] == "guidance"
        )
        self.assertIn("12%", guidance["metrics"]["percent_mentions"])
        self.assertTrue(guidance["excerpt"])

    def test_delta_is_idempotent_per_document_and_category(self):
        doc_id = uuid4()
        session = Session(
            [
                Result(
                    first={
                        "document_id": doc_id,
                        "company": "Acme",
                        "document_type": "annual_report",
                        "extracted_text": CURRENT_TEXT,
                        "created_at": NOW,
                    }
                ),
                Result(first=None),
            ]
        )
        summary = compute_filing_delta({}, str(doc_id), session=session)
        self.assertEqual(summary["status"], "computed")
        for statement, _params in session.calls:
            if "INSERT" in statement:
                self.assertIn("ON CONFLICT (document_id, category)", statement)

    def test_missing_document_is_reported_without_inserts(self):
        session = Session([Result(first=None)])
        summary = compute_filing_delta({}, str(uuid4()), session=session)
        self.assertEqual(summary["status"], "missing_document")
        self.assertFalse(
            [statement for statement, _ in session.calls if "INSERT" in statement]
        )

    def test_ingestion_hooks_delta_before_any_analysis(self):
        from investment_filings import _ingest_filing

        with (
            patch("investment_filings._already_ingested", return_value=False),
            patch(
                "investment_filings.store_document_url",
                return_value={"document_id": str(uuid4())},
            ),
            patch(
                "investment_filings.compute_filing_delta",
                return_value={"status": "computed", "categories": 3},
            ) as delta,
            patch("investment_filings.analyze_document") as analyze,
        ):
            result = _ingest_filing(
                {},
                {
                    "source": "other",
                    "filing_id": "f-1",
                    "source_url": "https://example.com/f",
                    "company": "Acme",
                },
                auto_analyze=False,
            )
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(result["filing_delta"], 3)
        delta.assert_called_once()
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
