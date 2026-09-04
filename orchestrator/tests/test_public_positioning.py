import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.base import CollectorNoData, CollectorSetupRequired
from collectors.public_positioning import (
    FinraShortVolumeCollector,
    SecForm4Collector,
)

from contracts.runtime_config import CollectorConfig

SEC_CONFIG = {
    "collectors": {
        "sec_form4": {
            "request_interval_seconds": 0,
            "max_concurrency": 2,
            "issuers": [
                {
                    "cik": "0000320193",
                    "symbol": "AAPL",
                    "name": "APPLE INC",
                    "assets": ["AAPL"],
                }
            ],
        }
    }
}

FINRA_CONFIG = {
    "collectors": {
        "finra_short_volume": {
            "request_interval_seconds": 0,
            "symbols": [{"symbol": "AAPL", "assets": ["AAPL"]}],
            "dates": ["2026-08-13"],
        }
    }
}


def _json_response(payload):
    response = Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    response.headers = {}
    response.content = b"{}"
    return response


def _text_response(content: bytes, status_code: int = 200):
    response = Mock()
    response.content = content
    response.status_code = status_code
    response.raise_for_status.return_value = None
    response.headers = {}
    return response


def _submissions_payload(entries):
    return {
        "cik": "320193",
        "name": "APPLE INC",
        "filings": {
            "recent": {
                "form": [entry["form"] for entry in entries],
                "accessionNumber": [entry["accession"] for entry in entries],
                "filingDate": [entry["filing_date"] for entry in entries],
                "primaryDocument": [
                    entry.get("primary_document", "form4.xml") for entry in entries
                ],
                "primaryDocDescription": [
                    entry.get("description", "") for entry in entries
                ],
            }
        },
    }


def _form4_xml(
    document_type="4",
    symbol="AAPL",
    issuer_cik="0000320193",
    owner_cik="0000123456",
    owner_name="JANE DOE",
    transactions=(),
):
    rows = []
    for tx in transactions:
        rows.append(
            f"""
        <nonDerivativeTransaction>
          <securityTitle><value>{tx["security"]}</value></securityTitle>
          <transactionDate><value>{tx["date"]}</value></transactionDate>
          <transactionCoding>
            <transactionFormType>{tx.get("form", document_type)}</transactionFormType>
            <transactionCode>{tx["code"]}</transactionCode>
            <equitySwapInvolved>0</equitySwapInvolved>
          </transactionCoding>
          <transactionAmounts>
            <transactionShares><value>{tx["shares"]}</value></transactionShares>
            <transactionPricePerShare><value>{tx.get("price", "")}</value></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>{tx["disposed"]}</value></transactionAcquiredDisposedCode>
          </transactionAmounts>
        </nonDerivativeTransaction>"""
        )
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0105</schemaVersion>
  <documentType>{document_type}</documentType>
  <periodOfReport>2026-08-05</periodOfReport>
  <issuer>
    <issuerCik>{issuer_cik}</issuerCik>
    <issuerName>APPLE INC</issuerName>
    <issuerTradingSymbol>{symbol}</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{owner_cik}</rptOwnerCik>
      <rptOwnerName>{owner_name}</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>{"".join(rows)}</nonDerivativeTable>
</ownershipDocument>"""


def _sec_request_side_effect(submissions, documents, failures=None):
    """Dispatch SEC requests: submissions URL vs archive URLs (by accession).

    ``documents`` maps an accession-without-dashes to bytes (or None to raise).
    """
    failures = failures or {}

    def handler(method, url, **kwargs):
        if "data.sec.gov/submissions/CIK" in url:
            if url in failures:
                raise failures[url]
            return _json_response(submissions)
        for accession, content in documents.items():
            if accession in url:
                if content is None:
                    raise RuntimeError("boom")
                return _text_response(content)
        raise AssertionError(f"unexpected SEC url: {url}")

    return handler


class SecForm4CollectorTests(unittest.TestCase):
    def test_form4_buy_sell_aggregated_per_transaction_date(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000001",
                    "filing_date": "2026-08-06",
                },
                {
                    "form": "4",
                    "accession": "0000320193-26-000002",
                    "filing_date": "2026-08-07",
                },
            ]
        )
        documents = {
            "000032019326000001": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "P",
                        "shares": "500",
                        "price": "200.00",
                        "disposed": "A",
                    },
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "G",
                        "shares": "100",
                        "disposed": "D",
                    },
                ]
            ).encode(),
            "000032019326000002": _form4_xml(
                owner_cik="0000654321",
                owner_name="JOHN SMITH",
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "S",
                        "shares": "300",
                        "price": "205.50",
                        "disposed": "D",
                    }
                ],
            ).encode(),
        }
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, documents),
        ):
            records = SecForm4Collector().collect(SEC_CONFIG, "corr")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "sec_form4")
        self.assertEqual(record["market_id"], "AAPL")
        self.assertEqual(record["report_date"], date(2026, 8, 5))
        self.assertEqual(record["category"], "insider_transactions")
        self.assertEqual(record["long_positions"], 500)
        self.assertEqual(record["short_positions"], 300)
        self.assertEqual(record["net_position"], 200)
        self.assertIsNone(record["open_interest"])
        metadata = record["metadata"]
        self.assertEqual(metadata["positioning_kind"], "insider_activity")
        self.assertEqual(metadata["buy_value_usd"], "100000.00")
        self.assertEqual(metadata["sell_value_usd"], "61650.00")
        self.assertEqual(metadata["buy_transaction_count"], 1)
        self.assertEqual(metadata["sell_transaction_count"], 1)
        self.assertEqual(metadata["other_transaction_count"], 1)
        self.assertEqual(metadata["owner_count"], 2)
        self.assertEqual(
            metadata["accession_numbers"],
            ["0000320193-26-000001", "0000320193-26-000002"],
        )
        self.assertEqual(metadata["assets"], ["AAPL"])

    def test_collect_accepts_validated_issuer_mapping_and_default_origins(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000001",
                    "filing_date": "2026-08-06",
                }
            ]
        )
        documents = {
            "000032019326000001": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "P",
                        "shares": "50",
                        "price": "200.00",
                        "disposed": "A",
                    }
                ]
            ).encode()
        }
        section = CollectorConfig(
            issuers=[{"cik": "0000320193", "symbol": "AAPL"}],
            request_interval_seconds=0,
            max_concurrency=1,
        )
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, documents),
        ):
            records = SecForm4Collector().collect(
                {"collectors": {"sec_form4": section}},
                "typed-config",
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["market_id"], "AAPL")

    def test_amendment_supersedes_original_and_duplicates_collapse(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000011",
                    "filing_date": "2026-08-06",
                },
                {
                    "form": "4/A",
                    "accession": "0000320193-26-000012",
                    "filing_date": "2026-08-08",
                },
            ]
        )
        documents = {
            "000032019326000011": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "P",
                        "shares": "100",
                        "price": "100.00",
                        "disposed": "A",
                    },
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "P",
                        "shares": "100",
                        "price": "100.00",
                        "disposed": "A",
                    },
                ]
            ).encode(),
            "000032019326000012": _form4_xml(
                document_type="4/A",
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "P",
                        "shares": "150",
                        "price": "100.00",
                        "disposed": "A",
                    }
                ],
            ).encode(),
        }
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, documents),
        ):
            collector = SecForm4Collector()
            records = collector.collect(SEC_CONFIG, "corr")

        self.assertEqual(len(records), 1)
        # The 4/A restated 150 shares supersedes the 100-share original; the
        # duplicate row inside the original would have collapsed anyway.
        self.assertEqual(records[0]["long_positions"], 150)
        self.assertEqual(
            records[0]["metadata"]["amendment_accession_numbers"],
            ["0000320193-26-000012"],
        )
        stats = collector.last_result_metadata["issuer_stats"][0]
        self.assertEqual(stats["superseded_transactions"], 1)

    def test_partial_failure_is_isolated_per_issuer(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000021",
                    "filing_date": "2026-08-10",
                }
            ]
        )
        documents = {
            "000032019326000021": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-10",
                        "code": "P",
                        "shares": "10",
                        "disposed": "A",
                    }
                ]
            ).encode()
        }
        failures = {
            "https://data.sec.gov/submissions/CIK0000789012.json": RuntimeError(
                "rate limited"
            )
        }
        config = {
            "collectors": {
                "sec_form4": {
                    "request_interval_seconds": 0,
                    "issuers": [
                        {"cik": "0000320193", "symbol": "AAPL"},
                        {"cik": "0000789012", "symbol": "MSFT"},
                    ],
                }
            }
        }
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(
                submissions, documents, failures=failures
            ),
        ):
            collector = SecForm4Collector()
            records = collector.collect(config, "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["market_id"], "AAPL")
        self.assertEqual(collector.last_result_metadata["state"], "partial")
        self.assertEqual(
            collector.last_result_metadata["issuers_failed"][0]["cik"],
            "0000789012",
        )

    def test_all_issuers_failing_raises_no_data(self):
        failures = {
            "https://data.sec.gov/submissions/CIK0000320193.json": RuntimeError(
                "rate limited"
            )
        }
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect({}, {}, failures=failures),
        ):
            with self.assertRaises(CollectorNoData) as caught:
                SecForm4Collector().collect(SEC_CONFIG, "corr")
        self.assertEqual(
            caught.exception.metadata["failed_issuers"][0]["cik"], "0000320193"
        )

    def test_lookback_window_and_filing_cap_bound_downloads(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000033",
                    "filing_date": "2026-08-11",
                    "primary_document": "xslF345X06/form4.xml",
                },
                {
                    "form": "4",
                    "accession": "0000320193-26-000032",
                    "filing_date": "2026-08-10",
                },
                {
                    "form": "4",
                    "accession": "0000320193-26-000031",
                    "filing_date": "2026-01-10",  # outside the 180-day window
                },
            ]
        )
        documents = {
            "000032019326000032": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-10",
                        "code": "P",
                        "shares": "10",
                        "disposed": "A",
                    }
                ]
            ).encode(),
            "000032019326000033": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-11",
                        "code": "P",
                        "shares": "20",
                        "disposed": "A",
                    }
                ]
            ).encode(),
        }
        config = {
            "collectors": {
                "sec_form4": {
                    "request_interval_seconds": 0,
                    "lookback_days": 180,
                    "max_filings_per_issuer": 1,
                    "issuers": [{"cik": "0000320193", "symbol": "AAPL"}],
                }
            }
        }
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, documents),
        ) as request:
            records = SecForm4Collector().collect(config, "corr")

        # Only the single newest in-window filing was fetched.
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["report_date"], date(2026, 8, 11))
        archive_requests = [
            call
            for call in request.call_args_list
            if "/Archives/edgar/data/" in call.args[1]
        ]
        self.assertEqual(len(archive_requests), 1)
        self.assertIn("000032019326000033", archive_requests[0].args[1])
        self.assertTrue(archive_requests[0].args[1].endswith("/form4.xml"))
        self.assertNotIn("xslF345X06", archive_requests[0].args[1])

    def test_malformed_document_fails_explicitly(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000041",
                    "filing_date": "2026-08-10",
                }
            ]
        )
        documents = {"000032019326000041": b"<not-xml"}
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, documents),
        ):
            with self.assertRaises(CollectorNoData) as caught:
                SecForm4Collector().collect(SEC_CONFIG, "corr")
        self.assertEqual(
            caught.exception.metadata["failed_issuers"][0]["code"],
            "invalid_source_data",
        )
        self.assertEqual(
            caught.exception.metadata["failed_issuers"][0]["cik"], "0000320193"
        )

    def test_no_data_for_issuer_without_form4_is_valid_empty(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "10-Q",
                    "accession": "0000320193-26-000051",
                    "filing_date": "2026-08-10",
                }
            ]
        )
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, {}),
        ) as request:
            collector = SecForm4Collector()
            with self.assertRaises(CollectorNoData):
                collector.collect(SEC_CONFIG, "corr")
        # No ownership documents were fetched; zero fabricated records.
        archive_requests = [
            call
            for call in request.call_args_list
            if "/Archives/edgar/data/" in call.args[1]
        ]
        self.assertEqual(archive_requests, [])

    def test_requires_configured_issuers(self):
        with self.assertRaises(CollectorSetupRequired):
            SecForm4Collector().collect({"collectors": {"sec_form4": {}}}, "corr")

    def test_timestamps_distinguish_source_and_acquisition(self):
        submissions = _submissions_payload(
            [
                {
                    "form": "4",
                    "accession": "0000320193-26-000061",
                    "filing_date": "2026-08-10",
                }
            ]
        )
        documents = {
            "000032019326000061": _form4_xml(
                transactions=[
                    {
                        "security": "Common Stock",
                        "date": "2026-08-05",
                        "code": "P",
                        "shares": "10",
                        "disposed": "A",
                    }
                ]
            ).encode()
        }
        with patch(
            "collectors.public_positioning.make_request",
            side_effect=_sec_request_side_effect(submissions, documents),
        ):
            records = SecForm4Collector().collect(SEC_CONFIG, "corr")

        record = records[0]
        self.assertEqual(record["report_date"], date(2026, 8, 5))
        self.assertEqual(record["metadata"]["source_time"], "2026-08-05")
        self.assertEqual(record["metadata"]["source_time_kind"], "transaction_date")
        self.assertIsInstance(record["acquired_at"], datetime)
        self.assertEqual(
            record["metadata"]["acquired_at"], record["acquired_at"].isoformat()
        )
        self.assertNotEqual(
            record["metadata"]["source_time"], record["metadata"]["acquired_at"]
        )

    def test_health_check_and_contract_methods(self):
        response = Mock()
        response.status_code = 200
        response.headers = {}
        with patch(
            "collectors.public_positioning.make_request", return_value=response
        ) as request:
            result = SecForm4Collector().health_check(SEC_CONFIG)
        self.assertTrue(result["healthy"])
        self.assertEqual(
            request.call_args.args[1],
            "https://data.sec.gov/submissions/CIK0000320193.json",
        )
        self.assertIn("User-Agent", request.call_args.kwargs["headers"])

        collector = SecForm4Collector()
        self.assertEqual(collector.get_target_table(), "positioning_reports")
        self.assertEqual(
            collector.get_conflict_columns(),
            ["source", "market_id", "report_date", "category"],
        )
        self.assertEqual(
            collector.get_schedule(
                {"collectors": {"sec_form4": {"schedule": "0 12 * * 1-5"}}}
            ),
            "0 12 * * 1-5",
        )


FINRA_SAMPLE = (
    "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
    "20260813|AAPL|4566073.748830|4800|12701390.568444|B,Q,N\n"
    "20260813|AAPL|1000.5|0|2000|Q\n"
    "20260813|MSFT|100|0|500|B,Q,N\n"
)


class FinraShortVolumeCollectorTests(unittest.TestCase):
    def test_short_volume_aggregated_across_market_rows(self):
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(FINRA_SAMPLE.encode()),
        ) as request:
            collector = FinraShortVolumeCollector()
            records = collector.collect(FINRA_CONFIG, "corr")

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "finra_short_volume")
        self.assertEqual(record["market_id"], "AAPL")
        self.assertEqual(record["report_date"], date(2026, 8, 13))
        self.assertEqual(record["category"], "short_volume")
        # Rows across markets (B,Q,N and Q) sum; columns hold rounded shares.
        self.assertEqual(record["long_positions"], 12703391)
        self.assertEqual(record["short_positions"], 4567074)
        self.assertEqual(record["net_position"], 8136316)
        metadata = record["metadata"]
        self.assertEqual(metadata["short_volume_exact"], "4567074.248830")
        self.assertEqual(metadata["short_exempt_volume_exact"], "4800")
        self.assertEqual(metadata["total_volume_exact"], "12703390.568444")
        self.assertEqual(metadata["market_codes"], ["B", "N", "Q"])
        self.assertEqual(metadata["row_count"], 2)
        self.assertEqual(metadata["assets"], ["AAPL"])
        self.assertEqual(
            request.call_args.args[1],
            "https://cdn.finra.org/equity/regsho/daily/CNMSshvol20260813.txt",
        )

    def test_collect_accepts_validated_symbol_mapping(self):
        section = CollectorConfig(
            request_interval_seconds=0,
            symbols=[{"symbol": "AAPL", "assets": ["AAPL"]}],
            dates=["2026-08-13"],
        )
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(FINRA_SAMPLE.encode()),
        ):
            records = FinraShortVolumeCollector().collect(
                {"collectors": {"finra_short_volume": section}},
                "typed-config",
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["market_id"], "AAPL")
        self.assertEqual(records[0]["metadata"]["assets"], ["AAPL"])

    def test_short_volume_is_never_labeled_short_interest(self):
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(FINRA_SAMPLE.encode()),
        ):
            records = FinraShortVolumeCollector().collect(FINRA_CONFIG, "corr")

        metadata = records[0]["metadata"]
        self.assertEqual(metadata["positioning_kind"], "short_volume")
        self.assertNotEqual(metadata["positioning_kind"], "short_interest")
        # Daily short volume is a delayed proxy/flow measure, never short
        # interest; both semantics and delay_note must say so explicitly.
        semantics = metadata["semantics"].lower()
        self.assertIn("delayed", semantics)
        self.assertIn("proxy", semantics)
        self.assertIn("flow", semantics)
        self.assertIn("not short interest", semantics)
        delay_note = metadata["delay_note"].lower()
        self.assertIn("delayed", delay_note)
        self.assertIn("proxy", delay_note)
        self.assertIn("flow", delay_note)
        self.assertIn("not short interest", delay_note)

    def test_missing_date_file_is_skipped_not_failed(self):
        def handler(method, url, **kwargs):
            if "20260809" in url:
                return _text_response(b"", status_code=404)
            return _text_response(FINRA_SAMPLE.encode())

        config = {
            "collectors": {
                "finra_short_volume": {
                    "request_interval_seconds": 0,
                    "symbols": ["AAPL"],
                    "dates": ["2026-08-09", "2026-08-13"],
                }
            }
        }
        with patch("collectors.public_positioning.make_request", side_effect=handler):
            collector = FinraShortVolumeCollector()
            records = collector.collect(config, "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["report_date"], date(2026, 8, 13))
        self.assertEqual(
            collector.last_result_metadata["dates_skipped"], ["2026-08-09"]
        )
        self.assertEqual(collector.last_result_metadata["dates_failed"], [])
        self.assertEqual(collector.last_result_metadata["state"], "success")

    def test_partial_failure_per_date(self):
        def handler(method, url, **kwargs):
            if "20260812" in url:
                response = Mock()
                response.status_code = 500
                response.headers = {}
                response.raise_for_status.side_effect = RuntimeError("server error")
                return response
            return _text_response(FINRA_SAMPLE.encode())

        config = {
            "collectors": {
                "finra_short_volume": {
                    "request_interval_seconds": 0,
                    "symbols": ["AAPL"],
                    "dates": ["2026-08-12", "2026-08-13"],
                }
            }
        }
        with patch("collectors.public_positioning.make_request", side_effect=handler):
            collector = FinraShortVolumeCollector()
            records = collector.collect(config, "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(collector.last_result_metadata["state"], "partial")
        self.assertEqual(
            collector.last_result_metadata["dates_failed"][0]["date"],
            "2026-08-12",
        )

    def test_all_dates_missing_raises_no_data(self):
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(b"", status_code=404),
        ):
            with self.assertRaises(CollectorNoData) as caught:
                FinraShortVolumeCollector().collect(FINRA_CONFIG, "corr")
        self.assertIn("2026-08-13", caught.exception.metadata["skipped_dates"])

    def test_malformed_rows_and_trailer_are_explicitly_skipped(self):
        content = (
            "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            "20260813|AAPL|100|0|200|Q\n"
            "20260813|AAPL|-5|0|200|Q\n"  # negative short volume -> malformed
            "20260812|AAPL|999|0|999|Q\n"  # wrong date -> malformed
            "Totals|9999|0|9999|ALL\n"  # trailer -> non-data row
        ).encode("ascii")
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(content),
        ):
            collector = FinraShortVolumeCollector()
            records = collector.collect(FINRA_CONFIG, "corr")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["short_positions"], 100)
        self.assertEqual(collector.last_result_metadata["malformed_rows"], 2)
        self.assertEqual(collector.last_result_metadata["non_data_rows"], 1)

    def test_headerless_file_fails_explicitly(self):
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(b"20260813|AAPL|1|0|2|Q\n"),
        ):
            with self.assertRaises(CollectorNoData) as caught:
                FinraShortVolumeCollector().collect(FINRA_CONFIG, "corr")
        self.assertEqual(
            caught.exception.metadata["failed_dates"][0]["code"],
            "invalid_source_data",
        )

    def test_lookback_and_file_size_bounds(self):
        # lookback_days is clamped to 30, so at most 30 files are downloaded.
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(b"", status_code=404),
        ) as request:
            with self.assertRaises(CollectorNoData):
                FinraShortVolumeCollector().collect(
                    {
                        "collectors": {
                            "finra_short_volume": {
                                "request_interval_seconds": 0,
                                "symbols": ["AAPL"],
                                "lookback_days": 400,
                            }
                        }
                    },
                    "corr",
                )
        self.assertEqual(request.call_count, 30)

        # A file larger than the configured bound fails that date explicitly.
        response = Mock()
        response.content = b"x"
        response.status_code = 200
        response.headers = {"Content-Length": "5000"}
        response.raise_for_status.return_value = None
        with patch("collectors.public_positioning.make_request", return_value=response):
            with self.assertRaises(CollectorNoData) as caught:
                FinraShortVolumeCollector().collect(
                    {
                        "collectors": {
                            "finra_short_volume": {
                                "request_interval_seconds": 0,
                                "symbols": ["AAPL"],
                                "max_file_bytes": 1000,
                                "dates": ["2026-08-13"],
                            }
                        }
                    },
                    "corr",
                )
        self.assertEqual(
            caught.exception.metadata["failed_dates"][0]["code"],
            "invalid_source_data",
        )

    def test_requires_configured_symbols(self):
        with self.assertRaises(CollectorSetupRequired):
            FinraShortVolumeCollector().collect(
                {"collectors": {"finra_short_volume": {}}}, "corr"
            )

    def test_timestamps_distinguish_trade_date_and_acquisition(self):
        with patch(
            "collectors.public_positioning.make_request",
            return_value=_text_response(FINRA_SAMPLE.encode()),
        ):
            records = FinraShortVolumeCollector().collect(FINRA_CONFIG, "corr")

        record = records[0]
        self.assertEqual(record["report_date"], date(2026, 8, 13))
        self.assertEqual(record["metadata"]["source_time"], "2026-08-13")
        self.assertEqual(record["metadata"]["source_time_kind"], "trade_date")
        self.assertIsInstance(record["acquired_at"], datetime)
        self.assertEqual(
            record["metadata"]["acquired_at"], record["acquired_at"].isoformat()
        )

    def test_health_check_and_contract_methods(self):
        def handler(method, url, **kwargs):
            if "20260814" in url:
                return _text_response(b"", status_code=404)
            return _text_response(b"x")

        with patch("collectors.public_positioning.make_request", side_effect=handler):
            result = FinraShortVolumeCollector().health_check(FINRA_CONFIG)
        self.assertTrue(result["healthy"])

        collector = FinraShortVolumeCollector()
        self.assertEqual(collector.get_target_table(), "positioning_reports")
        self.assertEqual(
            collector.get_conflict_columns(),
            ["source", "market_id", "report_date", "category"],
        )
        self.assertEqual(
            collector.get_schedule(
                {"collectors": {"finra_short_volume": {"schedule": "0 20 * * 1-5"}}}
            ),
            "0 20 * * 1-5",
        )


if __name__ == "__main__":
    unittest.main()
