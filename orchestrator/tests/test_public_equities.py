import sys
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.public_equities import (
    DEFAULT_CHART_URL,
    DEFAULT_USER_AGENT,
    MAX_RESPONSE_BYTES,
    PublicEquitiesCollector,
    corporate_action_id,
)

import db
from contracts.runtime_config import CollectorConfig

REGULAR_MARKET_TIME = 1715212800  # 2024-05-09 00:00:00 UTC
BAR_TIMESTAMPS = [1715212800, 1715299200]  # 2024-05-09, 2024-05-10 00:00:00 UTC
FIXED_NOW = datetime(2024, 5, 13, tzinfo=UTC)
DIVIDEND_EPOCH = 1715126400  # 2024-05-08 00:00:00 UTC
SPLIT_EPOCH = 1714953600  # 2024-05-06 00:00:00 UTC


def _config(symbols=None, **overrides):
    section = {
        "schedule": "0 7 * * 1-5",
        "symbols": ["AAPL"] if symbols is None else symbols,
        "range": "1y",
        "interval": "1d",
    }
    section.update(overrides)
    return {"database": {}, "collectors": {"public_equities": section}}


def _response(payload, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _chart_payload(
    timestamps=BAR_TIMESTAMPS,
    quote=None,
    meta=None,
    events=None,
    error=None,
    result=None,
):
    if result is None:
        result = [
            {
                "timestamp": timestamps,
                "indicators": {
                    "quote": [
                        quote
                        or {
                            "open": [173.5, 174.0],
                            "high": [175.0, 175.5],
                            "low": [172.5, 173.0],
                            "close": [174.5, 174.8],
                            "volume": [52000000, 48000000],
                        }
                    ]
                },
                "meta": meta
                or {
                    "symbol": "AAPL",
                    "currency": "USD",
                    "exchangeName": "NMS",
                    "regularMarketTime": REGULAR_MARKET_TIME,
                },
                "events": events,
            }
        ]
    return {"chart": {"result": result, "error": error}}


class RuntimeConfigMappingTests(unittest.TestCase):
    @patch("collectors.public_equities.make_request")
    def test_collect_accepts_validated_runtime_config_mapping(self, make_request):
        make_request.return_value = _response(_chart_payload())
        section = CollectorConfig(
            symbols=["AAPL"],
            range="1y",
            interval="1d",
        )

        result = PublicEquitiesCollector().collect(
            {"collectors": {"public_equities": section}},
            "typed-config",
            now=FIXED_NOW,
        )

        self.assertEqual(result.successful_series, 1)
        self.assertEqual(len(result.records), 2)

    @patch("collectors.public_equities.make_request")
    def test_bounded_concurrency_preserves_configured_symbol_order(self, make_request):
        def response(**kwargs):
            symbol = kwargs["url"].rsplit("/", 1)[-1]
            return _response(
                _chart_payload(
                    timestamps=[BAR_TIMESTAMPS[0]],
                    quote={
                        "open": [10.0],
                        "high": [11.0],
                        "low": [9.0],
                        "close": [10.5],
                        "volume": [1000],
                    },
                    meta={
                        "symbol": symbol,
                        "currency": "USD",
                        "exchangeName": "NMS",
                        "regularMarketTime": REGULAR_MARKET_TIME,
                    },
                )
            )

        make_request.side_effect = response
        result = PublicEquitiesCollector().collect(
            _config(
                symbols=["AAPL", "MSFT", "NVDA"],
                max_symbols=400,
                max_concurrency=3,
            ),
            "concurrent",
            now=FIXED_NOW,
        )

        self.assertEqual(
            [row["symbol"] for row in result.records], ["AAPL", "MSFT", "NVDA"]
        )
        self.assertEqual(result.successful_series, 3)
        self.assertEqual(result.metrics["api_calls_made"], 3)


class PublicEquitiesParsingTests(unittest.TestCase):
    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_daily_bars_are_deterministic_and_unadjusted(
        self, make_request, get_session
    ):
        make_request.return_value = _response(_chart_payload())

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.total_series, 1)
        self.assertEqual(result.successful_series, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.metrics["bars_fetched"], 2)
        self.assertEqual(len(result.records), 2)
        first = result.records[0]
        self.assertEqual(
            first,
            {
                "symbol": "AAPL",
                "timeframe": "1d",
                "timestamp": datetime(2024, 5, 9, tzinfo=UTC),
                "open": 173.5,
                "high": 175.0,
                "low": 172.5,
                "close": 174.5,
                "volume": 52000000.0,
                "source": "public_equities",
                "metadata": {
                    "adjusted": False,
                    "interval": "1d",
                    "range": "1y",
                    "provider_symbol": "AAPL",
                    "source_reference": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
                    "currency": "USD",
                    "exchange_name": "NMS",
                    "source_timestamp": "2024-05-09T00:00:00+00:00",
                    "available_at": "2024-05-13T00:00:00+00:00",
                },
            },
        )
        # Bars are stored verbatim: the second bar's values are untouched.
        self.assertEqual(result.records[1]["close"], 174.8)
        self.assertEqual(
            result.records[1]["timestamp"], datetime(2024, 5, 10, tzinfo=UTC)
        )
        # Prices never persist corporate actions when no events exist.
        get_session.assert_not_called()

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_split_and_dividend_are_distinct_action_rows(
        self, make_request, get_session
    ):
        payload = _chart_payload(
            events={
                "dividends": {str(DIVIDEND_EPOCH): {"amount": 0.24}},
                "splits": {
                    str(SPLIT_EPOCH): {
                        "splitRatio": "2:1",
                        "numerator": 2,
                        "denominator": 1,
                    }
                },
            }
        )
        make_request.return_value = _response(payload)

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.metrics["corporate_actions_fetched"], 2)
        # collect() never opens its own transaction: bars and actions are
        # handed to the executor as one insert-only additional batch.
        get_session.assert_not_called()
        self.assertEqual(len(result.additional_writes), 1)
        actions_batch = result.additional_writes[0]
        self.assertEqual(actions_batch.table_name, "corporate_actions")
        self.assertEqual(actions_batch.conflict_columns, ["action_id"])
        self.assertTrue(actions_batch.insert_only)
        self.assertEqual(len(actions_batch.records), 2)

        dividend, split = actions_batch.records
        self.assertEqual(dividend["action_type"], "dividend")
        self.assertEqual(dividend["symbol"], "AAPL")
        self.assertEqual(dividend["effective_date"], date(2024, 5, 8))
        self.assertEqual(dividend["amount"], 0.24)
        self.assertIsNone(dividend["ratio_numerator"])
        self.assertEqual(dividend["source_timestamp"], datetime(2024, 5, 8, tzinfo=UTC))
        self.assertEqual(dividend["available_at"], FIXED_NOW)
        self.assertNotEqual(dividend["source_timestamp"], dividend["available_at"])
        self.assertEqual(
            dividend["action_id"],
            corporate_action_id(
                "public_equities",
                "AAPL",
                "dividend",
                date(2024, 5, 8),
                amount=0.24,
            ),
        )

        self.assertEqual(split["action_type"], "split")
        self.assertEqual(split["effective_date"], date(2024, 5, 6))
        self.assertIsNone(split["amount"])
        self.assertEqual(split["ratio_numerator"], 2.0)
        self.assertEqual(split["ratio_denominator"], 1.0)
        self.assertEqual(split["description"], "2:1")
        self.assertEqual(
            split["action_id"],
            corporate_action_id(
                "public_equities",
                "AAPL",
                "split",
                date(2024, 5, 6),
                numerator=2.0,
                denominator=1.0,
            ),
        )

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_recollection_is_idempotent_for_identical_actions(
        self, make_request, get_session
    ):
        payload = _chart_payload(
            events={"dividends": {str(DIVIDEND_EPOCH): {"amount": 0.24}}}
        )
        make_request.return_value = _response(payload)

        collector = PublicEquitiesCollector()
        first = collector.collect(_config(), "cid", now=FIXED_NOW)
        second = collector.collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(first.metrics["corporate_actions_fetched"], 1)
        self.assertEqual(second.metrics["corporate_actions_fetched"], 1)
        get_session.assert_not_called()
        # Both runs derive the identical deterministic action_id: the second
        # insert-only batch is a no-op at the database (ON CONFLICT DO NOTHING
        # on action_id) instead of an in-place update.
        self.assertEqual(
            first.additional_writes[0].records[0]["action_id"],
            second.additional_writes[0].records[0]["action_id"],
        )

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_missing_bars_and_missing_actions_stay_missing(
        self, make_request, get_session
    ):
        payload = _chart_payload(
            quote={
                "open": [173.5, None],
                "high": [175.0, None],
                "low": [172.5, None],
                "close": [174.5, None],
                "volume": [52000000, None],
            },
            events=None,
        )
        make_request.return_value = _response(payload)

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.records), 1)
        self.assertEqual(
            result.records[0]["timestamp"], datetime(2024, 5, 9, tzinfo=UTC)
        )
        self.assertEqual(result.metrics["corporate_actions_fetched"], 0)
        self.assertEqual(result.additional_writes, [])
        get_session.assert_not_called()

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_empty_provider_result_is_valid_empty_output(
        self, make_request, get_session
    ):
        make_request.return_value = _response(_chart_payload(result=[]))

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.records, [])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.successful_series, 1)
        get_session.assert_not_called()

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_empty_symbol_list_is_valid_empty_output(self, make_request, get_session):
        result = PublicEquitiesCollector().collect(
            _config(symbols=[]), "cid", now=FIXED_NOW
        )

        self.assertEqual(result.records, [])
        self.assertEqual(result.total_series, 0)
        self.assertEqual(result.successful_series, 0)
        self.assertEqual(result.metrics["api_calls_made"], 0)
        make_request.assert_not_called()
        get_session.assert_not_called()

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_malformed_quote_array_rejects_symbol(self, make_request, get_session):
        payload = _chart_payload(
            quote={
                "open": [173.5, 174.0],
                "high": [175.0, 175.5],
                "low": [172.5, 173.0],
                "close": [174.5],
                "volume": [52000000, 48000000],
            }
        )
        make_request.return_value = _response(payload)

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.records, [])
        self.assertEqual(result.successful_series, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["symbol"], "AAPL")
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")
        get_session.assert_not_called()

    @patch("collectors.public_equities.make_request")
    def test_non_numeric_or_negative_values_reject_symbol(self, make_request):
        for bad_quote in (
            {"close": ["abc"]},
            {"close": [-1.0]},
            {"volume": [float("nan")]},
        ):
            payload = _chart_payload(
                timestamps=[1715212800],
                quote={
                    "open": [173.5],
                    "high": [175.0],
                    "low": [172.5],
                    "close": [174.5],
                    "volume": [52000000],
                    **bad_quote,
                },
            )
            make_request.return_value = _response(payload)

            result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

            self.assertEqual(result.records, [])
            self.assertEqual(result.successful_series, 0)
            self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_provider_error_payload_rejects_symbol(self, make_request, get_session):
        make_request.return_value = _response(
            _chart_payload(result=[], error={"code": "Not Found"})
        )

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.records, [])
        self.assertEqual(result.successful_series, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_malformed_dividend_event_rejects_symbol(self, make_request, get_session):
        payload = _chart_payload(
            events={"dividends": {str(DIVIDEND_EPOCH): {"amount": "abc"}}}
        )
        make_request.return_value = _response(payload)

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.records, [])
        self.assertEqual(result.successful_series, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")
        get_session.assert_not_called()

    # The collector never opens a database session: the patch target is
    # absent by design, so create=True makes the assertion below a real
    # regression guard instead of an import error.
    @patch("collectors.public_equities.get_session", create=True)
    @patch("collectors.public_equities.make_request")
    def test_network_failure_is_transient_symbol_error(self, make_request, get_session):
        import httpx

        make_request.side_effect = httpx.ConnectError("boom")

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.records, [])
        self.assertEqual(result.successful_series, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["error_class"], "transient_source")
        get_session.assert_not_called()

    @patch("collectors.public_equities.make_request")
    def test_descriptive_user_agent_is_sent(self, make_request):
        make_request.return_value = _response(_chart_payload())
        PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        headers = make_request.call_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], DEFAULT_USER_AGENT)

        make_request.reset_mock()
        make_request.return_value = _response(_chart_payload())
        PublicEquitiesCollector().collect(
            _config(user_agent="custom-agent/1.0"), "cid", now=FIXED_NOW
        )
        self.assertEqual(
            make_request.call_args.kwargs["headers"]["User-Agent"], "custom-agent/1.0"
        )

    @patch("collectors.public_equities.make_request")
    def test_multiple_symbols_use_one_request_each(self, make_request):
        def response(**kwargs):
            provider_symbol = kwargs["url"].rsplit("/", 1)[-1]
            return _response(
                _chart_payload(
                    meta={
                        "symbol": provider_symbol,
                        "currency": "USD",
                        "exchangeName": "NMS",
                        "regularMarketTime": REGULAR_MARKET_TIME,
                    }
                )
            )

        make_request.side_effect = response

        result = PublicEquitiesCollector().collect(
            _config(symbols=["AAPL", "BRK.B", "BLND.L"]),
            "cid",
            now=FIXED_NOW,
        )

        self.assertEqual(make_request.call_count, 3)
        self.assertCountEqual(
            [call.kwargs["url"] for call in make_request.call_args_list],
            [
                f"{DEFAULT_CHART_URL}/AAPL",
                f"{DEFAULT_CHART_URL}/BRK-B",
                f"{DEFAULT_CHART_URL}/BLND.L",
            ],
        )
        self.assertEqual(result.total_series, 3)
        self.assertEqual(result.successful_series, 3)
        self.assertEqual(result.metrics["api_calls_made"], 3)
        self.assertEqual(
            {record["symbol"] for record in result.records},
            {"AAPL", "BRK.B", "BLND.L"},
        )

    @patch("collectors.public_equities.make_request")
    def test_provider_symbol_mismatch_rejects_instrument(self, make_request):
        make_request.return_value = _response(
            _chart_payload(
                meta={"symbol": "MSFT", "regularMarketTime": REGULAR_MARKET_TIME}
            )
        )

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.successful_series, 0)
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    @patch("collectors.public_equities.make_request")
    def test_future_bar_rejects_symbol(self, make_request):
        future = int((FIXED_NOW + timedelta(days=2)).timestamp())
        make_request.return_value = _response(
            _chart_payload(
                timestamps=[future],
                quote={
                    "open": [10],
                    "high": [11],
                    "low": [9],
                    "close": [10],
                    "volume": [100],
                },
            )
        )

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.successful_series, 0)
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")

    @patch("collectors.public_equities.make_request")
    def test_oversize_response_rejects_before_json_parse(self, make_request):
        response = _response(_chart_payload())
        response.content = b"x" * (MAX_RESPONSE_BYTES + 1)
        make_request.return_value = response

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        self.assertEqual(result.successful_series, 0)
        self.assertEqual(result.errors[0]["error_class"], "invalid_source_data")
        response.json.assert_not_called()

    @patch("collectors.public_equities.make_request")
    def test_only_missing_symbols_use_bootstrap_range(self, make_request):
        def response(**kwargs):
            provider_symbol = kwargs["url"].rsplit("/", 1)[-1]
            return _response(
                _chart_payload(
                    meta={
                        "symbol": provider_symbol,
                        "currency": "USD",
                        "exchangeName": "NMS",
                        "regularMarketTime": REGULAR_MARKET_TIME,
                    }
                )
            )

        make_request.side_effect = response
        config = _config(symbols=["AAPL", "MSFT"], bootstrap_range="3mo")
        config["collectors"]["public_equities"]["_bootstrap_symbols"] = ["MSFT"]

        result = PublicEquitiesCollector().collect(config, "cid", now=FIXED_NOW)

        ranges = {
            call.kwargs["url"].rsplit("/", 1)[-1]: call.kwargs["params"]["range"]
            for call in make_request.call_args_list
        }
        self.assertEqual(ranges, {"AAPL": "1y", "MSFT": "3mo"})
        self.assertEqual(result.successful_series, 2)


class PublicEquitiesConfigValidationTests(unittest.TestCase):
    def test_unsafe_custom_origin_is_rejected(self):
        for origin in (
            "http://localhost:8000/chart",
            "https://192.168.1.10/chart",
            "https://169.254.169.254/chart",
            "http://example.com/chart",
        ):
            with self.assertRaisesRegex(ValueError, "chart_base_url"):
                PublicEquitiesCollector()._validated_origin({"chart_base_url": origin})

    def test_canonical_origin_is_accepted_without_validation(self):
        self.assertEqual(
            PublicEquitiesCollector()._validated_origin(
                {"chart_base_url": DEFAULT_CHART_URL}
            ),
            DEFAULT_CHART_URL,
        )

    def test_symbol_limit_is_bounded(self):
        symbols = [f"S{i:02d}" for i in range(51)]
        with self.assertRaisesRegex(ValueError, "exceeds the configured limit"):
            PublicEquitiesCollector()._symbols(_config(symbols=symbols))

        symbols = [f"S{i:03d}" for i in range(201)]
        with self.assertRaisesRegex(ValueError, "exceeds the configured limit"):
            PublicEquitiesCollector()._symbols(
                _config(symbols=symbols, max_symbols=200)
            )

    def test_symbol_format_is_validated(self):
        for bad in ("AAPL!", "A" * 21, "$AAPL", ""):
            with self.assertRaisesRegex(ValueError, "invalid public_equities symbol"):
                PublicEquitiesCollector()._symbols(_config(symbols=[bad]))

    def test_symbols_are_normalized_uppercase(self):
        self.assertEqual(
            PublicEquitiesCollector()._symbols(_config(symbols=["aapl", "Brk.B"])),
            ["AAPL", "BRK.B"],
        )

    def test_duplicate_symbols_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicates"):
            PublicEquitiesCollector()._symbols(_config(symbols=["AAPL", "aapl"]))

    def test_range_and_interval_are_whitelisted(self):
        collector = PublicEquitiesCollector()
        with self.assertRaisesRegex(ValueError, "range"):
            collector._range(_config(range="max"))
        with self.assertRaisesRegex(ValueError, "interval"):
            collector._interval(_config(interval="1h"))
        self.assertEqual(collector._range(_config(range="5y")), "5y")
        self.assertEqual(collector._interval(_config()), "1d")

    def test_timeout_is_bounded(self):
        collector = PublicEquitiesCollector()
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            collector._timeout(_config(timeout_seconds=1))
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            collector._timeout(_config(timeout_seconds=120))
        self.assertEqual(collector._timeout(_config()), 15.0)


class PublicEquitiesAdditionalBatchTests(unittest.TestCase):
    @patch("collectors.public_equities.make_request")
    def test_collected_actions_write_do_nothing_and_never_update(self, make_request):
        payload = _chart_payload(
            events={"dividends": {str(DIVIDEND_EPOCH): {"amount": 0.24}}}
        )
        make_request.return_value = _response(payload)

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        session = MagicMock()
        results = db.write_batches_in_session(session, result.additional_writes)

        self.assertEqual(results, [db.WriteResult(1, 1, 0, ())])
        statement = str(session.execute.call_args.args[0]).lower()
        self.assertIn("insert into corporate_actions", statement)
        self.assertIn("on conflict (action_id) do nothing", statement)
        self.assertNotIn("do update", statement)
        params = session.execute.call_args.args[1]
        self.assertEqual(params[0]["symbol"], "AAPL")
        self.assertIsInstance(params[0]["metadata"], str)
        session.commit.assert_not_called()

    @patch("collectors.public_equities.make_request")
    def test_collected_action_records_are_homogeneous(self, make_request):
        payload = _chart_payload(
            events={
                "dividends": {str(DIVIDEND_EPOCH): {"amount": 0.24}},
                "splits": {
                    str(SPLIT_EPOCH): {
                        "splitRatio": "2:1",
                        "numerator": 2,
                        "denominator": 1,
                    }
                },
            }
        )
        make_request.return_value = _response(payload)

        result = PublicEquitiesCollector().collect(_config(), "cid", now=FIXED_NOW)

        records = result.additional_writes[0].records
        self.assertEqual(len(records), 2)
        key_sets = {frozenset(record) for record in records}
        self.assertEqual(len(key_sets), 1)
        self.assertIn("action_id", records[0])
        self.assertIn("metadata", records[0])


class PublicEquitiesContractTests(unittest.TestCase):
    def test_target_table_and_conflict_columns(self):
        collector = PublicEquitiesCollector()
        self.assertEqual(collector.get_target_table(), "market_data")
        self.assertEqual(
            collector.get_conflict_columns(), ["symbol", "timeframe", "timestamp"]
        )

    def test_historical_bars_are_declared_insert_only(self):
        # Re-collection must never revise a stored historical bar: the
        # executor writes bars with ON CONFLICT DO NOTHING whenever a
        # collector declares insert_only, so updated_at can never be
        # bumped past accepted cutoffs by a rerun.
        self.assertIs(PublicEquitiesCollector.insert_only, True)

    def test_schedule_defaults_and_overrides(self):
        self.assertEqual(
            PublicEquitiesCollector().get_schedule(_config()), "0 7 * * 1-5"
        )
        self.assertEqual(
            PublicEquitiesCollector().get_schedule(_config(schedule="30 6 * * 1-5")),
            "30 6 * * 1-5",
        )
        self.assertEqual(PublicEquitiesCollector().get_schedule({}), "0 7 * * 1-5")

    @patch("collectors.public_equities.make_request")
    def test_health_check_reports_reachable(self, make_request):
        make_request.return_value = _response(_chart_payload())

        result = PublicEquitiesCollector().health_check(_config())

        self.assertTrue(result["healthy"])
        self.assertGreaterEqual(result["latency_ms"], 0)

    @patch("collectors.public_equities.make_request")
    def test_health_check_probes_dotted_symbol_via_provider_symbol(self, make_request):
        """BRK.B probes Yahoo as BRK-B exactly like collection does."""
        make_request.return_value = _response(_chart_payload())

        result = PublicEquitiesCollector().health_check(_config(symbols=["BRK.B"]))

        self.assertTrue(result["healthy"])
        self.assertEqual(
            make_request.call_args.kwargs["url"],
            f"{DEFAULT_CHART_URL}/BRK-B",
        )

    @patch("collectors.public_equities.make_request")
    def test_health_check_reports_provider_error(self, make_request):
        make_request.return_value = _response(
            _chart_payload(result=[], error={"code": "Not Found"})
        )

        result = PublicEquitiesCollector().health_check(_config())

        self.assertFalse(result["healthy"])

    @patch("collectors.public_equities.make_request")
    def test_health_check_reports_http_failure(self, make_request):
        make_request.return_value = _response(_chart_payload(), status=503)

        result = PublicEquitiesCollector().health_check(_config())

        self.assertFalse(result["healthy"])
        self.assertIn("503", result["message"])

    def test_health_check_reports_configuration_errors(self):
        result = PublicEquitiesCollector().health_check(
            _config(symbols=[], range="max")
        )
        self.assertFalse(result["healthy"])
        self.assertIn("range", result["message"])

        result = PublicEquitiesCollector().health_check(_config(symbols=[]))
        self.assertFalse(result["healthy"])
        self.assertIn("no configured symbols", result["message"])

    @patch("collectors.public_equities.make_request")
    def test_health_check_reports_unreachable(self, make_request):
        make_request.side_effect = RuntimeError("boom")

        result = PublicEquitiesCollector().health_check(_config())

        self.assertFalse(result["healthy"])
        self.assertIn("unreachable", result["message"])


if __name__ == "__main__":
    unittest.main()
