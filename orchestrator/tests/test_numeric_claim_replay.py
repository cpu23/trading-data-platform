"""Tests for numeric claim replay and report payload validation."""

from __future__ import annotations

import unittest

from investment_schemas import (
    validate_investment_report_payload,
    validate_numeric_claim_rows,
)


class TestNumericClaimReplay(unittest.TestCase):
    def _sample_report_payload(self) -> dict:
        return {
            "classification": {
                "document_type": "10-K",
                "sector": "Technology",
                "industry": "Software",
                "region": "North America",
                "confidence": "high",
            },
            "qualitative": {},
            "summary": "Company reported solid financial performance in FY2024.",
            "thesis": "Long term growth driven by cloud adoption.",
            "counter_thesis": "Margin pressure from increasing compute infrastructure costs.",
            "materiality_assessment": {},
            "numeric_claims": [
                {
                    "claim_id": "rev_fy24",
                    "path": "$.summary",
                    "value": 245120.0,
                    "metric": "total_revenue",
                    "period": "FY2024",
                    "currency": "USD",
                    "unit": "usd",
                }
            ],
        }

    def test_valid_report_payload_passes_validation(self) -> None:
        payload = self._sample_report_payload()
        problems = validate_investment_report_payload(payload)
        self.assertEqual(problems, [])

    def test_invalid_numeric_claims_surface_in_report_validation(self) -> None:
        payload = self._sample_report_payload()
        payload["numeric_claims"] = [
            {
                "claim_id": "bad_claim",
                "path": "$.summary",
                "value": "not-a-number",
                "metric": "",
                "period": "FY2024",
                "unit": "invalid_unit",
            }
        ]
        problems = validate_investment_report_payload(payload)
        self.assertTrue(any("value: must be a finite number" in p for p in problems))
        self.assertTrue(any("metric: must be a nonblank string" in p for p in problems))
        self.assertTrue(any("must have a valid unit or currency" in p for p in problems))

    def test_batch_replayed_claims_accumulate_all_row_problems(self) -> None:
        claims = [
            {"claim_id": "c1", "value": "nan", "metric": "m1", "period": "p1", "unit": "usd"},
            {"claim_id": "c1", "value": 10, "metric": "m2", "period": "p2", "currency": "USD"},
            {"claim_id": "", "value": 20, "metric": "", "period": "", "unit": "bad"},
        ]
        problems = validate_numeric_claim_rows(claims)
        self.assertGreaterEqual(len(problems), 4)
        self.assertTrue(any("duplicate claim_id 'c1'" in p for p in problems))
        self.assertTrue(any("value: must be a finite number" in p for p in problems))
        self.assertTrue(any("claim_id: must be a nonblank string" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
