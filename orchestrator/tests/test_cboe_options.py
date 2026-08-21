import json
import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import http_client
from collectors.cboe_options import (
    DEFAULT_BASE_URL,
    FEATURE_TABLE,
    FEATURE_VERSION,
    SOURCE_ID,
    CboeOptionsCollector,
)
from contracts.runtime_config import CollectorConfig
from errors import InvalidSourceData, TransientSourceError
from options_analytics import (
    STATE_INSUFFICIENT_HISTORY,
    STATE_NO_DATA,
    STATE_OK,
    analyze_chain,
)

PINNED_CAPTURED_AT = datetime(2026, 8, 14, 21, 5, 0, tzinfo=UTC)
SOURCE_TIME_UTC = datetime(2026, 8, 14, 20, 58, 53, 163568, tzinfo=UTC)


def _contract(
    symbol: str,
    exp: date,
    kind: str,
    strike_price: float,
    **fields,
) -> dict:
    """Build a provider option item from the OCC identity parts.

    ``exp``/``kind``/``strike_price`` encode the OCC symbol; any extra
    ``fields`` (for example a provider-side ``expiration`` or ``strike``) are
    merged in verbatim so identity-conflict cases can be exercised.
    """
    code = f"{int(round(strike_price * 1000)):08d}"
    return {
        "option": f"{symbol}{exp.strftime('%y%m%d')}{kind}{code}",
        **fields,
    }


def _spy_chain_payload() -> dict:
    """Representative delayed SPY chain: 3 expirations, 15 contracts,
    string and numeric values, full per-contract quote data."""
    exp_0821 = date(2026, 8, 21)
    exp_0918 = date(2026, 9, 18)
    exp_1218 = date(2026, 12, 18)
    options = [
        _contract(
            "SPY",
            exp_0821,
            "C",
            550,
            bid="0.45",
            ask="0.50",
            last_trade_price="0.47",
            volume=300,
            open_interest=4100,
            iv="0.190",
        ),
        _contract(
            "SPY",
            exp_0821,
            "P",
            555.5,
            bid="0.55",
            ask="0.60",
            last_trade_price="0.57",
            volume=250,
            open_interest=3300,
            iv="0.184",
        ),
        _contract(
            "SPY",
            exp_0821,
            "C",
            580,
            bid="5.20",
            ask="5.30",
            last_trade_price="5.25",
            volume=1200,
            open_interest=8500,
            iv="0.181",
        ),
        _contract(
            "SPY",
            exp_0821,
            "C",
            585,
            bid="2.40",
            ask="2.50",
            last_trade_price="2.45",
            volume=800,
            open_interest=6200,
            iv="0.172",
        ),
        _contract(
            "SPY",
            exp_0821,
            "C",
            586,
            bid="1.85",
            ask="1.95",
            last_trade_price="1.90",
            volume=1500,
            open_interest=9100,
            iv="0.168",
        ),
        _contract(
            "SPY",
            exp_0821,
            "P",
            586,
            bid="1.75",
            ask="1.85",
            last_trade_price="1.80",
            volume=1100,
            open_interest=8200,
            iv="0.174",
        ),
        _contract(
            "SPY",
            exp_0821,
            "C",
            590,
            bid="0.80",
            ask="0.90",
            last_trade_price="0.85",
            volume=700,
            open_interest=5400,
            iv="0.166",
        ),
        _contract(
            "SPY",
            exp_0821,
            "P",
            590,
            bid="1.10",
            ask="1.20",
            last_trade_price="1.15",
            volume=600,
            open_interest=4800,
            iv="0.171",
        ),
        _contract(
            "SPY",
            exp_0918,
            "C",
            585,
            bid="18.50",
            ask="18.90",
            last_trade_price="18.70",
            volume=2200,
            open_interest=15500,
            iv="0.195",
        ),
        _contract(
            "SPY",
            exp_0918,
            "C",
            590,
            bid="15.00",
            ask="15.40",
            last_trade_price="15.20",
            volume=1800,
            open_interest=12300,
            iv="0.193",
        ),
        _contract(
            "SPY",
            exp_0918,
            "P",
            590,
            bid="14.60",
            ask="15.00",
            last_trade_price="14.80",
            volume=1700,
            open_interest=11800,
            iv="0.196",
        ),
        _contract(
            "SPY",
            exp_0918,
            "P",
            585,
            bid="17.90",
            ask="18.30",
            last_trade_price="18.10",
            volume=2100,
            open_interest=14900,
            iv="0.199",
        ),
        _contract(
            "SPY",
            exp_1218,
            "C",
            580,
            bid="25.00",
            ask="25.60",
            last_trade_price="25.30",
            volume=900,
            open_interest=21000,
            iv="0.212",
        ),
        _contract(
            "SPY",
            exp_1218,
            "P",
            580,
            bid="24.20",
            ask="24.80",
            last_trade_price="24.50",
            volume=850,
            open_interest=19800,
            iv="0.217",
        ),
        _contract(
            "SPY",
            exp_1218,
            "C",
            600,
            bid="16.80",
            ask="17.40",
            last_trade_price="17.10",
            volume=750,
            open_interest=17400,
            iv="0.205",
        ),
    ]
    return {
        "data": {
            "symbol": "SPY",
            "security_type": "etf",
            "current_price": "586.40",
            "timestamp": "2026-08-14 15:58:53.163568",
            "data_date": "2026-08-14",
            "data_time": "15:58:53",
            "captured_at": "2026-08-14 15:58:54.307415",
            "microseconds": "2026-08-14 15:58:53.163568",
            "options": options,
        },
        "timestamp": "2026-08-14 15:58:53.163568",
    }


def _aapl_chain_payload() -> dict:
    return {
        "data": {
            "symbol": "AAPL",
            "current_price": "200.00",
            "timestamp": "2026-08-14 15:58:53.163568",
            "options": [
                _contract(
                    "AAPL",
                    date(2026, 8, 21),
                    "C",
                    200,
                    bid=4.0,
                    ask=4.2,
                    last_trade_price=4.1,
                    volume=500,
                    open_interest=3000,
                    iv=0.22,
                ),
            ],
        }
    }


class CboeOptionsCollectorTests(unittest.TestCase):
    def _config(self, **overrides):
        cfg = {
            "collectors": {
                SOURCE_ID: {
                    "schedule": "*/15 9-16 * * 1-5",
                    "symbols": ["SPY"],
                    **overrides,
                }
            }
        }
        return cfg

    def _response(self, payload, headers=None):
        return httpx.Response(200, json=payload, headers=headers or {})

    def _collect(self, make_request, payload=None, config=None):
        make_request.return_value = self._response(
            payload if payload is not None else _spy_chain_payload()
        )
        collector = CboeOptionsCollector()
        self.last_collector = collector
        self.last_result = collector.collect(
            config if config is not None else self._config(), correlation_id="cid"
        )
        return self.last_result.records

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_collect_accepts_validated_runtime_config_defaults(
        self, make_request, sleep, utc_now
    ):
        section = CollectorConfig(symbols=["SPY"])

        records = self._collect(
            make_request,
            config={"collectors": {SOURCE_ID: section}},
        )

        self.assertEqual(len(records), 15)
        self.assertEqual(
            make_request.call_args.kwargs["url"],
            f"{DEFAULT_BASE_URL}/SPY.json",
        )
        self.assertTrue(
            make_request.call_args.kwargs["headers"]["User-Agent"].startswith(
                "trading-data-platform"
            )
        )
        sleep.assert_not_called()

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_happy_path_parses_deterministic_point_in_time_rows(
        self, make_request, sleep, utc_now
    ):
        records = self._collect(make_request)

        self.assertEqual(len(records), 15)
        self.assertTrue(
            all(record["captured_at"] == PINNED_CAPTURED_AT for record in records)
        )
        self.assertTrue(
            all(record["source_timestamp"] == SOURCE_TIME_UTC for record in records)
        )

        record = next(
            r for r in records if r["contract_symbol"] == "SPY260821C00586000"
        )
        self.assertEqual(record["source"], SOURCE_ID)
        self.assertEqual(record["symbol"], "SPY")
        self.assertEqual(record["expiration"], date(2026, 8, 21))
        self.assertEqual(record["strike"], 586.0)
        self.assertEqual(record["option_type"], "call")
        self.assertEqual(record["bid"], 1.85)
        self.assertEqual(record["ask"], 1.95)
        self.assertEqual(record["last"], 1.90)
        self.assertEqual(record["volume"], 1500)
        self.assertEqual(record["open_interest"], 9100)
        self.assertEqual(record["implied_volatility"], 0.168)
        self.assertEqual(record["underlying_price"], 586.4)

        metadata = record["metadata"]
        self.assertTrue(metadata["delayed"])
        self.assertEqual(metadata["delay_minutes"], 15)
        self.assertEqual(metadata["source_timezone"], "America/Chicago")
        self.assertEqual(metadata["source_time_raw"], "2026-08-14 15:58:53.163568")
        self.assertEqual(metadata["acquisition_time"], PINNED_CAPTURED_AT.isoformat())
        self.assertEqual(metadata["contract_root"], "SPY")
        self.assertEqual(
            metadata["truncated"],
            {"symbols": False, "expiries": False, "contracts": False},
        )

        request = make_request.call_args
        self.assertEqual(request.kwargs["url"], f"{DEFAULT_BASE_URL}/SPY.json")
        self.assertEqual(
            request.kwargs["headers"]["User-Agent"].startswith("trading-data-platform"),
            True,
        )
        self.assertEqual(request.kwargs["correlation_id"], "cid")
        sleep.assert_not_called()

        self.assertEqual(self.last_collector.last_result_metadata["state"], "success")
        self.assertEqual(self.last_collector.last_result_metadata["contracts_kept"], 15)
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_rejected"], 0
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_identical_payload_yields_identical_records(
        self, make_request, sleep, utc_now
    ):
        first = self._collect(make_request)
        second = self._collect(make_request)
        self.assertEqual(first, second)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_feature_batch_is_insert_only_and_point_in_time(
        self, make_request, sleep, utc_now
    ):
        self._collect(make_request)
        result = self.last_result
        self.assertEqual(len(result.additional_writes), 1)
        batch = result.additional_writes[0]
        self.assertEqual(batch.table_name, FEATURE_TABLE)
        self.assertEqual(batch.conflict_columns, ["source", "symbol", "captured_at"])
        self.assertTrue(batch.insert_only)

        feature = batch.records[0]
        self.assertEqual(feature["source"], SOURCE_ID)
        self.assertEqual(feature["symbol"], "SPY")
        self.assertEqual(feature["captured_at"], PINNED_CAPTURED_AT)
        self.assertEqual(feature["feature_version"], FEATURE_VERSION)
        self.assertEqual(feature["source_timestamp_min"], SOURCE_TIME_UTC)
        self.assertEqual(feature["source_timestamp_max"], SOURCE_TIME_UTC)
        self.assertEqual(feature["available_at"], PINNED_CAPTURED_AT)
        self.assertEqual(feature["contract_count"], 15)
        self.assertEqual(feature["metadata"]["contracts_seen"], 15)
        self.assertEqual(feature["metadata"]["contracts_kept"], 15)
        self.assertEqual(feature["metadata"]["contracts_rejected"], 0)

        analytics = feature["analytics"]
        self.assertEqual(analytics["state"], "ok")
        self.assertEqual(analytics["term_structure_state"], "ok")
        self.assertEqual(analytics["unusualness"]["state"], "insufficient_history")
        self.assertEqual(analytics["totals"]["n_contracts"], 15)
        self.assertEqual(analytics["expiries"][0]["expiration"], "2026-08-21")
        self.assertAlmostEqual(analytics["expiries"][0]["atm"]["iv"], 0.171)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_feature_analytics_exclude_rejected_contracts(
        self, make_request, sleep, utc_now
    ):
        payload = _spy_chain_payload()
        payload["data"]["options"].append(
            _contract(
                "SPY",
                date(2026, 8, 21),
                "C",
                595,
                bid=3.0,
                ask=2.5,
                volume=999999,
                open_interest=999999,
            )
        )
        records = self._collect(make_request, payload=payload)
        self.assertEqual(len(records), 15)
        feature = self.last_result.additional_writes[0].records[0]
        self.assertEqual(feature["contract_count"], 15)
        # The impossible quote never reaches the analyzer: its fabricated
        # activity is absent from the analytics totals.
        self.assertEqual(feature["analytics"]["totals"]["n_contracts"], 15)
        self.assertEqual(feature["analytics"]["totals"]["volume"], 16750)
        self.assertEqual(feature["metadata"]["contracts_rejected"], 1)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_feature_reflects_truncation_bounds(self, make_request, sleep, utc_now):
        self._collect(make_request, config=self._config(max_expiries=1))
        feature = self.last_result.additional_writes[0].records[0]
        self.assertEqual(feature["contract_count"], 8)
        self.assertTrue(feature["metadata"]["truncated"]["expiries"])
        self.assertEqual(feature["metadata"]["contracts_seen"], 15)
        analytics = feature["analytics"]
        self.assertEqual(len(analytics["expiries"]), 1)
        self.assertEqual(analytics["term_structure_state"], "insufficient_history")

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_multiple_symbols_share_one_feature_batch(
        self, make_request, sleep, utc_now
    ):
        make_request.side_effect = [
            self._response(_spy_chain_payload()),
            self._response(_aapl_chain_payload()),
        ]
        result = CboeOptionsCollector().collect(
            self._config(symbols=["SPY", "AAPL"]), correlation_id="cid"
        )
        self.assertEqual(len(result.additional_writes), 1)
        rows = result.additional_writes[0].records
        self.assertEqual([row["symbol"] for row in rows], ["SPY", "AAPL"])
        self.assertEqual(rows[1]["contract_count"], 1)
        self.assertEqual(rows[1]["analytics"]["state"], "ok")

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_feature_rows_are_deterministic(self, make_request, sleep, utc_now):
        self._collect(make_request)
        first = self.last_result.additional_writes[0].records[0]
        self._collect(make_request)
        second = self.last_result.additional_writes[0].records[0]
        self.assertEqual(first, second)

    def test_identity_contract(self):
        collector = CboeOptionsCollector()
        self.assertEqual(collector.source_id, SOURCE_ID)
        self.assertEqual(collector.get_target_table(), "option_chain_snapshots")
        self.assertEqual(
            collector.get_conflict_columns(),
            ["source", "contract_symbol", "captured_at"],
        )
        self.assertEqual(collector.get_schedule(self._config()), "*/15 9-16 * * 1-5")

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_rejects_crossed_market_contract(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        payload["data"]["options"].append(
            _contract("SPY", date(2026, 8, 21), "C", 595, bid=3.0, ask=2.5)
        )
        records = self._collect(make_request, payload=payload)
        self.assertNotIn("SPY260821C00595000", [r["contract_symbol"] for r in records])
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_rejected"], 1
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_rejects_negative_and_nonfinite_quotes(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        exp = date(2026, 8, 21)
        payload["data"]["options"].extend(
            [
                _contract("SPY", exp, "C", 595, bid="-0.5"),
                _contract("SPY", exp, "C", 596, ask="NaN"),
                _contract("SPY", exp, "C", 597, last_trade_price="-1.0"),
                _contract("SPY", exp, "C", 598, volume=-10),
                _contract("SPY", exp, "C", 599, open_interest="-3"),
                _contract("SPY", exp, "P", 595, iv="-0.2"),
            ]
        )
        records = self._collect(make_request, payload=payload)
        self.assertEqual(len(records), 15)
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_rejected"], 6
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_rejects_malformed_contract_symbols(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        payload["data"]["options"].extend(
            [
                {"option": "SPY260821X00580000"},  # unknown side
                {"option": "SPY260230C00580000"},  # invalid month
                {"option": "SPY260821C005800"},  # 7-digit strike
                {"option": "SPY260821C00"},  # short
                {"option": "QQQ260821C00580000"},  # root mismatch
                {"option": 12345},  # not a string
            ]
        )
        records = self._collect(make_request, payload=payload)
        self.assertEqual(len(records), 15)
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_rejected"], 6
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_rejects_contracts_conflicting_with_symbol_decode(
        self, make_request, sleep, utc_now
    ):
        payload = _spy_chain_payload()
        payload["data"]["options"].extend(
            [
                _contract(
                    "SPY",
                    date(2026, 8, 21),
                    "C",
                    595,
                    expiration="2026-08-22",
                    bid=1.0,
                ),
                _contract(
                    "SPY",
                    date(2026, 8, 21),
                    "C",
                    596,
                    strike="580.5",
                    bid=1.0,
                ),
            ]
        )
        records = self._collect(make_request, payload=payload)
        self.assertEqual(len(records), 15)
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_rejected"], 2
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_duplicate_contract_symbols_rejected(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        payload["data"]["options"].append(
            _contract("SPY", date(2026, 8, 21), "C", 586, bid=9.9)
        )
        records = self._collect(make_request, payload=payload)
        self.assertEqual(len(records), 15)
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_rejected"], 1
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_all_contracts_rejected_fails_explicitly(
        self, make_request, sleep, utc_now
    ):
        payload = {"data": {"options": [{"option": "garbage"}, {"option": 1}]}}
        with self.assertRaises(InvalidSourceData):
            self._collect(make_request, payload=payload)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_empty_chain_is_valid_empty_output(self, make_request, sleep, utc_now):
        records = self._collect(make_request, payload={"data": {"options": []}})
        self.assertEqual(records, [])
        self.assertEqual(self.last_collector.last_result_metadata["state"], "success")
        self.assertEqual(self.last_result.additional_writes, [])

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_malformed_payloads_fail_explicitly(self, make_request, sleep, utc_now):
        for payload in (
            ["not", "an", "object"],
            {"no_data": {}},
            {"data": {"options": "not-a-list"}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidSourceData):
                    self._collect(make_request, payload=payload)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_unparseable_timestamp_fails_explicitly(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        payload["data"]["timestamp"] = "not-a-time"
        with self.assertRaises(InvalidSourceData):
            self._collect(make_request, payload=payload)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_negative_underlying_price_fails_explicitly(
        self, make_request, sleep, utc_now
    ):
        payload = _spy_chain_payload()
        payload["data"]["current_price"] = "-5.0"
        with self.assertRaises(InvalidSourceData):
            self._collect(make_request, payload=payload)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_unparseable_json_fails_explicitly(self, make_request, sleep, utc_now):
        make_request.return_value = httpx.Response(200, content=b"{not json")
        with self.assertRaises(InvalidSourceData):
            CboeOptionsCollector().collect(self._config(), correlation_id="cid")

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_byte_bound_via_content_length(self, make_request, sleep, utc_now):
        make_request.return_value = self._response(
            _spy_chain_payload(), headers={"content-length": "99999999"}
        )
        with self.assertRaises(InvalidSourceData):
            CboeOptionsCollector().collect(self._config(), correlation_id="cid")

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_byte_bound_is_enforced_via_streaming_cap(self, make_request, sleep, utc_now):
        # The body cap is enforced while the response streams inside
        # make_request; the collector must hand the bound over. A declared
        # Content-Length under the cap is accepted here because the
        # incremental byte count belongs to make_request.
        make_request.return_value = self._response(
            _spy_chain_payload(), headers={"content-length": "800"}
        )
        result = CboeOptionsCollector().collect(
            self._config(max_response_bytes=1024), correlation_id="cid"
        )
        self.assertEqual(len(result.records), 15)
        self.assertEqual(make_request.call_args.kwargs["max_response_bytes"], 1024)
        self.assertEqual(
            make_request.call_args.kwargs["deadline_seconds"],
            60.0,
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_streaming_oversize_is_reported_as_invalid_source_data(
        self, make_request, sleep, utc_now
    ):
        request = httpx.Request(
            "GET", f"{DEFAULT_BASE_URL.rstrip('/')}/SPY.json"
        )
        make_request.side_effect = http_client.ResponseBodyTooLarge(
            "response exceeds 1024 bytes", request=request
        )
        with self.assertRaises(InvalidSourceData) as raised:
            self._collect(make_request, config=self._config(max_response_bytes=1024))
        self.assertIn("byte bound", str(raised.exception))

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_contract_bound_truncates_deterministically(
        self, make_request, sleep, utc_now
    ):
        records = self._collect(
            make_request, config=self._config(max_contracts_per_symbol=5)
        )
        self.assertEqual(
            [r["contract_symbol"] for r in records],
            [
                "SPY260821C00550000",
                "SPY260821P00555500",
                "SPY260821C00580000",
                "SPY260821C00585000",
                "SPY260821C00586000",
            ],
        )
        self.assertTrue(records[0]["metadata"]["truncated"]["contracts"])
        self.assertEqual(
            self.last_collector.last_result_metadata["contracts_truncated"], True
        )
        self.assertEqual(self.last_collector.last_result_metadata["contracts_kept"], 5)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_expiry_bound_keeps_nearest_expirations(self, make_request, sleep, utc_now):
        records = self._collect(make_request, config=self._config(max_expiries=1))
        self.assertEqual(len(records), 8)
        self.assertTrue(all(r["expiration"] == date(2026, 8, 21) for r in records))
        self.assertTrue(records[0]["metadata"]["truncated"]["expiries"])
        self.assertEqual(
            self.last_collector.last_result_metadata["expiries_truncated"], True
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_symbol_cap_and_rate_delay(self, make_request, sleep, utc_now):
        make_request.side_effect = [
            self._response(_spy_chain_payload()),
            self._response(_aapl_chain_payload()),
        ]
        collector = CboeOptionsCollector()
        result = collector.collect(
            self._config(
                symbols=["SPY", "AAPL", "MSFT"], max_symbols=2, rate_delay_seconds=0.25
            ),
            correlation_id="cid",
        )
        records = result.records
        self.assertEqual(len(records), 16)
        self.assertEqual(make_request.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertEqual(collector.last_result_metadata["symbols_truncated"], True)
        self.assertTrue(records[0]["metadata"]["truncated"]["symbols"])

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_partial_symbol_failure_returns_successful_symbols(
        self, make_request, sleep, utc_now
    ):
        make_request.side_effect = [
            self._response(_spy_chain_payload()),
            httpx.ConnectError("boom"),
        ]
        collector = CboeOptionsCollector()
        result = collector.collect(
            self._config(symbols=["SPY", "AAPL"]), correlation_id="cid"
        )
        records = result.records
        self.assertEqual(len(records), 15)
        self.assertEqual(collector.last_result_metadata["state"], "partial_failure")
        self.assertEqual(collector.last_result_metadata["symbols_failed"], 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["symbol"], "AAPL")
        self.assertEqual(result.errors[0]["code"], "request_failed")
        self.assertEqual(
            result.errors[0]["error_class"], TransientSourceError.error_class
        )
        self.assertTrue(result.partial_failure)
        self.assertEqual(result.total_series, 2)
        self.assertEqual(result.successful_series, 1)
        self.assertEqual(result.metrics, {"api_calls_made": 2})
        # Features exist only for the successfully fetched symbol.
        self.assertEqual(len(result.additional_writes), 1)
        feature_rows = result.additional_writes[0].records
        self.assertEqual([row["symbol"] for row in feature_rows], ["SPY"])

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_provider_rejection_isolated_to_one_symbol(
        self, make_request, sleep, utc_now
    ):
        make_request.side_effect = [
            self._response(_spy_chain_payload()),
            InvalidSourceData("provider returned HTTP 403"),
        ]
        result = CboeOptionsCollector().collect(
            self._config(symbols=["SPY", "BLND.L"]), correlation_id="cid"
        )

        self.assertEqual(len(result.records), 15)
        self.assertTrue(result.partial_failure)
        self.assertEqual(result.successful_series, 1)
        self.assertEqual(result.errors[0]["symbol"], "BLND.L")
        self.assertEqual(result.errors[0]["stage"], "chain_parse")
        self.assertEqual(result.errors[0]["code"], "invalid_source_data")
        self.assertEqual(result.errors[0]["error_class"], InvalidSourceData.error_class)
        self.assertEqual(
            [row["symbol"] for row in result.additional_writes[0].records], ["SPY"]
        )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_all_symbols_failing_raises(self, make_request, sleep, utc_now):
        make_request.side_effect = [
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
        ]
        with self.assertRaises(httpx.ConnectError):
            CboeOptionsCollector().collect(
                self._config(symbols=["SPY", "AAPL"]), correlation_id="cid"
            )

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_missing_source_timestamp_is_preserved(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        del payload["data"]["timestamp"]
        del payload["timestamp"]
        records = self._collect(make_request, payload=payload)
        self.assertTrue(all(r["source_timestamp"] is None for r in records))
        self.assertEqual(records[0]["metadata"]["source_time_raw"], None)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_underlying_price_fallbacks(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        del payload["data"]["current_price"]
        payload["data"]["quote"] = {"symbol": "SPY", "last_price": "586.10"}
        records = self._collect(make_request, payload=payload)
        self.assertEqual(records[0]["underlying_price"], 586.1)

        payload = _spy_chain_payload()
        del payload["data"]["current_price"]
        payload["data"]["underlying"] = {"symbol": "SPY", "last_price": "586.20"}
        records = self._collect(make_request, payload=payload)
        self.assertEqual(records[0]["underlying_price"], 586.2)

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_missing_underlying_price_preserved_as_null(
        self, make_request, sleep, utc_now
    ):
        payload = _spy_chain_payload()
        del payload["data"]["current_price"]
        records = self._collect(make_request, payload=payload)
        self.assertTrue(all(r["underlying_price"] is None for r in records))

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_missing_quote_fields_preserved(self, make_request, sleep, utc_now):
        payload = _spy_chain_payload()
        payload["data"]["options"][4] = _contract(
            "SPY", date(2026, 8, 21), "C", 586, iv="0.168"
        )  # no bid/ask/last/volume/OI
        records = self._collect(make_request, payload=payload)
        record = next(
            r for r in records if r["contract_symbol"] == "SPY260821C00586000"
        )
        self.assertIsNone(record["bid"])
        self.assertIsNone(record["ask"])
        self.assertIsNone(record["last"])
        self.assertIsNone(record["volume"])
        self.assertIsNone(record["open_interest"])
        self.assertEqual(record["implied_volatility"], 0.168)

    @patch("collectors.cboe_options.make_request")
    def test_health_check(self, make_request):
        collector = CboeOptionsCollector()
        make_request.return_value = httpx.Response(200, json=_spy_chain_payload())
        result = collector.health_check(self._config())
        self.assertTrue(result["healthy"])
        self.assertIn("latency_ms", result)

        make_request.return_value = httpx.Response(503, text="unavailable")
        result = collector.health_check(self._config())
        self.assertFalse(result["healthy"])
        self.assertIn("503", result["message"])

        make_request.side_effect = httpx.ConnectError("refused")
        result = collector.health_check(self._config())
        self.assertFalse(result["healthy"])

    @patch("collectors.cboe_options.make_request")
    def test_custom_origin_must_be_https(self, make_request):
        with self.assertRaises(ValueError):
            CboeOptionsCollector().collect(
                self._config(
                    base_url="http://cdn.cboe.com/api/global/delayed_quotes/options"
                ),
                correlation_id="cid",
            )
        make_request.assert_not_called()

    @patch("collectors.cboe_options.make_request")
    def test_missing_symbols_config_fails(self, make_request):
        with self.assertRaises(ValueError):
            CboeOptionsCollector().collect(
                self._config(symbols=[]), correlation_id="cid"
            )

    def test_invalid_bound_config_fails(self):
        for override in (
            {"max_contracts_per_symbol": 0},
            {"max_expiries": "many"},
            {"rate_delay_seconds": -1},
            {"delay_minutes": -1},
            {"source_timezone": "Mars/Olympus"},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    CboeOptionsCollector().collect(
                        self._config(**override), correlation_id="cid"
                    )


class OptionsAnalyticsTests(unittest.TestCase):
    pinned = PINNED_CAPTURED_AT

    def _config(self, **overrides):
        return {
            "collectors": {
                SOURCE_ID: {
                    "schedule": "* * * * *",
                    "symbols": ["SPY"],
                    **overrides,
                }
            }
        }

    def _row(
        self,
        *,
        contract_symbol,
        expiration,
        strike,
        option_type,
        bid=None,
        ask=None,
        last=None,
        volume=None,
        open_interest=None,
        implied_volatility=None,
        underlying_price=586.4,
        captured_at=None,
        symbol="SPY",
        source_timestamp=None,
    ):
        return {
            "source": SOURCE_ID,
            "symbol": symbol,
            "contract_symbol": contract_symbol,
            "captured_at": captured_at or self.pinned,
            "source_timestamp": source_timestamp,
            "expiration": expiration,
            "strike": strike,
            "option_type": option_type,
            "bid": bid,
            "ask": ask,
            "last": last,
            "volume": volume,
            "open_interest": open_interest,
            "implied_volatility": implied_volatility,
            "underlying_price": underlying_price,
            "metadata": {},
        }

    @staticmethod
    def _occ(symbol, expiration, kind, strike):
        code = f"{int(round(strike * 1000)):08d}"
        return f"{symbol}{expiration.strftime('%y%m%d')}{kind}{code}"

    def _current_spy_rows(self, underlying_price=586.4):
        exp = date(2026, 8, 21)
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 580),
                expiration=exp,
                strike=580.0,
                option_type="call",
                bid=5.20,
                ask=5.30,
                last=5.25,
                volume=1200,
                open_interest=8500,
                implied_volatility=0.181,
                underlying_price=underlying_price,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 586),
                expiration=exp,
                strike=586.0,
                option_type="call",
                bid=1.85,
                ask=1.95,
                last=1.90,
                volume=1500,
                open_interest=9100,
                implied_volatility=0.168,
                underlying_price=underlying_price,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "P", 586),
                expiration=exp,
                strike=586.0,
                option_type="put",
                bid=1.75,
                ask=1.85,
                last=1.80,
                volume=1100,
                open_interest=8200,
                implied_volatility=0.174,
                underlying_price=underlying_price,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "P", 555.5),
                expiration=exp,
                strike=555.5,
                option_type="put",
                bid=0.55,
                ask=0.60,
                last=0.57,
                volume=250,
                open_interest=3300,
                implied_volatility=0.184,
                underlying_price=underlying_price,
            ),
        ]
        exp2 = date(2026, 9, 18)
        rows.extend(
            [
                self._row(
                    contract_symbol=self._occ("SPY", exp2, "C", 585),
                    expiration=exp2,
                    strike=585.0,
                    option_type="call",
                    bid=18.50,
                    ask=18.90,
                    last=18.70,
                    volume=2200,
                    open_interest=15500,
                    implied_volatility=0.195,
                    underlying_price=underlying_price,
                ),
                self._row(
                    contract_symbol=self._occ("SPY", exp2, "P", 585),
                    expiration=exp2,
                    strike=585.0,
                    option_type="put",
                    bid=17.90,
                    ask=18.30,
                    last=18.10,
                    volume=2100,
                    open_interest=14900,
                    implied_volatility=0.199,
                    underlying_price=underlying_price,
                ),
            ]
        )
        return rows

    @patch("collectors.cboe_options._utc_now", return_value=PINNED_CAPTURED_AT)
    @patch("collectors.cboe_options._sleep")
    @patch("collectors.cboe_options.make_request")
    def test_analytics_over_collector_fixture(self, make_request, sleep, utc_now):
        make_request.return_value = httpx.Response(200, json=_spy_chain_payload())
        result = CboeOptionsCollector().collect(self._config(), correlation_id="cid")
        records = result.records
        result = analyze_chain(records)

        self.assertEqual(result["state"], STATE_OK)
        symbol = result["symbols"]["SPY"]
        self.assertEqual(symbol["underlying_price"], 586.4)
        self.assertEqual(symbol["captured_at"], PINNED_CAPTURED_AT.isoformat())
        self.assertEqual(symbol["source_timestamp"], SOURCE_TIME_UTC.isoformat())

        expiries = {e["expiration"]: e for e in symbol["expiries"]}
        near = expiries["2026-08-21"]
        self.assertEqual(near["dte"], 7)
        self.assertFalse(near["expired"])
        self.assertEqual(near["state"], STATE_OK)
        self.assertEqual(near["atm"]["strike"], 586.0)
        self.assertAlmostEqual(near["atm"]["iv"], 0.171, places=6)
        self.assertEqual(near["atm"]["sources"], ["call", "put"])
        self.assertEqual(near["straddle_price"], 3.70)
        self.assertAlmostEqual(
            near["implied_move_pct"], (1.90 + 1.80) / 586.4 * 100.0, places=6
        )
        self.assertAlmostEqual(
            near["iv_move_pct"], 0.171 * (7 / 365) ** 0.5 * 100.0, places=6
        )
        self.assertEqual(near["put_call_skew"]["otm_strike"], 555.5)
        self.assertAlmostEqual(near["put_call_skew"]["value"], 0.013, places=6)
        self.assertEqual(near["volume"], 6450)
        self.assertEqual(near["open_interest"], 49600)
        self.assertTrue(near["volume_complete"])

        mid = expiries["2026-09-18"]
        self.assertEqual(mid["atm"]["strike"], 585.0)
        self.assertAlmostEqual(mid["atm"]["iv"], 0.197, places=6)

        far = expiries["2026-12-18"]
        self.assertEqual(far["atm"]["strike"], 580.0)
        self.assertAlmostEqual(far["atm"]["iv"], 0.2145, places=6)

        self.assertEqual(symbol["term_structure_state"], STATE_OK)
        self.assertEqual(len(symbol["term_structure"]), 3)
        self.assertEqual(
            [p["expiration"] for p in symbol["term_structure"]],
            ["2026-08-21", "2026-09-18", "2026-12-18"],
        )

        totals = symbol["totals"]
        self.assertEqual(totals["volume"], 16750)
        self.assertEqual(totals["open_interest"], 162300)
        self.assertEqual(totals["n_contracts"], 15)
        self.assertEqual(totals["n_calls"], 9)
        self.assertEqual(totals["n_puts"], 6)
        self.assertTrue(totals["volume_complete"])
        self.assertTrue(totals["oi_complete"])

        unusualness = symbol["unusualness"]
        self.assertEqual(unusualness["state"], STATE_INSUFFICIENT_HISTORY)
        self.assertEqual(unusualness["available_history_snapshots"], 0)
        self.assertIsNone(unusualness["unusual_volume"])

    def test_analytics_underlying_price_missing(self):
        rows = self._current_spy_rows(underlying_price=None)
        result = analyze_chain(rows)
        symbol = result["symbols"]["SPY"]
        self.assertEqual(symbol["state"], "insufficient_data")
        self.assertIsNone(symbol["underlying_price"])
        entry = symbol["expiries"][0]
        self.assertEqual(entry["state"], "insufficient_data")
        self.assertEqual(entry["reason"], "underlying_price_missing")
        self.assertIsNone(entry["atm"])
        self.assertIsNone(entry["implied_move_pct"])
        # Totals do not need the underlying price and must still be reported.
        self.assertGreater(entry["volume"], 0)
        self.assertEqual(entry["volume_complete"], True)

    def test_analytics_missing_iv_never_backfilled(self):
        exp = date(2026, 8, 21)
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 586),
                expiration=exp,
                strike=586.0,
                option_type="call",
                bid=1.85,
                ask=1.95,
                implied_volatility=None,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "P", 586),
                expiration=exp,
                strike=586.0,
                option_type="put",
                bid=1.75,
                ask=1.85,
                implied_volatility=None,
            ),
        ]
        result = analyze_chain(rows)
        entry = result["symbols"]["SPY"]["expiries"][0]
        self.assertEqual(entry["state"], "insufficient_data")
        self.assertEqual(entry["reason"], "atm_iv_missing")
        self.assertEqual(entry["atm"]["state"], "insufficient_data")
        self.assertIsNone(entry["atm"]["iv"])
        self.assertIsNone(entry["iv_move_pct"])

    def test_analytics_straddle_unavailable(self):
        exp = date(2026, 8, 21)
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 586),
                expiration=exp,
                strike=586.0,
                option_type="call",
                bid=1.85,
                ask=1.95,
                implied_volatility=0.168,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "P", 586),
                expiration=exp,
                strike=586.0,
                option_type="put",
                implied_volatility=0.174,
            ),
        ]
        result = analyze_chain(rows)
        entry = result["symbols"]["SPY"]["expiries"][0]
        self.assertEqual(entry["state"], STATE_OK)
        self.assertIsNone(entry["straddle_price"])
        self.assertIsNone(entry["implied_move_pct"])
        self.assertEqual(entry["implied_move_state"], "insufficient_data")
        self.assertEqual(entry["implied_move_reason"], "straddle_unavailable")
        self.assertIsNotNone(entry["iv_move_pct"])

    def test_analytics_expired_expiry_excluded(self):
        exp = date(2026, 8, 7)  # 7 days before the snapshot
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 580),
                expiration=exp,
                strike=580.0,
                option_type="call",
                bid=1.0,
                ask=1.1,
                implied_volatility=0.15,
            ),
            self._row(
                contract_symbol=self._occ("SPY", date(2026, 8, 21), "C", 586),
                expiration=date(2026, 8, 21),
                strike=586.0,
                option_type="call",
                bid=1.85,
                ask=1.95,
                implied_volatility=0.168,
            ),
            self._row(
                contract_symbol=self._occ("SPY", date(2026, 8, 21), "P", 586),
                expiration=date(2026, 8, 21),
                strike=586.0,
                option_type="put",
                bid=1.75,
                ask=1.85,
                implied_volatility=0.174,
            ),
        ]
        result = analyze_chain(rows)
        symbol = result["symbols"]["SPY"]
        expired_entry = next(
            e for e in symbol["expiries"] if e["expiration"] == "2026-08-07"
        )
        self.assertTrue(expired_entry["expired"])
        self.assertEqual(expired_entry["state"], "insufficient_data")
        self.assertEqual(expired_entry["reason"], "expired")
        self.assertIsNone(expired_entry["atm"])
        # One tradable expiry left: no term structure claim.
        self.assertEqual(symbol["term_structure_state"], STATE_INSUFFICIENT_HISTORY)

    def test_analytics_term_structure_needs_two_expiries(self):
        exp = date(2026, 8, 21)
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 586),
                expiration=exp,
                strike=586.0,
                option_type="call",
                bid=1.85,
                ask=1.95,
                implied_volatility=0.168,
            )
        ]
        result = analyze_chain(rows)
        symbol = result["symbols"]["SPY"]
        self.assertEqual(symbol["term_structure_state"], STATE_INSUFFICIENT_HISTORY)
        self.assertEqual(
            symbol["term_structure_reason"], "need_at_least_two_expiries_with_atm_iv"
        )

    def test_analytics_unusualness_requires_local_history(self):
        rows = self._current_spy_rows()
        result = analyze_chain(rows)
        unusualness = result["symbols"]["SPY"]["unusualness"]
        self.assertEqual(unusualness["state"], STATE_INSUFFICIENT_HISTORY)
        self.assertEqual(unusualness["available_history_snapshots"], 0)
        self.assertIsNone(unusualness["volume_percentile"])
        self.assertIsNone(unusualness["unusual_volume"])

    def _history_rows(self, count, volumes, ois, base=None):
        base = base or self.pinned
        rows = []
        for i in range(count):
            captured_at = base - timedelta(days=1 + i)
            rows.append(
                self._row(
                    contract_symbol=self._occ("SPY", date(2026, 8, 21), "C", 580),
                    expiration=date(2026, 8, 21),
                    strike=580.0,
                    option_type="call",
                    volume=volumes[i],
                    open_interest=ois[i],
                    captured_at=captured_at,
                )
            )
        return rows

    def test_analytics_unusualness_with_sufficient_history(self):
        rows = self._current_spy_rows()
        history = self._history_rows(
            6,
            [1000, 2000, 3000, 4000, 5000, 6000],
            [1000, 2000, 3000, 4000, 5000, 6000],
        )
        result = analyze_chain(rows, history=history)
        unusualness = result["symbols"]["SPY"]["unusualness"]
        self.assertEqual(unusualness["state"], STATE_OK)
        self.assertEqual(unusualness["available_history_snapshots"], 6)
        self.assertEqual(unusualness["volume_percentile"], 1.0)
        self.assertEqual(unusualness["open_interest_percentile"], 1.0)
        self.assertTrue(unusualness["unusual_volume"])
        self.assertTrue(unusualness["unusual_open_interest"])
        self.assertTrue(unusualness["local_history_only"])

    def test_analytics_unusualness_mid_rank_not_unusual(self):
        exp = date(2026, 8, 21)
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 580),
                expiration=exp,
                strike=580.0,
                option_type="call",
                volume=1500,
                open_interest=2500,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 586),
                expiration=exp,
                strike=586.0,
                option_type="call",
                volume=2000,
                open_interest=2500,
            ),
        ]
        history = self._history_rows(
            6,
            [1000, 2000, 3000, 4000, 5000, 6000],
            [1000, 2000, 3000, 4000, 5000, 6000],
        )
        unusualness = analyze_chain(rows, history=history)["symbols"]["SPY"][
            "unusualness"
        ]
        self.assertEqual(unusualness["state"], STATE_OK)
        self.assertEqual(unusualness["volume_percentile"], 0.5)
        self.assertAlmostEqual(unusualness["open_interest_percentile"], 5 / 6)
        self.assertFalse(unusualness["unusual_volume"])
        self.assertFalse(unusualness["unusual_open_interest"])

    def test_analytics_insufficient_history_below_minimum(self):
        rows = self._current_spy_rows()
        history = self._history_rows(
            4, [1000, 2000, 3000, 4000], [1000, 2000, 3000, 4000]
        )
        unusualness = analyze_chain(rows, history=history)["symbols"]["SPY"][
            "unusualness"
        ]
        self.assertEqual(unusualness["state"], STATE_INSUFFICIENT_HISTORY)
        self.assertEqual(unusualness["available_history_snapshots"], 4)
        self.assertEqual(unusualness["reason"], "need_at_least_5_prior_snapshots")
        self.assertIsNone(unusualness["unusual_volume"])

    def test_analytics_prior_groups_in_input_count_as_history(self):
        rows = self._current_spy_rows()
        rows.extend(self._history_rows(2, [100, 200], [100, 200]))
        unusualness = analyze_chain(rows)["symbols"]["SPY"]["unusualness"]
        self.assertEqual(unusualness["state"], STATE_INSUFFICIENT_HISTORY)
        self.assertEqual(unusualness["available_history_snapshots"], 2)

    def test_analytics_history_dedupes_same_snapshot(self):
        rows = self._current_spy_rows()
        history = [
            self._row(
                contract_symbol=self._occ("SPY", date(2026, 8, 21), "C", 580),
                expiration=date(2026, 8, 21),
                strike=580.0,
                option_type="call",
                volume=999999,
                open_interest=999999,
                captured_at=self.pinned,
            )
        ]
        result = analyze_chain(rows, history=history)
        symbol = result["symbols"]["SPY"]
        self.assertEqual(symbol["totals"]["n_contracts"], 6)
        self.assertEqual(symbol["totals"]["volume"], 8350)
        self.assertEqual(symbol["totals"]["open_interest"], 59500)

    def test_analytics_partial_totals_are_labeled(self):
        exp = date(2026, 8, 21)
        rows = [
            self._row(
                contract_symbol=self._occ("SPY", exp, "C", 586),
                expiration=exp,
                strike=586.0,
                option_type="call",
                bid=1.85,
                ask=1.95,
                implied_volatility=0.168,
                volume=1500,
                open_interest=9100,
            ),
            self._row(
                contract_symbol=self._occ("SPY", exp, "P", 586),
                expiration=exp,
                strike=586.0,
                option_type="put",
                bid=1.75,
                ask=1.85,
                implied_volatility=0.174,
                open_interest=8200,
            ),
        ]
        entry = analyze_chain(rows)["symbols"]["SPY"]["expiries"][0]
        self.assertEqual(entry["volume"], 1500)
        self.assertFalse(entry["volume_complete"])
        self.assertEqual(entry["open_interest"], 17300)
        self.assertTrue(entry["oi_complete"])

    def test_analytics_no_data(self):
        result = analyze_chain([])
        self.assertEqual(result["state"], STATE_NO_DATA)
        self.assertEqual(result["symbols"], {})

    def test_analytics_multiple_symbols(self):
        exp = date(2026, 8, 21)
        rows = self._current_spy_rows()
        rows.append(
            self._row(
                contract_symbol=self._occ("AAPL", exp, "C", 200),
                expiration=exp,
                strike=200.0,
                option_type="call",
                bid=4.0,
                ask=4.2,
                implied_volatility=0.22,
                underlying_price=200.0,
                symbol="AAPL",
            )
        )
        result = analyze_chain(rows)
        self.assertEqual(set(result["symbols"]), {"SPY", "AAPL"})
        self.assertEqual(result["symbols"]["AAPL"]["underlying_price"], 200.0)
        self.assertEqual(
            result["symbols"]["AAPL"]["expiries"][0]["atm"]["strike"], 200.0
        )

    def test_analytics_malformed_rows_fail_explicitly(self):
        good = self._current_spy_rows()[0]
        bad_rows = [
            {**good, "option_type": "C"},
            {**good, "strike": -5.0},
            {**good, "strike": "abc"},
            {**good, "implied_volatility": "abc"},
            {**good, "volume": 1.5},
            {**good, "expiration": "2026-13-45"},
            {**good, "captured_at": None},
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    analyze_chain([row])

    def test_analytics_deterministic(self):
        rows = self._current_spy_rows()
        self.assertEqual(analyze_chain(rows), analyze_chain(rows))

    def test_analytics_no_dealer_gamma_inference(self):
        rows = self._current_spy_rows()
        result = analyze_chain(rows)
        serialized = json.dumps(result).lower()
        self.assertNotIn("gamma", serialized)
        self.assertNotIn("dealer", serialized)
        self.assertNotIn("gex", serialized)


if __name__ == "__main__":
    unittest.main()
