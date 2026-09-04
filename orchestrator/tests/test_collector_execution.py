import unittest
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import collector_execution as runtime
from collectors.base import CollectionResult, CollectionWriteBatch
from collectors.public_equities import corporate_action_id
from errors import PersistenceError, TransientSourceError
from events.publisher import PublicationResult
from events.repository import EventInsertResult
from sources.news_result import NewsCollectionResult

from db import BatchWriteError, WriteResult


class DynamicResearchUniverseTests(unittest.TestCase):
    def _config(self, symbols=None, max_symbols=3, include_active_theses=True):
        return {
            "collectors": {
                "public_equities": {
                    "symbols": ["AAPL"] if symbols is None else symbols,
                    "max_symbols": max_symbols,
                    "include_active_theses": include_active_theses,
                }
            }
        }

    def test_active_thesis_symbols_extend_options_collection(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "NVDA", "origin": "fusion", "status": "active"}
        ]
        config = {
            "collectors": {
                "cboe_options": {
                    "symbols": ["SPY"],
                    "max_symbols": 2,
                    "include_active_theses": True,
                }
            }
        }
        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            extended = runtime._with_active_thesis_symbols(config, "cboe_options")
        self.assertEqual(
            extended["collectors"]["cboe_options"]["symbols"],
            ["SPY", "NVDA"],
        )

    def test_active_thesis_symbols_extend_a_copied_bounded_config(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "BLND.L", "origin": "fusion", "status": "active"},
            {"symbol": "AAPL", "origin": "fusion", "status": "candidate"},
            {"symbol": "ICG.L", "origin": "fusion", "status": "paused"},
            {"symbol": "unsafe symbol", "origin": "fusion", "status": "active"},
            {"symbol": "SNN", "origin": "fusion", "status": "active"},
        ]
        config = self._config()

        with patch.object(
            runtime, "get_session", return_value=nullcontext(session)
        ) as get_session:
            extended = runtime._with_active_thesis_symbols(config)

        # Configured symbol keeps priority; the paused thesis is a live
        # fusion universe member; malformed symbols are never injected.
        self.assertEqual(
            extended["collectors"]["public_equities"]["symbols"],
            ["AAPL", "BLND.L", "ICG.L"],
        )
        # The original snapshot is frozen: neither the config nor any nested
        # mapping it shares with the extension is mutated.
        self.assertIsNot(extended, config)
        self.assertIsNot(extended["collectors"], config["collectors"])
        self.assertIsNot(
            extended["collectors"]["public_equities"],
            config["collectors"]["public_equities"],
        )
        self.assertEqual(config["collectors"]["public_equities"]["symbols"], ["AAPL"])
        get_session.assert_called_once_with(config)

    def test_checked_in_investment_universe_expands_without_database(self):
        config = {
            "collectors": {
                "public_equities": {
                    "symbols": ["AAPL"],
                    "max_symbols": 4,
                    "include_active_theses": False,
                    "include_investment_universe": True,
                }
            }
        }
        companies = [
            {"symbol": "AAPL"},
            {"symbol": "MSFT"},
            {"symbol": "ASML"},
            {"symbol": "MRO.L"},
            {"symbol": "IGNORED"},
        ]
        with (
            patch(
                "investment_universe.top_us_uk_eu_companies",
                return_value=companies,
            ),
            patch.object(runtime, "get_session") as get_session,
        ):
            extended = runtime._with_active_thesis_symbols(config)

        self.assertEqual(
            extended["collectors"]["public_equities"]["symbols"],
            ["AAPL", "MSFT", "ASML", "MRO.L"],
        )
        self.assertEqual(config["collectors"]["public_equities"]["symbols"], ["AAPL"])
        get_session.assert_not_called()

    def test_public_equity_bootstrap_marks_only_symbols_without_rows(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "AAPL"}
        ]
        config = {
            "collectors": {
                "public_equities": {
                    "symbols": ["AAPL", "MSFT"],
                }
            }
        }

        with patch.object(
            runtime,
            "get_session",
            return_value=nullcontext(session),
        ):
            extended = runtime._with_public_equity_bootstrap(config)

        self.assertEqual(
            extended["collectors"]["public_equities"]["_bootstrap_symbols"],
            ["MSFT"],
        )
        self.assertNotIn(
            "_bootstrap_symbols",
            config["collectors"]["public_equities"],
        )
        self.assertIn("FROM market_data", str(session.execute.call_args.args[0]))

    def test_non_equity_thesis_symbols_do_not_consume_expectations_slots(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "EURUSD=X"},
            {"symbol": "NVDA"},
            {"symbol": "TSLA"},
        ]
        config = {
            "collectors": {
                "company_expectations": {
                    "symbols": ["AAPL"],
                    "max_symbols": 2,
                    "include_active_theses": True,
                }
            }
        }

        with patch.object(
            runtime,
            "get_session",
            return_value=nullcontext(session),
        ):
            extended = runtime._with_active_thesis_symbols(
                config,
                "company_expectations",
            )

        self.assertEqual(
            extended["collectors"]["company_expectations"]["symbols"],
            ["AAPL", "NVDA"],
        )
        sql = str(session.execute.call_args.args[0])
        self.assertIn("^[A-Z0-9][A-Z0-9.-]{0,19}$", sql)

    def test_dynamic_symbol_query_limits_to_live_fusion_theses(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        config = self._config(max_symbols=7)

        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            runtime._with_active_thesis_symbols(config)

        sql = str(session.execute.call_args.args[0])
        # Only fusion-origin theses in a live state qualify; every other
        # origin or terminal/draft state is excluded by the query itself.
        self.assertIn("origin = 'fusion'", sql)
        self.assertIn("status IN ('candidate', 'active', 'paused')", sql)
        self.assertNotIn("'draft'", sql)
        self.assertNotIn("'archived'", sql)
        self.assertNotIn("'closed'", sql)
        # The configured symbol is excluded inside the same bounded query.
        self.assertEqual(
            session.execute.call_args.args[1], {"limit": 14, "excluded": ["AAPL"]}
        )

    def test_configured_symbols_keep_priority_within_the_cap(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "NVDA", "origin": "fusion", "status": "active"},
            {"symbol": "MSFT", "origin": "fusion", "status": "active"},
            {"symbol": "TSLA", "origin": "fusion", "status": "active"},
        ]
        config = self._config(symbols=["AAPL", "MSFT"], max_symbols=3)

        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            extended = runtime._with_active_thesis_symbols(config)

        # Configured symbols occupy the first slots in order, the dynamic
        # universe is deduplicated against them, and the merged list never
        # exceeds the cap (TSLA is dropped).
        self.assertEqual(
            extended["collectors"]["public_equities"]["symbols"],
            ["AAPL", "MSFT", "NVDA"],
        )

    def test_database_failure_fails_soft_to_configured_symbols(self):
        session = MagicMock()
        session.execute.side_effect = RuntimeError("database unavailable")
        config = self._config()

        with (
            patch.object(runtime, "get_session", return_value=nullcontext(session)),
            patch.object(runtime, "logger") as logger,
        ):
            self.assertIs(runtime._with_active_thesis_symbols(config), config)

        self.assertEqual(config["collectors"]["public_equities"]["symbols"], ["AAPL"])
        logger.warning.assert_called_once()

    def test_disabled_dynamic_universe_does_not_open_a_session(self):
        config = self._config(include_active_theses=False)
        with patch.object(runtime, "get_session") as get_session:
            self.assertIs(runtime._with_active_thesis_symbols(config), config)
        get_session.assert_not_called()

    def test_dynamic_query_filters_and_normalizes_symbols_before_limit(self):
        """The grammar predicate precedes grouping/order/LIMIT in one query."""
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        config = self._config(max_symbols=7)

        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            runtime._with_active_thesis_symbols(config)

        sql = str(session.execute.call_args.args[0])
        # The grammar predicate is applied in SQL before any row is counted
        # toward LIMIT, so invalid high-ranked persisted symbols cannot
        # starve eligible theses.
        predicate = "UPPER(BTRIM(symbol)) ~ '^[A-Z0-9][A-Z0-9.^=-]{0,19}$'"
        self.assertIn(predicate, sql)
        self.assertLess(sql.index(predicate), sql.index("GROUP BY"))
        self.assertLess(sql.index("GROUP BY"), sql.index("ORDER BY"))
        self.assertLess(sql.index("ORDER BY"), sql.index("LIMIT"))
        # Case/whitespace variants collapse into one canonical group and
        # configured symbols are excluded inside the query, so duplicates
        # cannot consume dynamic slots.
        self.assertIn("GROUP BY UPPER(BTRIM(symbol))", sql)
        self.assertIn("UPPER(BTRIM(symbol)) <> ALL(:excluded)", sql)
        # Exactly one bounded query with the doubled LIMIT as margin.
        session.execute.assert_called_once()
        self.assertEqual(
            session.execute.call_args.args[1], {"limit": 14, "excluded": ["AAPL"]}
        )

    def test_invalid_high_ranked_rows_cannot_starve_the_dynamic_universe(self):
        """More than 2*cap invalid rows never block valid eligible symbols."""
        session = MagicMock()
        invalid = {"symbol": "!!!INVALID", "origin": "fusion", "status": "active"}
        rows = [invalid] * (2 * 3 + 2) + [
            {"symbol": "NVDA", "origin": "fusion", "status": "active"},
            {"symbol": "TSLA", "origin": "fusion", "status": "active"},
            {"symbol": "MSFT", "origin": "fusion", "status": "active"},
        ]
        session.execute.return_value.mappings.return_value.all.return_value = rows
        config = self._config(max_symbols=3)

        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            extended = runtime._with_active_thesis_symbols(config)

        # The valid symbols still fill the bounded universe even when the
        # returned row set is polluted with more than 2*cap invalid rows,
        # and only the single bounded query is used.
        self.assertEqual(
            extended["collectors"]["public_equities"]["symbols"],
            ["AAPL", "NVDA", "TSLA"],
        )
        session.execute.assert_called_once()
        self.assertEqual(
            session.execute.call_args.args[1], {"limit": 6, "excluded": ["AAPL"]}
        )

    def test_configured_duplicate_rows_do_not_consume_dynamic_slots(self):
        """Case/whitespace variants of configured symbols never displace
        eligible theses (excluded in SQL, deduplicated in Python)."""
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "aapl", "origin": "fusion", "status": "active"},
            {"symbol": " AAPL ", "origin": "fusion", "status": "active"},
            {"symbol": "NVDA", "origin": "fusion", "status": "active"},
            {"symbol": "TSLA", "origin": "fusion", "status": "active"},
            {"symbol": "MSFT", "origin": "fusion", "status": "active"},
        ]
        config = self._config(symbols=["AAPL"], max_symbols=3)

        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            extended = runtime._with_active_thesis_symbols(config)

        # The configured duplicates are skipped and the dynamic slots are
        # filled by the eligible theses that follow them.
        self.assertEqual(
            extended["collectors"]["public_equities"]["symbols"],
            ["AAPL", "NVDA", "TSLA"],
        )
        session.execute.assert_called_once()
        self.assertEqual(
            session.execute.call_args.args[1], {"limit": 6, "excluded": ["AAPL"]}
        )

    def test_dynamic_query_without_configured_symbols_omits_exclusion(self):
        """An empty configured list keeps the query single-bounded and the
        exclusion clause out of the SQL entirely."""
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "NVDA", "origin": "fusion", "status": "active"},
            {"symbol": "TSLA", "origin": "fusion", "status": "active"},
        ]
        config = self._config(symbols=[], max_symbols=2)

        with patch.object(runtime, "get_session", return_value=nullcontext(session)):
            extended = runtime._with_active_thesis_symbols(config)

        sql = str(session.execute.call_args.args[0])
        self.assertNotIn(":excluded", sql)
        session.execute.assert_called_once()
        self.assertEqual(session.execute.call_args.args[1], {"limit": 4})
        self.assertEqual(
            extended["collectors"]["public_equities"]["symbols"],
            ["NVDA", "TSLA"],
        )

    def test_run_collector_impl_passes_expanded_config_to_public_equities(self):
        """The executor boundary hands the copied expansion to the collector."""
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            {"symbol": "TSLA", "origin": "fusion", "status": "active"},
        ]
        collector = MagicMock()
        collector.collect.return_value = []
        collector.get_target_table.return_value = "market_data"
        collector.get_conflict_columns.return_value = [
            "symbol",
            "timeframe",
            "timestamp",
        ]
        config = self._config(max_symbols=2)

        with (
            patch.object(runtime, "get_session", return_value=nullcontext(session)),
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", config, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        passed = collector.collect.call_args.args[0]
        # The collector receives the expanded copy, never the caller's
        # original snapshot.
        self.assertIsNot(passed, config)
        self.assertEqual(
            passed["collectors"]["public_equities"]["symbols"],
            ["AAPL", "TSLA"],
        )
        self.assertEqual(config["collectors"]["public_equities"]["symbols"], ["AAPL"])


class CollectorErrorMetadataTests(unittest.TestCase):
    def test_transient_source_failure_is_retryable(self):
        collector = MagicMock()
        collector.collect.side_effect = TransientSourceError("upstream timed out")

        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl("fred", {}, "correlation-id", False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "transient_source")
        self.assertTrue(result["retryable"])

    def test_generic_failure_never_persists_or_logs_raw_exception_text(self):
        secret = "RAW_SECRET_abc123_bearer_token"
        collector = MagicMock()
        collector.collect.side_effect = RuntimeError(
            f"provider rejected token={secret}"
        )
        written = {}

        def capture(**kwargs):
            written.update(kwargs)

        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(runtime, "_write_collection_log", side_effect=capture),
            patch.object(runtime, "logger") as logger,
            patch.dict("os.environ", {}, clear=False),
        ):
            result = runtime._run_collector_impl("fred", {}, "correlation-id", False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "error")
        self.assertNotIn(secret, str(result["error"]))
        self.assertNotIn(secret, str(written.get("error_message")))
        self.assertIsNone(written.get("error_traceback"))
        for call in logger.error.call_args_list:
            self.assertNotIn(secret, str(call))
        self.assertEqual(logger.error.call_args.kwargs["error_type"], "RuntimeError")

    def test_persistence_failure_is_retryable_and_explicit(self):
        collector = MagicMock()
        collector.collect.return_value = [{"series_id": "GDP"}]
        collector.get_target_table.return_value = "macro_series"
        collector.get_conflict_columns.return_value = ["series_id"]

        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(
                runtime,
                "upsert_records",
                side_effect=PersistenceError("database unavailable"),
            ),
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl("fred", {}, "correlation-id", False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "persistence")
        self.assertTrue(result["retryable"])

    def test_structured_request_failures_remain_retryable_source_errors(self):
        collector = MagicMock()
        collector.collect.return_value = CollectionResult(
            errors=[
                {
                    "code": "request_failed",
                    "error_class": "transient_source",
                }
            ],
            total_series=1,
        )

        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl("fred", {}, "correlation-id", False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "transient_source")
        self.assertTrue(result["retryable"])

    def test_structured_invalid_data_is_not_retryable(self):
        collector = MagicMock()
        collector.collect.return_value = CollectionResult(
            errors=[
                {
                    "code": "parse_failed",
                    "error_class": "invalid_source_data",
                }
            ],
            total_series=1,
        )

        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl("fred", {}, "correlation-id", False)

        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["error_class"], "invalid_source_data")
        self.assertFalse(result["retryable"])

    def test_invalid_news_payload_is_not_reported_as_transient(self):
        outcome = NewsCollectionResult(
            [],
            "error",
            "upstream payload is malformed",
            error_class="invalid_source_data",
        )
        with (
            patch("sources.news_registry.get_news_collector", return_value=MagicMock()),
            patch("sources.news_feed.collect_and_publish", return_value=outcome),
            patch.object(runtime, "advisory_lock", return_value=nullcontext()),
        ):
            result = runtime.run_news_source(
                "kobeissi", "correlation-id", {}, manage_lifecycle=False
            )

        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["error_class"], "invalid_source_data")
        self.assertFalse(result["retryable"])


class MultiBatchWriteTests(unittest.TestCase):
    def setUp(self):
        self.collector = MagicMock()
        self.collector.get_target_table.return_value = "market_data"
        self.collector.get_conflict_columns.return_value = [
            "symbol",
            "timeframe",
            "timestamp",
        ]
        self.action_batch = CollectionWriteBatch(
            "corporate_actions",
            [{"action_id": "a1"}],
            ["action_id"],
            insert_only=True,
        )

    def test_additional_only_output_writes_primary_plus_additional_batches(self):
        self.collector.collect.return_value = CollectionResult(
            records=[],
            additional_writes=[self.action_batch],
            total_series=1,
            successful_series=1,
            metrics={"api_calls_made": 1},
        )
        with (
            patch.object(runtime, "get_collector", return_value=self.collector),
            patch.object(
                runtime, "get_session", return_value=nullcontext(MagicMock())
            ) as get_session,
            patch.object(
                runtime,
                "write_batches_in_session",
                return_value=[WriteResult(0, 0, 0, ()), WriteResult(1, 1, 0, ())],
            ) as write_batches,
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", {}, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["records_fetched"], 0)
        self.assertEqual(result["records_written"], 1)
        self.assertEqual(result["metrics"]["db_batches_total"], 2)
        self.assertEqual(result["metrics"]["db_batches_written"], 2)
        self.assertEqual(result["metrics"]["db_records_written"], 1)
        self.assertEqual(result["metrics"]["db_records_failed"], 0)
        write_batches.assert_called_once()
        get_session.assert_called_once_with({})
        batches = write_batches.call_args.args[1]
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].table_name, "market_data")
        self.assertEqual(batches[0].records, [])
        self.assertEqual(
            batches[0].conflict_columns, ["symbol", "timeframe", "timestamp"]
        )
        self.assertFalse(batches[0].insert_only)
        self.assertIs(batches[1], self.action_batch)

    def test_primary_and_additional_batches_share_one_session(self):
        bar = {
            "symbol": "AAPL",
            "timeframe": "1d",
            "timestamp": "2024-05-09T00:00:00+00:00",
        }
        self.collector.collect.return_value = CollectionResult(
            records=[bar],
            additional_writes=[self.action_batch],
            total_series=1,
            successful_series=1,
        )
        with (
            patch.object(runtime, "get_collector", return_value=self.collector),
            patch.object(
                runtime, "get_session", return_value=nullcontext(MagicMock())
            ) as get_session,
            patch.object(
                runtime,
                "write_batches_in_session",
                return_value=[WriteResult(1, 1, 0, ()), WriteResult(1, 1, 0, ())],
            ) as write_batches,
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", {}, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["records_written"], 2)
        get_session.assert_called_once_with({})
        write_batches.assert_called_once()
        batches = write_batches.call_args.args[1]
        self.assertEqual(batches[0].records, [bar])
        self.assertFalse(batches[0].insert_only)
        self.assertTrue(batches[1].insert_only)

    def test_insert_only_collector_bars_batch_is_do_nothing(self):
        # A collector that declares insert_only must hand the executor a
        # DO NOTHING primary batch, never a revising upsert.
        class ImmutableBarsCollector:
            insert_only = True

            def get_target_table(self):
                return "market_data"

            def get_conflict_columns(self):
                return ["symbol", "timeframe", "timestamp"]

            def collect(self, config, correlation_id):
                return CollectionResult(
                    records=[
                        {
                            "symbol": "AAPL",
                            "timeframe": "1d",
                            "timestamp": "2024-05-09T00:00:00+00:00",
                        }
                    ],
                    additional_writes=[self.action_batch],
                    total_series=1,
                    successful_series=1,
                )

        collector = ImmutableBarsCollector()
        collector.action_batch = self.action_batch
        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(runtime, "get_session", return_value=nullcontext(MagicMock())),
            patch.object(
                runtime,
                "write_batches_in_session",
                return_value=[WriteResult(1, 1, 0, ()), WriteResult(1, 1, 0, ())],
            ) as write_batches,
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", {}, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        batches = write_batches.call_args.args[1]
        self.assertTrue(batches[0].insert_only)
        self.assertTrue(batches[1].insert_only)

    def test_insert_only_collector_legacy_path_keeps_do_nothing(self):
        # The single-table legacy writer honors the same insert-only policy
        # instead of its default upsert.
        class ImmutableLegacyCollector:
            insert_only = True

            def get_target_table(self):
                return "market_data"

            def get_conflict_columns(self):
                return ["symbol", "timeframe", "timestamp"]

            def collect(self, config, correlation_id):
                return [
                    {
                        "symbol": "AAPL",
                        "timeframe": "1d",
                        "timestamp": "2024-05-09T00:00:00+00:00",
                    }
                ]

        with (
            patch.object(
                runtime,
                "get_collector",
                return_value=ImmutableLegacyCollector(),
            ),
            patch.object(
                runtime,
                "upsert_records",
                return_value=MagicMock(written=1, attempted=1, status="success"),
            ) as upsert_records,
            patch.object(runtime, "write_batches_in_session") as write_batches,
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", {}, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        upsert_records.assert_called_once_with(
            table_name="market_data",
            records=[
                {
                    "symbol": "AAPL",
                    "timeframe": "1d",
                    "timestamp": "2024-05-09T00:00:00+00:00",
                }
            ],
            conflict_columns=["symbol", "timeframe", "timestamp"],
            config={},
            insert_only=True,
        )
        write_batches.assert_not_called()

    def test_insert_only_collector_event_path_forwards_flag(self):
        # The event publication path writes primary raw rows as DO NOTHING
        # when the collector declares insert_only.
        class ImmutableEventCollector:
            insert_only = True

            def get_target_table(self):
                return "market_data"

            def get_conflict_columns(self):
                return ["symbol", "timeframe", "timestamp"]

            def collect(self, config, correlation_id):
                return CollectionResult(
                    records=[
                        {
                            "symbol": "AAPL",
                            "timeframe": "1d",
                            "timestamp": "2024-05-09T00:00:00+00:00",
                        }
                    ],
                    total_series=1,
                    successful_series=1,
                )

        config = {
            "event_pipeline": {
                "enabled": True,
                "sources": ["public_equities"],
            },
            "collectors": {"public_equities": {}},
        }
        with (
            patch.object(
                runtime,
                "get_collector",
                return_value=ImmutableEventCollector(),
            ),
            patch.object(
                runtime,
                "publish_collector_records_atomic",
                return_value=PublicationResult(1, 1, 1, 0, 1),
            ) as publish_atomic,
            patch.object(runtime, "write_batches_in_session") as write_batches,
            patch.object(runtime, "_write_collection_log"),
            patch.object(runtime, "_record_source_freshness"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", config, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        publish_atomic.assert_called_once()
        self.assertIs(publish_atomic.call_args.kwargs["insert_only"], True)
        write_batches.assert_not_called()

    def test_single_table_collector_keeps_legacy_upsert_path(self):
        self.collector.get_target_table.return_value = "macro_series"
        self.collector.get_conflict_columns.return_value = ["series_id"]
        self.collector.collect.return_value = [{"series_id": "GDP"}]
        with (
            patch.object(runtime, "get_collector", return_value=self.collector),
            patch.object(
                runtime,
                "upsert_records",
                return_value=MagicMock(written=1, attempted=1, status="success"),
            ) as upsert_records,
            patch.object(runtime, "write_batches_in_session") as write_batches,
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl("fred", {}, "correlation-id", False)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["records_written"], 1)
        upsert_records.assert_called_once_with(
            table_name="macro_series",
            records=[{"series_id": "GDP"}],
            conflict_columns=["series_id"],
            config={},
            insert_only=False,
        )
        write_batches.assert_not_called()

    def test_batch_write_failure_is_a_persistence_failure(self):
        self.collector.collect.return_value = CollectionResult(
            records=[{"symbol": "AAPL", "timeframe": "1d", "timestamp": "t"}],
            additional_writes=[self.action_batch],
            total_series=1,
            successful_series=1,
        )
        with (
            patch.object(runtime, "get_collector", return_value=self.collector),
            patch.object(runtime, "get_session", return_value=nullcontext(MagicMock())),
            patch.object(
                runtime,
                "write_batches_in_session",
                side_effect=BatchWriteError(1, "corporate_actions", "OperationalError"),
            ),
            patch.object(runtime, "_write_collection_log"),
        ):
            result = runtime._run_collector_impl(
                "public_equities", {}, "correlation-id", False
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "persistence")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["records_written"], 0)

    def test_event_enabled_source_passes_additional_writes_into_publisher(self):
        self.collector.get_target_table.return_value = "macro_series"
        self.collector.get_conflict_columns.return_value = ["series_id", "observed_at"]
        self.collector.collect.return_value = CollectionResult(
            records=[
                {
                    "series_id": "GDP",
                    "observed_at": datetime(2026, 8, 5, 12, tzinfo=UTC),
                    "value": 3.2,
                }
            ],
            additional_writes=[self.action_batch],
            total_series=1,
            successful_series=1,
        )
        config = {
            "event_pipeline": {"enabled": True, "sources": ["fred"]},
            "collectors": {"fred": {}},
        }
        with (
            patch.object(runtime, "get_collector", return_value=self.collector),
            patch.object(
                runtime,
                "publish_collector_records_atomic",
                return_value=PublicationResult(1, 1, 1, 0, 1),
            ) as publish_atomic,
            patch.object(runtime, "write_batches_in_session") as write_batches,
            patch.object(runtime, "_write_collection_log"),
            patch.object(runtime, "_record_source_freshness"),
        ):
            result = runtime._run_collector_impl(
                "fred", config, "correlation-id", False
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["metrics"]["events_inserted"], 1)
        publish_atomic.assert_called_once()
        self.assertEqual(
            publish_atomic.call_args.kwargs["additional_writes"], [self.action_batch]
        )
        write_batches.assert_not_called()


class EventPublicationHookTests(unittest.TestCase):
    def _fred_record(self):
        return {
            "series_id": "GDP",
            "observed_at": datetime(2026, 8, 5, 12, tzinfo=UTC),
            "value": 3.2,
            "released_at": None,
            "revision_at": None,
            "metadata": {"units": "percent"},
        }

    def _transaction(self, session, events):
        @contextmanager
        def transaction():
            events.append("open")
            try:
                yield session
                session.commit()
                events.append("commit")
            except Exception:
                session.rollback()
                events.append("rollback")
                raise
            finally:
                session.close()
                events.append("close")

        return transaction()

    def _publisher_patches(self, session, events):
        return (
            patch(
                "events.publisher.get_session",
                return_value=self._transaction(session, events),
            ),
            patch("events.publisher.find_latest_event", return_value=None),
            patch(
                "events.publisher.insert_event",
                side_effect=lambda active, event, **kwargs: EventInsertResult(
                    event=event, inserted=True, outbox_inserted=True
                ),
            ),
            patch("events.publisher.upsert_raw"),
        )

    def _corporate_action_record(self):
        return {
            "action_id": corporate_action_id(
                "public_equities", "AAPL", "dividend", date(2024, 5, 8), amount=0.24
            ),
            "symbol": "AAPL",
            "action_type": "dividend",
            "effective_date": date(2024, 5, 8),
            "source": "public_equities",
            "source_timestamp": datetime(2024, 5, 8, tzinfo=UTC),
            "available_at": datetime(2024, 5, 13, tzinfo=UTC),
            "amount": 0.24,
            "ratio_numerator": None,
            "ratio_denominator": None,
            "description": None,
            "metadata": {"provider_event_key": "1715126400"},
        }

    def test_hook_writes_additional_batches_in_the_publisher_transaction(self):
        from events.publisher import publish_collector_records_atomic

        session = MagicMock()
        events = []
        batch = CollectionWriteBatch(
            "corporate_actions",
            [self._corporate_action_record()],
            ["action_id"],
            insert_only=True,
        )
        p0, p1, p2, p3 = self._publisher_patches(session, events)
        with (
            p0,
            p1,
            p2,
            p3,
            patch("events.publisher._insert_raw_do_nothing") as insert_raw,
        ):
            result = publish_collector_records_atomic(
                source_id="fred",
                table_name="macro_series",
                records=[self._fred_record()],
                conflict_columns=["series_id", "observed_at"],
                additional_writes=[batch],
            )

        # Counts aggregate across the primary records and the additional
        # batch: one fred raw/event plus one corporate action raw/event.
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.raw_written, 2)
        self.assertEqual(result.events_inserted, 2)
        self.assertEqual(result.events_deduplicated, 0)
        self.assertEqual(result.outbox_inserted, 2)
        self.assertEqual(events, ["open", "commit", "close"])
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()
        # The additional batch ran on the same session/transaction as the
        # primary publication. Corporate actions use immutable INSERT ...
        # ON CONFLICT DO NOTHING semantics, never the mutable upsert helper.
        insert_raw.assert_called_once()
        self.assertIs(insert_raw.call_args.args[0], session)
        self.assertEqual(insert_raw.call_args.args[1], "corporate_actions")
        self.assertEqual(insert_raw.call_args.args[3], ["action_id"])

    def test_hook_additional_batch_failure_rolls_back_events_and_raw_writes(self):
        from events.publisher import publish_collector_records_atomic

        session = MagicMock()
        events = []
        batch = CollectionWriteBatch(
            "corporate_actions",
            [self._corporate_action_record()],
            ["action_id"],
            insert_only=True,
        )
        calls = [0]

        def insert_event(active, event, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                # The corporate action event fails after the fred event and
                # raw write already executed on this session.
                raise RuntimeError("boom")
            return EventInsertResult(event=event, inserted=True, outbox_inserted=True)

        p0, p1, p2, p3 = self._publisher_patches(session, events)
        p2 = patch("events.publisher.insert_event", side_effect=insert_event)
        with p0, p1, p2, p3:
            with self.assertRaises(RuntimeError) as raised:
                publish_collector_records_atomic(
                    source_id="fred",
                    table_name="macro_series",
                    records=[self._fred_record()],
                    conflict_columns=["series_id", "observed_at"],
                    additional_writes=[batch],
                )

        self.assertEqual(type(raised.exception).__name__, "RuntimeError")
        self.assertEqual(events, ["open", "rollback", "close"])
        session.commit.assert_not_called()
        session.rollback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
