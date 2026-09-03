"""Tests for numeric claims domain and structure validation."""

from __future__ import annotations

import unittest

from investment_schemas import validate_numeric_claim_rows


class TestNumericClaimDomains(unittest.TestCase):
    def test_valid_numeric_claims_pass(self) -> None:
        claims = [
            {
                "claim_id": "c1",
                "value": 100.5,
                "metric": "revenue",
                "period": "FY2024",
                "currency": "USD",
            },
            {
                "claim_id": "c2",
                "value": "15.2%",
                "metric": "operating_margin",
                "period": "FY2024",
                "unit": "percent",
            },
        ]
        self.assertEqual(validate_numeric_claim_rows(claims), [])

    def test_none_and_empty_claims_pass(self) -> None:
        self.assertEqual(validate_numeric_claim_rows(None), [])
        self.assertEqual(validate_numeric_claim_rows([]), [])

    def test_non_list_claims_rejected(self) -> None:
        problems = validate_numeric_claim_rows("not-a-list")  # type: ignore[arg-type]
        self.assertEqual(problems, ["$.numeric_claims: must be an array"])

    def test_non_dict_row_rejected(self) -> None:
        problems = validate_numeric_claim_rows(["bad-row"])  # type: ignore[list-item]
        self.assertEqual(problems, ["$.numeric_claims[0]: must be an object"])

    def test_duplicate_claim_ids_rejected(self) -> None:
        claims = [
            {"claim_id": "c1", "value": 10, "metric": "rev", "period": "2024", "currency": "USD"},
            {"claim_id": "c1", "value": 20, "metric": "ebit", "period": "2024", "currency": "USD"},
        ]
        problems = validate_numeric_claim_rows(claims)
        self.assertTrue(any("duplicate claim_id 'c1'" in p for p in problems))

    def test_blank_and_missing_claim_ids_rejected(self) -> None:
        claims = [
            {"value": 10, "metric": "rev", "period": "2024", "currency": "USD"},
            {"claim_id": "   ", "value": 20, "metric": "ebit", "period": "2024", "currency": "USD"},
        ]
        problems = validate_numeric_claim_rows(claims)
        self.assertIn("$.numeric_claims[0].claim_id: must be a nonblank string", problems)
        self.assertIn("$.numeric_claims[1].claim_id: must be a nonblank string", problems)

    def test_more_than_40_claims_rejected(self) -> None:
        claims = [
            {"claim_id": f"c_{i}", "value": i, "metric": "m", "period": "2024", "currency": "USD"}
            for i in range(41)
        ]
        problems = validate_numeric_claim_rows(claims)
        self.assertIn("$.numeric_claims: must contain at most 40 items", problems)


if __name__ == "__main__":
    unittest.main()
