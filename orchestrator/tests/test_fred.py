import sys
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.fred import FRED_OBSERVATIONS_URL, FRED_SERIES_URL, FredCollector


class FredMetadataPersistenceTests(unittest.TestCase):
    def _config(self, series=None, **fred_overrides):
        fred = {
            "api_key": "test-key",
            "schedule": "0 6 * * *",
            "metadata_ttl_days": 30,
            "series": series or [{"id": "GDP", "frequency": "quarterly"}],
        }
        fred.update(fred_overrides)
        return {"database": {}, "collectors": {"fred": fred}}

    @staticmethod
    def _response(payload):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    @staticmethod
    def _metadata_row(series_id="GDP", fetched_at=None):
        return {
            "series_id": series_id,
            "title": f"Title {series_id}",
            "units": "Percent",
            "seasonal_adjustment": "SA",
            "frequency": "Quarterly",
            "fetched_at": fetched_at or datetime.now(timezone.utc),
        }

    def _query_with_metadata(self, rows):
        def query_latest(*, table_name, filters, **kwargs):
            if table_name == "macro_series_metadata":
                row = rows.get(filters["series_id"])
                return [row] if row is not None else []
            if table_name == "macro_series":
                return []
            raise AssertionError(table_name)

        return query_latest

    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_fresh_persisted_metadata_skips_metadata_http_call(self, query_latest, make_request):
        query_latest.side_effect = self._query_with_metadata(
            {"GDP": self._metadata_row(fetched_at=datetime.now(timezone.utc) - timedelta(days=29))}
        )
        make_request.return_value = self._response(
            {"observations": [{"date": "2025-01-01", "value": "1.5"}]}
        )

        result = FredCollector().collect(self._config(), "cid")

        self.assertEqual([record["series_id"] for record in result.records], ["GDP"])
        self.assertEqual(make_request.call_count, 1)
        self.assertEqual(make_request.call_args.kwargs["url"], FRED_OBSERVATIONS_URL)

    @patch("collectors.fred.get_session")
    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_missing_metadata_fetches_once_and_parameterized_transactional_upsert(
        self, query_latest, make_request, get_session
    ):
        query_latest.side_effect = self._query_with_metadata({})
        make_request.side_effect = [
            self._response(
                {"seriess": [{"title": "GDP", "units": "USD", "frequency": "Quarterly"}]}
            ),
            self._response({"observations": []}),
        ]
        session = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = session
        get_session.return_value = context

        FredCollector().collect(self._config(), "cid")

        self.assertEqual(
            [call.kwargs["url"] for call in make_request.call_args_list],
            [FRED_SERIES_URL, FRED_OBSERVATIONS_URL],
        )
        get_session.assert_called_once_with(self._config())
        statement, params = session.execute.call_args.args
        sql = str(statement).lower()
        self.assertIn("insert into macro_series_metadata", sql)
        self.assertIn("on conflict (series_id) do update", sql)
        self.assertNotIn("test-key", sql)
        self.assertEqual(params["series_id"], "GDP")
        self.assertIsInstance(params["fetched_at"], datetime)
        self.assertIsNotNone(params["fetched_at"].tzinfo)

    @patch("collectors.fred.get_session")
    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_new_collector_instance_uses_metadata_persisted_by_prior_instance(
        self, query_latest, make_request, get_session
    ):
        rows = {}
        query_latest.side_effect = self._query_with_metadata(rows)
        make_request.return_value = self._response(
            {"seriess": [{"title": "GDP", "units": "USD", "frequency": "Quarterly"}]}
        )
        session = MagicMock()

        @contextmanager
        def session_context(_config):
            yield session
            rows["GDP"] = {
                **session.execute.call_args.args[1],
            }

        get_session.side_effect = session_context

        first = FredCollector()._fetch_series_metadata("GDP", "test-key", "cid", self._config())
        second = FredCollector()._fetch_series_metadata("GDP", "test-key", "cid", self._config())

        self.assertEqual(first, second)
        self.assertEqual(make_request.call_count, 1)

    @patch("collectors.fred.get_session")
    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_expired_timezone_aware_metadata_is_refetched(
        self, query_latest, make_request, get_session
    ):
        fetched_at = datetime.now(timezone(timedelta(hours=9))) - timedelta(days=31)
        query_latest.side_effect = self._query_with_metadata(
            {"GDP": self._metadata_row(fetched_at=fetched_at)}
        )
        make_request.return_value = self._response({"seriess": [{"title": "New GDP"}]})
        context = MagicMock()
        context.__enter__.return_value = MagicMock()
        get_session.return_value = context

        metadata = FredCollector()._fetch_series_metadata(
            "GDP", "test-key", "cid", self._config()
        )

        self.assertEqual(metadata["title"], "New GDP")
        self.assertEqual(make_request.call_count, 1)

    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_malformed_db_row_is_safe_miss_and_does_not_crash_other_series(
        self, query_latest, make_request
    ):
        malformed = self._metadata_row("BAD", fetched_at="not-a-date")
        good = self._metadata_row("GOOD")
        query_latest.side_effect = self._query_with_metadata({"BAD": malformed, "GOOD": good})

        def request(*, url, params, **kwargs):
            if url == FRED_SERIES_URL:
                raise RuntimeError("metadata unavailable")
            return self._response(
                {"observations": [{"date": "2025-01-01", "value": "2.0"}]}
            )

        make_request.side_effect = request
        config = self._config(
            series=[
                {"id": "BAD", "frequency": "monthly"},
                {"id": "GOOD", "frequency": "monthly"},
            ]
        )

        result = FredCollector().collect(config, "cid")

        self.assertTrue(result.partial_failure)
        self.assertEqual([record["series_id"] for record in result.records], ["GOOD"])
        self.assertEqual(result.errors[0]["series_id"], "BAD")

    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_warm_n_series_makes_n_observation_calls_and_zero_metadata_calls(
        self, query_latest, make_request
    ):
        series = [
            {"id": "A", "frequency": "daily"},
            {"id": "B", "frequency": "monthly"},
            {"id": "C", "frequency": "quarterly"},
        ]
        query_latest.side_effect = self._query_with_metadata(
            {entry["id"]: self._metadata_row(entry["id"]) for entry in series}
        )
        make_request.return_value = self._response({"observations": []})

        result = FredCollector().collect(self._config(series=series), "cid")

        self.assertEqual(result.successful_series, 3)
        self.assertEqual(make_request.call_count, 3)
        self.assertTrue(
            all(call.kwargs["url"] == FRED_OBSERVATIONS_URL for call in make_request.call_args_list)
        )


class FredObservationConcurrencyTests(FredMetadataPersistenceTests):
    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_observation_fetches_are_bounded_and_results_keep_configured_order(
        self, query_latest, make_request
    ):
        series = [
            {"id": "A", "frequency": "daily"},
            {"id": "B", "frequency": "daily"},
            {"id": "C", "frequency": "daily"},
        ]
        query_threads = []
        metadata_rows = {
            entry["id"]: self._metadata_row(entry["id"]) for entry in series
        }

        def query(*args, **kwargs):
            query_threads.append(threading.get_ident())
            return self._query_with_metadata(metadata_rows)(*args, **kwargs)

        query_latest.side_effect = query
        lock = threading.Lock()
        release_a = threading.Event()
        active = 0
        max_active = 0
        completed = []

        def request(*, url, params, **kwargs):
            nonlocal active, max_active
            self.assertEqual(url, FRED_OBSERVATIONS_URL)
            series_id = params["series_id"]
            with lock:
                active += 1
                max_active = max(max_active, active)
            if series_id == "A":
                release_a.wait(0.3)
            elif series_id == "B":
                release_a.set()
            else:
                time.sleep(0.01)
            with lock:
                completed.append(series_id)
                active -= 1
            return self._response(
                {"observations": [{"date": "2025-01-01", "value": str(ord(series_id))}]}
            )

        make_request.side_effect = request
        config = self._config(series=series, max_concurrency=2)

        result = FredCollector().collect(config, "cid")

        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, 2)
        self.assertNotEqual(completed, ["A", "B", "C"])
        self.assertEqual([record["series_id"] for record in result.records], ["A", "B", "C"])
        self.assertEqual(len(set(query_threads)), 1, "database metadata reads must stay sequential")

    def test_max_concurrency_is_validated_and_clamped(self):
        self.assertEqual(FredCollector._max_concurrency({}), 4)
        self.assertEqual(FredCollector._max_concurrency({"max_concurrency": 0}), 1)
        self.assertEqual(FredCollector._max_concurrency({"max_concurrency": -5}), 1)
        self.assertEqual(FredCollector._max_concurrency({"max_concurrency": 999}), 16)
        self.assertEqual(FredCollector._max_concurrency({"max_concurrency": "bad"}), 4)

    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_one_observation_failure_is_partial_without_cancelling_other_series(
        self, query_latest, make_request
    ):
        series = [
            {"id": "A", "frequency": "daily"},
            {"id": "B", "frequency": "daily"},
            {"id": "C", "frequency": "daily"},
        ]
        query_latest.side_effect = self._query_with_metadata(
            {entry["id"]: self._metadata_row(entry["id"]) for entry in series}
        )
        called = []

        def request(*, params, **kwargs):
            series_id = params["series_id"]
            called.append(series_id)
            if series_id == "B":
                raise RuntimeError("safe failure")
            return self._response(
                {"observations": [{"date": "2025-01-01", "value": "1"}]}
            )

        make_request.side_effect = request

        result = FredCollector().collect(
            self._config(series=series, max_concurrency=2), "cid"
        )

        self.assertCountEqual(called, ["A", "B", "C"])
        self.assertEqual([record["series_id"] for record in result.records], ["A", "C"])
        self.assertTrue(result.partial_failure)
        self.assertEqual(result.errors[0]["series_id"], "B")

    @patch("collectors.fred.make_request", side_effect=RuntimeError("down"))
    @patch("collectors.fred.query_latest")
    def test_all_observation_failures_are_truthfully_all_failed(
        self, query_latest, make_request
    ):
        series = [{"id": "A", "frequency": "daily"}, {"id": "B", "frequency": "daily"}]
        query_latest.side_effect = self._query_with_metadata(
            {entry["id"]: self._metadata_row(entry["id"]) for entry in series}
        )

        result = FredCollector().collect(
            self._config(series=series, max_concurrency=2), "cid"
        )

        self.assertTrue(result.all_failed)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(make_request.call_count, 2)

    @patch("collectors.fred.make_request")
    @patch("collectors.fred.query_latest")
    def test_collection_propagates_real_substage_metrics(
        self, query_latest, make_request
    ):
        query_latest.side_effect = self._query_with_metadata({"GDP": self._metadata_row()})
        make_request.return_value = self._response(
            {"observations": [{"date": "2025-01-01", "value": "1"}]}
        )

        result = FredCollector().collect(self._config(max_concurrency=1), "cid")

        self.assertEqual(
            set(result.metrics),
            {
                "metadata_cache_duration_ms",
                "observation_fetch_duration_ms",
                "parse_normalize_duration_ms",
            },
        )
        self.assertTrue(all(value >= 0 for value in result.metrics.values()))


class FredOrchestratorMetricTests(unittest.TestCase):
    def test_central_db_write_duration_is_measured_and_propagated(self):
        import orchestrator as runtime
        from collectors.base import CollectionResult
        from db import WriteResult

        collector = MagicMock()
        collector.collect.return_value = CollectionResult(
            records=[{"series_id": "GDP"}],
            total_series=1,
            successful_series=1,
            metrics={
                "metadata_cache_duration_ms": 2,
                "observation_fetch_duration_ms": 3,
                "parse_normalize_duration_ms": 4,
            },
        )
        collector.get_target_table.return_value = "macro_series"
        collector.get_conflict_columns.return_value = ["series_id", "observed_at"]

        with (
            patch.object(runtime, "get_collector", return_value=collector),
            patch.object(
                runtime,
                "upsert_records",
                return_value=WriteResult(1, 1, 0, ()),
            ),
            patch.object(runtime, "_write_collection_log"),
            patch.object(runtime, "_estimate_api_calls", return_value=1),
            patch.object(runtime.time, "monotonic", side_effect=[1.0, 1.1, 1.14, 1.2]),
        ):
            result = runtime._run_collector_impl(
                "fred", {"collectors": {"fred": {}}}, "cid", manage_lifecycle=False
            )

        self.assertEqual(result["metrics"]["db_write_duration_ms"], 40)
        self.assertEqual(result["metrics"]["metadata_cache_duration_ms"], 2)


if __name__ == "__main__":
    unittest.main()
