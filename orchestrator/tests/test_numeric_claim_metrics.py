"""Tests for numeric claim value, metric, period, and unit validation."""

from __future__ import annotations

import unittest

from investment_schemas import NUMERIC_CLAIM_UNITS, validate_numeric_claim_rows


class TestNumericClaimMetrics(unittest.TestCase):
    def test_finite_number_values_accepted(self) -> None:
        valid_values = [100, 0, -50.2, "123.45", "$1,000.50", "45.6%"]
        for val in valid_values:
            claims = [{"claim_id": "c1", "value": val, "metric": "m", "period": "FY24", "currency": "USD"}]
            self.assertEqual(validate_numeric_claim_rows(claims), [], f"Value {val} failed")

    def test_non_numeric_values_rejected(self) -> None:
        invalid_values = ["not-a-number", None, float("inf"), float("-inf"), float("nan"), True, False, []]
        for val in invalid_values:
            claims = [{"claim_id": "c1", "value": val, "metric": "m", "period": "FY24", "currency": "USD"}]
            problems = validate_numeric_claim_rows(claims)
            self.assertTrue(
                any("value: must be a finite number or valid numeric string" in p for p in problems),
                f"Value {val} unexpectedly passed",
            )

    def test_blank_metric_rejected(self) -> None:
        claims = [{"claim_id": "c1", "value": 10, "metric": "  ", "period": "FY24", "currency": "USD"}]
        problems = validate_numeric_claim_rows(claims)
        self.assertIn("$.numeric_claims[0].metric: must be a nonblank string", problems)

    def test_blank_period_rejected(self) -> None:
        claims = [{"claim_id": "c1", "value": 10, "metric": "rev", "period": "", "currency": "USD"}]
        problems = validate_numeric_claim_rows(claims)
        self.assertIn("$.numeric_claims[0].period: must be a nonblank string", problems)

    def test_valid_units_and_currency_accepted(self) -> None:
        for unit in list(NUMERIC_CLAIM_UNITS)[:5]:
            claims = [{"claim_id": "c1", "value": 10, "metric": "m", "period": "FY24", "unit": unit}]
            self.assertEqual(validate_numeric_claim_rows(claims), [])
        claims = [{"claim_id": "c1", "value": 10, "metric": "m", "period": "FY24", "currency": "EUR"}]
        self.assertEqual(validate_numeric_claim_rows(claims), [])

    def test_invalid_unit_and_missing_currency_rejected(self) -> None:
        claims = [{"claim_id": "c1", "value": 10, "metric": "m", "period": "FY24", "unit": "invalid_unit"}]
        problems = validate_numeric_claim_rows(claims)
        self.assertIn("$.numeric_claims[0]: must have a valid unit or currency", problems)

    def test_missing_unit_and_currency_rejected(self) -> None:
        claims = [{"claim_id": "c1", "value": 10, "metric": "m", "period": "FY24"}]
        problems = validate_numeric_claim_rows(claims)
        self.assertIn("$.numeric_claims[0]: must have a valid unit or currency", problems)


if __name__ == "__main__":
    unittest.main()
