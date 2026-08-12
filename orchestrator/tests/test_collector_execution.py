import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import collector_execution as runtime
from collectors.base import CollectionResult
from errors import PersistenceError, TransientSourceError
from sources.news_result import NewsCollectionResult


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
        self.assertEqual(
            logger.error.call_args.kwargs["error_type"], "RuntimeError"
        )

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


if __name__ == "__main__":
    unittest.main()
