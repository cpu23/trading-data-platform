from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from collectors.company_expectations import (
    CompanyExpectationsCollector,
    _forecast_rows,
    _institutional_positioning,
    _short_interest_history,
)
from events.publisher import map_record

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class CompanyExpectationsCollectorTests(unittest.TestCase):
    def _config(self):
        return {
            "collectors": {
                "company_expectations": {
                    "enabled": True,
                    "base_url": "https://api.nasdaq.com/api",
                    "symbols": ["AAPL"],
                    "max_symbols": 10,
                    "lookback_days": 2,
                    "timeout_seconds": 20,
                }
            }
        }

    @patch("collectors.company_expectations.make_request")
    def test_collect_preserves_revision_dispersion_and_announced_catalyst(
        self, request
    ):
        request.side_effect = [
            _response(
                {
                    "data": {
                        "rows": [
                            {
                                "symbol": "AAPL",
                                "name": "Apple Inc.",
                                "time": "time-after-hours",
                                "fiscalQuarterEnding": "Sep/2026",
                                "epsForecast": "$1.98",
                                "noOfEsts": "8",
                                "lastYearEPS": "$1.64",
                            }
                        ]
                    }
                }
            ),
            _response({"data": {"rows": []}}),
            _response(
                {
                    "data": {
                        "symbol": "aapl",
                        "quarterlyForecast": {
                            "rows": [
                                {
                                    "fiscalEnd": "Sep 2026",
                                    "consensusEPSForecast": 1.98,
                                    "highEPSForecast": 2.09,
                                    "lowEPSForecast": 1.91,
                                    "noOfEstimates": 8,
                                    "up": 2,
                                    "down": 4,
                                }
                            ]
                        },
                        "yearlyForecast": {"rows": []},
                    }
                }
            ),
            _response(
                {
                    "data": {
                        "ownershipSummary": {
                            "SharesOutstandingPCT": {"value": "76.59%"},
                            "ShareoutstandingTotal": {"value": "14,594"},
                            "TotalHoldingsValue": {"value": "$3,419,603"},
                        },
                        "activePositions": {
                            "rows": [
                                {
                                    "positions": "Increased Positions",
                                    "holders": "2,862",
                                    "shares": "335,113,546",
                                },
                                {
                                    "positions": "Decreased Positions",
                                    "holders": "3,194",
                                    "shares": "243,033,146",
                                },
                            ]
                        },
                        "newSoldOutPositions": {"rows": []},
                        "holdingsTransactions": {
                            "table": {
                                "rows": [
                                    {
                                        "ownerName": "Vanguard Group Inc",
                                        "date": "6/30/2026",
                                        "sharesHeld": "1,426,283,914",
                                        "sharesChange": "26,856,752",
                                        "sharesChangePCT": "1.919%",
                                        "marketValue": "$436,343,038",
                                    }
                                ]
                            }
                        },
                    }
                }
            ),
            _response(
                {
                    "data": {
                        "shortInterestTable": {
                            "rows": [
                                {
                                    "settlementDate": "07/31/2026",
                                    "interest": "141,606,163",
                                    "avgDailyShareVolume": "58,400,983",
                                    "daysToCover": 2.424722,
                                }
                            ]
                        }
                    }
                }
            ),
        ]

        result = CompanyExpectationsCollector().collect(
            self._config(), "corr-1", now=NOW
        )

        self.assertEqual(result.successful_series, 1)
        self.assertEqual(result.metrics["api_calls_made"], 5)
        self.assertEqual(len(result.records), 1)
        row = result.records[0]
        self.assertEqual(row["source"], "company_expectations")
        self.assertEqual(row["published_at"], NOW)
        self.assertEqual(row["acquired_at"], NOW)
        self.assertEqual(row["metadata"]["ticker"], "AAPL")
        forecast = row["metadata"]["quarterly"][0]
        self.assertEqual(forecast["consensusEPSForecast"], 1.98)
        self.assertEqual(forecast["highEPSForecast"], 2.09)
        self.assertEqual(forecast["lowEPSForecast"], 1.91)
        self.assertEqual(forecast["up"], 2)
        self.assertEqual(forecast["down"], 4)
        self.assertEqual(row["metadata"]["next_earnings"]["reportDate"], "2026-08-16")
        self.assertIn("earnings catalyst", row["content"])
        self.assertEqual(
            row["metadata"]["institutional_positioning"]["institutional_ownership_pct"],
            76.59,
        )
        self.assertEqual(
            row["metadata"]["institutional_positioning"]["top_holders"][0][
                "shares_change"
            ],
            26_856_752,
        )
        self.assertEqual(
            row["metadata"]["short_interest"][0]["short_interest_shares"],
            141_606_163,
        )
        self.assertIs(row["metadata"]["borrow"]["available"], False)
        self.assertIn("no borrow cost", row["content"])

        source_event_id, raw, payload = map_record(row, source="company_expectations")
        self.assertEqual(source_event_id, row["document_id"])
        self.assertEqual(raw["source"], "company_expectations")
        self.assertEqual(payload["document_type"], "consensus_snapshot")

    def test_missing_forecast_fields_remain_missing(self):
        rows = _forecast_rows(
            [
                {
                    "fiscalEnd": "Dec 2026",
                    "consensusEPSForecast": "N/A",
                    "highEPSForecast": None,
                    "lowEPSForecast": "--",
                    "noOfEstimates": "3",
                    "up": "0",
                    "down": "1",
                }
            ]
        )
        self.assertEqual(
            rows,
            [{"fiscalEnd": "Dec 2026", "noOfEstimates": 3, "up": 0, "down": 1}],
        )

    def test_positioning_parsers_reject_malformed_rows_without_inference(self):
        self.assertIsNone(_institutional_positioning({"data": None}))
        self.assertEqual(
            _short_interest_history(
                {
                    "data": {
                        "shortInterestTable": {
                            "rows": [
                                {
                                    "settlementDate": "not-a-date",
                                    "interest": "99",
                                }
                            ]
                        }
                    }
                }
            ),
            [],
        )

    @patch("collectors.company_expectations.make_request")
    def test_valid_empty_forecast_is_not_a_collection_failure(self, request):
        request.side_effect = [
            _response({"data": {"rows": []}}),
            _response({"data": {"rows": []}}),
            _response(
                {
                    "data": {
                        "quarterlyForecast": {"rows": []},
                        "yearlyForecast": {"rows": []},
                    }
                }
            ),
            _response({"data": None}),
            _response({"data": None}),
        ]
        result = CompanyExpectationsCollector().collect(
            self._config(), "corr-empty", now=NOW
        )
        self.assertEqual(result.successful_series, 1)
        self.assertEqual(result.records, [])
        self.assertEqual(result.errors, [])

    @patch("collectors.company_expectations.make_request")
    def test_malformed_success_envelope_is_a_collection_error(self, request):
        request.side_effect = [
            _response({"data": {"rows": []}}),
            _response({"data": {"rows": []}}),
            _response({"data": None}),
        ]

        result = CompanyExpectationsCollector().collect(
            self._config(), "corr-malformed", now=NOW
        )

        self.assertEqual(result.successful_series, 0)
        self.assertEqual(result.records, [])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    @patch("collectors.company_expectations.make_request")
    def test_identical_day_has_stable_document_identity(self, request):
        payloads = [
            {"data": {"rows": []}},
            {"data": {"rows": []}},
            {
                "data": {
                    "quarterlyForecast": {
                        "rows": [
                            {"fiscalEnd": "Sep 2026", "consensusEPSForecast": 1.98}
                        ]
                    },
                    "yearlyForecast": {"rows": []},
                }
            },
            {"data": None},
            {"data": None},
        ]
        request.side_effect = [_response(value) for value in payloads * 2]
        collector = CompanyExpectationsCollector()
        first = collector.collect(self._config(), "corr-1", now=NOW)
        second = collector.collect(self._config(), "corr-2", now=NOW)
        self.assertEqual(
            first.records[0]["document_id"], second.records[0]["document_id"]
        )


if __name__ == "__main__":
    unittest.main()
