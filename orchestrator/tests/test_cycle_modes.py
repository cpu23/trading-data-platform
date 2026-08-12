import base64
import json
import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DASHBOARD_USER", "internal-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "internal-pass")
INTERNAL_AUTH = {
    "Authorization": "Basic "
    + base64.b64encode(b"internal-user:internal-pass").decode()
}


class CycleModeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.auth_patcher = patch.dict(
            os.environ,
            {
                "DASHBOARD_USER": "internal-user",
                "DASHBOARD_PASSWORD": "internal-pass",
            },
        )
        self.auth_patcher.start()
        self.addCleanup(self.auth_patcher.stop)
        from fastapi.testclient import TestClient

        import main

        self.main = main
        self.client = TestClient(main.app, headers=INTERNAL_AUTH)

    def test_every_mutation_family_requires_internal_basic_auth(self):
        from fastapi.testclient import TestClient

        unauthenticated = TestClient(self.main.app)
        paths = (
            "/run_cycle",
            "/run_collector/not-real",
            "/run_processor/not-real",
            "/run_news/not-real",
            "/runs/00000000-0000-0000-0000-000000000000/retry",
        )
        for path in paths:
            with self.subTest(path=path):
                response = unauthenticated.post(path, json={})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("www-authenticate"), "Basic")

    def test_invalid_mode_values_and_types_are_rejected_before_config_or_acceptance(
        self,
    ):
        with (
            patch("main._get_config") as get_config,
            patch("main.accept_and_enqueue_operation") as accept,
        ):
            for mode in (
                "invalid",
                ["refresh"],
                {"mode": "refresh"},
                1,
                True,
                None,
            ):
                with self.subTest(mode=mode):
                    response = self.client.post("/run_cycle", json={"mode": mode})
                    self.assertEqual(response.status_code, 422)

        get_config.assert_not_called()
        accept.assert_not_called()

    def test_invalid_body_and_confirmation_types_are_rejected_before_config_or_acceptance(
        self,
    ):
        with (
            patch("main._get_config") as get_config,
            patch("main.accept_and_enqueue_operation") as accept,
        ):
            for body in (
                ["refresh"],
                "refresh",
                1,
                True,
                {"budget_confirmed": "true"},
                {"budget_confirmed": 1},
                {"budget_confirmed": [True]},
            ):
                with self.subTest(body=body):
                    response = self.client.post("/run_cycle", json=body)
                    self.assertEqual(response.status_code, 422)

        get_config.assert_not_called()
        accept.assert_not_called()

    def test_absent_or_null_body_defaults_to_refresh(self):
        enqueued = Mock()
        enqueued.inserted = True
        enqueued.suppressed = False
        with (
            patch("main._get_config", return_value={}),
            patch(
                "main.accept_and_enqueue_operation",
                return_value=(datetime.now(UTC), enqueued),
            ) as accept,
        ):
            result = self.main.trigger_cycle(body=None)

        self.assertIn("job_id", result)
        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "refresh", "budget_confirmed": False},
        )

    def test_force_full_body_confirmation_without_auth_cannot_bypass(self):
        from fastapi.testclient import TestClient

        with patch("main.accept_and_enqueue_operation") as accept:
            response = TestClient(self.main.app).post(
                "/run_cycle",
                json={"mode": "force_full", "budget_confirmed": True},
            )

        self.assertEqual(response.status_code, 401)
        accept.assert_not_called()

    def test_force_full_requires_confirmation_even_with_valid_auth(self):
        with patch("main.accept_and_enqueue_operation") as accept:
            response = self.client.post(
                "/run_cycle",
                json={"mode": "force_full", "budget_confirmed": False},
                auth=("internal-user", "internal-pass"),
            )

        self.assertEqual(response.status_code, 422)
        accept.assert_not_called()

    def test_force_full_valid_auth_records_confirmation_without_minting_context(self):
        accepted_at = datetime(2026, 7, 15, tzinfo=UTC)
        enqueued = Mock()
        enqueued.inserted = True
        enqueued.suppressed = False
        with (
            patch("main._get_config", return_value={}),
            patch(
                "main.accept_and_enqueue_operation",
                return_value=(accepted_at, enqueued),
            ) as accept,
        ):
            result = self.main.trigger_cycle(
                body={"mode": "force_full", "budget_confirmed": True},
                credentials=Mock(username="internal-user", password="internal-pass"),
            )

        summary = accept.call_args.kwargs["request_summary"]
        self.assertEqual(summary, {"mode": "force_full", "budget_confirmed": True})
        self.assertEqual(accept.call_args.kwargs["payload"], {"mode": "force_full"})
        self.assertEqual(accept.call_args.kwargs["run_kind"], "cycle")
        self.assertEqual(result["job_id"], accept.call_args.kwargs["correlation_id"])
        self.assertNotIn("internal-pass", json.dumps(summary))
        # No budget context is minted at the HTTP boundary; the worker
        # re-authorizes the override at claim time.
        self.assertNotIn("budget_context", accept.call_args.kwargs)

    def test_refresh_acceptance_stores_mode_and_enqueues_automatic_context(self):
        enqueued = Mock()
        enqueued.inserted = True
        enqueued.suppressed = False
        with (
            patch("main._get_config", return_value={}),
            patch(
                "main.accept_and_enqueue_operation",
                return_value=(datetime.now(UTC), enqueued),
            ) as accept,
        ):
            self.main.trigger_cycle(body={})

        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "refresh", "budget_confirmed": False},
        )
        self.assertEqual(accept.call_args.kwargs["payload"], {"mode": "refresh"})


class StrictBodyValidationTests(unittest.TestCase):
    """Durable acceptance bodies are validated before any DB work."""

    def setUp(self):
        self.auth_patcher = patch.dict(
            os.environ,
            {
                "DASHBOARD_USER": "internal-user",
                "DASHBOARD_PASSWORD": "internal-pass",
            },
        )
        self.auth_patcher.start()
        self.addCleanup(self.auth_patcher.stop)
        from fastapi.testclient import TestClient

        import main

        self.client = TestClient(main.app, headers=INTERNAL_AUTH)

    def _assert_rejected_without_acceptance(self, path, payloads):
        from unittest.mock import Mock

        with patch("main._get_config", return_value={}), patch(
            "main.accept_and_enqueue_operation", return_value=(datetime.now(UTC), Mock())
        ) as accept, patch(
            "collectors.get_all_collectors", return_value={"fred": Mock()}
        ), patch(
            "processors.get_all_processors", return_value={"briefing": Mock()}
        ):
            for payload in payloads:
                with self.subTest(payload=payload):
                    response = self.client.post(path, json=payload)
                    self.assertEqual(response.status_code, 422, response.text)
            accept.assert_not_called()

    def test_cycle_rejects_unknown_nested_oversize_and_wrong_types(self):
        self._assert_rejected_without_acceptance(
            "/run_cycle",
            [
                {"unknown_field": True},
                {"mode": {"nested": "refresh"}},
                {"idempotency_key": "x" * 200},
                {"idempotency_key": 123},
                {"idempotency_key": "   "},
                {"budget_confirmed": "true"},
                {"budget_confirmed": 1},
                {"budget_confirmed": [True]},
                {"correlation_id": "not-a-uuid"},
                {"correlation_id": {"nested": "uuid"}},
                {"mode": 1},
                {"mode": ["refresh"]},
            ],
        )

    def test_run_requests_reject_unknown_nested_oversize_and_wrong_types(self):
        for path in ("/run_collector/fred", "/run_news/reuters", "/run_processor/briefing"):
            with self.subTest(path=path):
                self._assert_rejected_without_acceptance(
                    path,
                    [
                        {"unknown_field": True},
                        {"idempotency_key": {"nested": "key"}},
                        {"idempotency_key": "x" * 200},
                        {"idempotency_key": 3.5},
                        {"idempotency_key": ""},
                        {"correlation_id": "not-a-uuid"},
                        {"mode": "force_full"},
                    ],
                )

    def test_filings_reject_unknown_and_non_bool_auto_analyze(self):
        self._assert_rejected_without_acceptance(
            "/investment/filings/collect",
            [
                {"unknown_field": True},
                {"auto_analyze": "true"},
                {"auto_analyze": 1},
                {"auto_analyze": [True]},
                {"idempotency_key": "x" * 200},
            ],
        )


    def test_health_never_leaks_error_text_or_quality_exceptions(self):

        secret = "api_key=sk-secret-token-12345"
        run = {
            "collector": "fred",
            "status": "failed",
            "started_at": None,
            "records_fetched": 0,
            "records_written": 0,
            "error_message": secret,
        }
        with (
            patch("main._get_config", return_value={}),
            patch("main.check_connection", return_value=True),
            patch("main.get_last_collection_runs", return_value=[run]),
            patch("main._role_readiness", return_value=([], True)),
            patch("main.fresh_role_heartbeats", return_value=[]),
            patch(
                "main._health_quality_snapshot",
                side_effect=RuntimeError(secret),
            ),
            patch("main.logger") as logger,
            patch("main.evaluate_quality", return_value="degraded"),
            patch("main.required_quality_checks", return_value=set()),
            patch("main.readiness_critical_checks", return_value=set()),
        ):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("sk-secret-token", response.text)
        # Bounded class only on both status surfaces.
        self.assertIn("error", response.text)
        for call in logger.error.call_args_list:
            self.assertNotIn(secret, str(call))
        logged_error_type = logger.error.call_args.kwargs.get("error_type")
        self.assertEqual(logged_error_type, "RuntimeError")

    def test_health_collector_error_exposes_class_not_text(self):

        secret = "api_key=sk-another-secret"
        run = {
            "collector": "fred",
            "status": "failed",
            "started_at": None,
            "records_fetched": 0,
            "records_written": 0,
            "error_message": secret,
        }
        with (
            patch("main._get_config", return_value={}),
            patch("main.check_connection", return_value=True),
            patch("main.get_last_collection_runs", return_value=[run]),
            patch("main._role_readiness", return_value=([], True)),
            patch("main.fresh_role_heartbeats", return_value=[]),
            patch(
                "main._health_quality_snapshot",
                return_value={"quality_runner": {"healthy": True}},
            ),
            patch("main.evaluate_quality", return_value="healthy"),
            patch("main.required_quality_checks", return_value=set()),
            patch("main.readiness_critical_checks", return_value=set()),
        ):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("sk-another-secret", response.text)
        body = response.json()
        self.assertEqual(
            body["collectors"]["fred"]["error_message"], "Error"
        )


class CollectorDueTests(unittest.TestCase):
    def test_never_successful_collector_is_due(self):
        import orchestrator

        with patch.object(
            orchestrator, "_last_successful_collection", return_value=None
        ):
            self.assertTrue(
                orchestrator._collector_is_due(
                    "fred",
                    {"collectors": {"fred": {"schedule": "0 6 * * 1-5"}}},
                    now=datetime(2026, 7, 15, 5, tzinfo=UTC),
                )
            )

    def test_named_and_posix_weekday_schedules_share_next_fire_semantics(self):
        import orchestrator

        last = datetime(2026, 7, 13, 6, 1, tzinfo=UTC)  # Monday
        now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)  # Tuesday
        for schedule in ("0 6 * * mon-fri", "0 6 * * 1-5"):
            with (
                self.subTest(schedule=schedule),
                patch.object(
                    orchestrator, "_last_successful_collection", return_value=last
                ),
            ):
                self.assertTrue(
                    orchestrator._collector_is_due(
                        "fred",
                        {"collectors": {"fred": {"schedule": schedule}}},
                        now=now,
                    )
                )

    def test_not_due_until_next_scheduled_fire(self):
        import orchestrator

        last = datetime(2026, 7, 14, 6, 1, tzinfo=UTC)
        with patch.object(
            orchestrator, "_last_successful_collection", return_value=last
        ):
            self.assertFalse(
                orchestrator._collector_is_due(
                    "fred",
                    {"collectors": {"fred": {"schedule": "0 6 * * *"}}},
                    now=datetime(2026, 7, 15, 5, 59, tzinfo=UTC),
                )
            )

    def test_lookup_or_schedule_failure_fails_safe_due(self):
        import orchestrator

        for failure in (RuntimeError("db"),):
            with patch.object(
                orchestrator, "_last_successful_collection", side_effect=failure
            ):
                self.assertTrue(
                    orchestrator._collector_is_due("fred", {}, now=datetime.now(UTC))
                )
        with patch.object(
            orchestrator,
            "_last_successful_collection",
            return_value=datetime.now(UTC),
        ):
            self.assertTrue(
                orchestrator._collector_is_due(
                    "fred",
                    {"collectors": {"fred": {"schedule": "not cron"}}},
                    now=datetime.now(UTC),
                )
            )


class CycleExecutionModeTests(unittest.TestCase):
    def _config(self):
        return {
            "collectors": {
                "due": {"enabled": True, "schedule": "0 * * * *"},
                "stale": {"enabled": True, "schedule": "0 * * * *"},
            },
            "processors": {},
        }

    def test_refresh_runs_only_due_and_stale_success_satisfies_dependencies(self):
        import orchestrator

        availability_seen = {}
        with (
            patch.object(
                orchestrator,
                "get_all_collectors",
                return_value={"due": Mock(), "stale": Mock()},
            ),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(
                orchestrator,
                "_collector_is_due",
                side_effect=lambda source, *_a, **_k: source == "due",
            ),
            patch.object(
                orchestrator,
                "_last_successful_collection",
                return_value=datetime.now(UTC),
            ),
            patch.object(
                orchestrator,
                "run_collector",
                return_value={"collector": "due", "status": "success"},
            ) as collect,
            patch.object(
                orchestrator,
                "_resolve_and_run_processors",
                side_effect=lambda **kwargs: availability_seen.update(kwargs) or {},
            ),
            patch.object(orchestrator, "update_run_progress"),
        ):
            result = orchestrator._run_full_cycle_impl(
                self._config(), "cycle", manage_lifecycle=False, mode="refresh"
            )

        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0], "due")
        self.assertEqual(result["collectors"]["stale"]["reason"], "not_due")
        self.assertEqual(result["collectors"]["stale"]["mode"], "refresh")
        self.assertTrue(result["collectors"]["stale"]["no_change"])
        self.assertEqual(availability_seen["successful_collectors"], {"due", "stale"})

    def test_due_failure_is_not_masked_by_historical_success(self):
        import orchestrator

        seen = {}
        with (
            patch.object(
                orchestrator, "get_all_collectors", return_value={"due": Mock()}
            ),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(orchestrator, "_collector_is_due", return_value=True),
            patch.object(
                orchestrator,
                "_last_successful_collection",
                return_value=datetime.now(UTC),
            ),
            patch.object(
                orchestrator,
                "run_collector",
                return_value={"collector": "due", "status": "failed"},
            ),
            patch.object(
                orchestrator,
                "_resolve_and_run_processors",
                side_effect=lambda **kwargs: seen.update(kwargs) or {},
            ),
            patch.object(orchestrator, "update_run_progress"),
        ):
            orchestrator._run_full_cycle_impl(
                {"collectors": {"due": {"enabled": True}}, "processors": {}},
                "cycle",
                manage_lifecycle=False,
                mode="refresh",
            )

        self.assertEqual(seen["successful_collectors"], set())

    def test_analyze_runs_no_collectors_or_history_lookups_and_bypasses_collector_gate(
        self,
    ):
        import orchestrator

        seen = {}
        with (
            patch.object(
                orchestrator,
                "get_all_collectors",
                return_value={"old": Mock(), "empty": Mock()},
            ),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(orchestrator, "_last_successful_collection") as history,
            patch.object(orchestrator, "run_collector") as collect,
            patch.object(
                orchestrator,
                "_resolve_and_run_processors",
                side_effect=lambda **kwargs: seen.update(kwargs) or {},
            ),
            patch.object(orchestrator, "update_run_progress"),
        ):
            result = orchestrator._run_full_cycle_impl(
                {
                    "collectors": {
                        "old": {"enabled": True},
                        "empty": {"enabled": True},
                    },
                    "processors": {},
                },
                "cycle",
                manage_lifecycle=False,
                mode="analyze",
            )

        collect.assert_not_called()
        history.assert_not_called()
        self.assertEqual(seen["successful_collectors"], set())
        self.assertTrue(seen["analyze_existing_data"])
        self.assertEqual(result["collectors"]["empty"]["status"], "skipped")
        self.assertEqual(
            result["collectors"]["empty"]["reason"], "analyze_mode_no_collection"
        )
        self.assertEqual(result["collectors"]["empty"]["mode"], "analyze")
        self.assertTrue(result["collectors"]["empty"]["no_change"])

    def test_analyze_attempts_raw_data_processors_then_preserves_processor_order(self):
        import orchestrator

        macro = Mock()
        macro.get_depends_on.return_value = ["fred"]
        event = Mock()
        event.get_depends_on.return_value = ["forex_factory"]
        briefing = Mock()
        briefing.get_depends_on.return_value = ["macro_regime"]
        config = {
            "processors": {
                "macro_regime": {"enabled": True},
                "event_impact": {"enabled": True},
                "briefing": {"enabled": True},
            }
        }

        with (
            patch.object(
                orchestrator,
                "get_all_processors",
                return_value={
                    "macro_regime": macro,
                    "event_impact": event,
                    "briefing": briefing,
                },
            ),
            patch.object(
                orchestrator,
                "run_processor",
                side_effect=[
                    {"processor": "macro_regime", "status": "success"},
                    {"processor": "event_impact", "status": "success"},
                    {"processor": "briefing", "status": "success"},
                ],
            ) as run,
        ):
            results = orchestrator._resolve_and_run_processors(
                config,
                "cycle",
                set(),
                analyze_existing_data=True,
            )

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            ["macro_regime", "event_impact", "briefing"],
        )
        self.assertEqual(results["macro_regime"]["status"], "success")
        self.assertEqual(results["event_impact"]["status"], "success")
        self.assertEqual(results["briefing"]["status"], "success")

    def test_force_full_runs_all_collectors_and_forces_processors(self):
        import orchestrator

        seen = {}
        with (
            patch.object(
                orchestrator,
                "get_all_collectors",
                return_value={"a": Mock(), "b": Mock()},
            ),
            patch.object(orchestrator, "get_all_processors", return_value={}),
            patch.object(
                orchestrator,
                "run_collector",
                side_effect=lambda source, **_kwargs: {
                    "collector": source,
                    "status": "success",
                },
            ) as collect,
            patch.object(
                orchestrator,
                "_resolve_and_run_processors",
                side_effect=lambda **kwargs: seen.update(kwargs) or {},
            ),
            patch.object(orchestrator, "update_run_progress"),
        ):
            result = orchestrator._run_full_cycle_impl(
                self._config(), "cycle", manage_lifecycle=False, mode="force_full"
            )

        self.assertEqual(collect.call_count, 2)
        self.assertTrue(seen["force"])
        self.assertEqual(result["mode"], "force_full")
        self.assertTrue(result["forced"])

    def test_processor_force_flag_tracks_cycle_mode(self):
        import orchestrator

        processor = Mock()
        processor.get_depends_on.return_value = []
        config = {"processors": {"macro_regime": {"enabled": True}}}
        for force in (False, True):
            with (
                self.subTest(force=force),
                patch.object(
                    orchestrator,
                    "get_all_processors",
                    return_value={"macro_regime": processor},
                ),
                patch.object(
                    orchestrator,
                    "run_processor",
                    return_value={"processor": "macro_regime", "status": "success"},
                ) as run,
            ):
                orchestrator._resolve_and_run_processors(
                    config, "cycle", set(), force=force
                )
            self.assertEqual(run.call_args.kwargs["force"], force)

    def test_public_cycle_forwards_requested_mode_to_cycle_impl(self):
        import orchestrator

        with (
            patch.object(orchestrator, "advisory_lock") as lock,
            patch.object(
                orchestrator, "_run_full_cycle_impl", return_value={"status": "success"}
            ) as run,
        ):
            lock.return_value.__enter__.return_value = None
            lock.return_value.__exit__.return_value = None
            orchestrator.run_full_cycle(
                config={},
                correlation_id="cycle",
                manage_lifecycle=False,
                mode="analyze",
            )

        self.assertEqual(run.call_args.kwargs["mode"], "analyze")


class ForceFullRetryTests(unittest.TestCase):
    def test_retry_preserves_force_computation_but_drops_budget_bypass(self):
        import main

        previous = {
            "status": "abandoned",
            "run_kind": "cycle",
            "requested_component": None,
            "summary": {"mode": "force_full", "budget_confirmed": True},
        }
        enqueued = Mock()
        enqueued.inserted = True
        enqueued.suppressed = False
        with (
            patch("main._get_config", return_value={}),
            patch("main.get_run_for_retry", return_value=previous),
            patch(
                "main._accept_and_enqueue",
                return_value=(datetime.now(UTC), enqueued),
            ) as accept,
        ):
            main.retry_abandoned_run("11111111-1111-4111-8111-111111111111")

        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "force_full", "budget_confirmed": False, "retry": True},
        )
        self.assertEqual(
            accept.call_args.kwargs["payload"],
            {"mode": "force_full", "budget_confirmed": False},
        )

    def test_retry_malformed_mode_fails_safe_to_refresh_without_budget_context(self):
        import main

        previous = {
            "status": "abandoned",
            "run_kind": "cycle",
            "requested_component": None,
            "summary": {"mode": ["force_full"], "budget_confirmed": True},
        }
        enqueued = Mock()
        enqueued.inserted = True
        enqueued.suppressed = False
        with (
            patch("main._get_config", return_value={}),
            patch("main.get_run_for_retry", return_value=previous),
            patch(
                "main._accept_and_enqueue",
                return_value=(datetime.now(UTC), enqueued),
            ) as accept,
        ):
            main.retry_abandoned_run("11111111-1111-4111-8111-111111111111")

        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "refresh", "budget_confirmed": False, "retry": True},
        )
        self.assertEqual(
            accept.call_args.kwargs["payload"],
            {"mode": "refresh", "budget_confirmed": False},
        )


if __name__ == "__main__":
    unittest.main()
