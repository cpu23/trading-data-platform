"""Tests for Pydantic schema constraints and edge cases on numeric claims."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from investment_schemas import InvestmentReport, NumericClaimItem


class TestNumericClaimAdversarial(unittest.TestCase):
    def test_valid_numeric_claim_item(self) -> None:
        item = NumericClaimItem(
            claim_id="c1",
            path="$.summary",
            value=100.0,
            metric="revenue",
            period="FY2024",
            currency="USD",
            unit="usd_millions",
        )
        self.assertEqual(item.claim_id, "c1")
        self.assertEqual(item.source_kind, "text")

    def test_extra_fields_forbidden(self) -> None:
        with self.assertRaises(ValidationError):
            NumericClaimItem(
                claim_id="c1",
                path="$.summary",
                value=100.0,
                metric="rev",
                period="FY24",
                unit="usd_millions",
                forged_field="adversarial_payload",  # type: ignore[call-arg]
            )

    def test_field_length_limits(self) -> None:
        with self.assertRaises(ValidationError):
            NumericClaimItem(
                claim_id="c" * 201,
                path="$.summary",
                value=100.0,
                metric="rev",
                period="FY24",
                unit="usd_millions",
            )

    def test_source_kind_literal_validation(self) -> None:
        for kind in ("text", "fact", "arithmetic"):
            item = NumericClaimItem(
                claim_id="c1",
                path="$.summary",
                value=10.0,
                metric="m",
                period="p",
                unit="usd_millions",
                source_kind=kind,  # type: ignore[arg-type]
            )
            self.assertEqual(item.source_kind, kind)
        with self.assertRaises(ValidationError):
            NumericClaimItem(
                claim_id="c1",
                path="$.summary",
                value=10.0,
                metric="m",
                period="p",
                unit="usd_millions",
                source_kind="unsupported_kind",  # type: ignore[arg-type]
            )

    def test_investment_report_bounds_numeric_claims_at_40(self) -> None:
        valid_claims = [
            NumericClaimItem(
                claim_id=f"c_{i}",
                path="$.summary",
                value=i,
                metric="m",
                period="p",
                unit="usd_millions",
            )
            for i in range(41)
        ]
        with self.assertRaises(ValidationError):
            InvestmentReport.model_validate(
                {
                    "classification": {
                        "document_type": "10-K",
                        "sector": "Tech",
                        "industry": "Software",
                        "region": "US",
                        "confidence": "high",
                    },
                    "qualitative": {},
                    "summary": "Summary text",
                    "thesis": "Thesis text",
                    "counter_thesis": "Counter thesis text",
                    "materiality_assessment": {},
                    "numeric_claims": [c.model_dump() for c in valid_claims],
                }
            )


if __name__ == "__main__":
    unittest.main()
