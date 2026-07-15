import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DASHBOARD_USER", "internal-user")
os.environ.setdefault("DASHBOARD_PASSWORD", "internal-pass")


class CycleModeEndpointTests(unittest.TestCase):
    def setUp(self):
        import main
        from fastapi.testclient import TestClient

        self.main = main
        self.client = TestClient(main.app)
        main._cycle_correlation_id = None

    def test_invalid_mode_values_and_types_are_rejected_before_config_or_acceptance(self):
        with patch("main._get_config") as get_config, patch("main.accept_run") as accept:
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

    def test_invalid_body_and_confirmation_types_are_rejected_before_config_or_acceptance(self):
        with patch("main._get_config") as get_config, patch("main.accept_run") as accept:
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
        background = Mock()
        with patch(
            "main._accept_http_run", return_value=datetime.now(timezone.utc)
        ) as accept:
            result = self.main.trigger_cycle(background, body=None)

        self.assertIn("job_id", result)
        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "refresh", "budget_confirmed": False},
        )

    def test_force_full_body_confirmation_without_auth_cannot_bypass(self):
        with patch("main.accept_run") as accept:
            response = self.client.post(
                "/run_cycle",
                json={"mode": "force_full", "budget_confirmed": True},
            )

        self.assertEqual(response.status_code, 401)
        accept.assert_not_called()

    def test_force_full_requires_confirmation_even_with_valid_auth(self):
        with patch("main.accept_run") as accept:
            response = self.client.post(
                "/run_cycle",
                json={"mode": "force_full", "budget_confirmed": False},
                auth=("internal-user", "internal-pass"),
            )

        self.assertEqual(response.status_code, 422)
        accept.assert_not_called()

    def test_force_full_valid_auth_mints_trusted_cycle_only_context(self):
        background = Mock()
        accepted_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
        with patch("main._get_config", return_value={}), patch(
            "main.accept_run", return_value=accepted_at
        ) as accept:
            result = self.main.trigger_cycle(
                background,
                body={"mode": "force_full", "budget_confirmed": True},
                credentials=Mock(username="internal-user", password="internal-pass"),
            )

        summary = accept.call_args.kwargs["request_summary"]
        self.assertEqual(summary, {"mode": "force_full", "budget_confirmed": True})
        task_args = background.add_task.call_args.args
        self.assertEqual(task_args[:3], (self.main._run_cycle_task, result["job_id"], "force_full"))
        self.assertTrue(task_args[3].trusted_manual_force)
        self.assertNotIn("internal-pass", json.dumps(summary))

    def test_refresh_acceptance_stores_mode_and_enqueues_automatic_context(self):
        background = Mock()
        with patch("main._get_config", return_value={}), patch(
            "main.accept_run", return_value=datetime.now(timezone.utc)
        ) as accept:
            self.main.trigger_cycle(background, body={})

        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "refresh", "budget_confirmed": False},
        )
        self.assertEqual(background.add_task.call_args.args[2], "refresh")
        self.assertIsNone(background.add_task.call_args.args[3])


class CollectorDueTests(unittest.TestCase):
    def test_never_successful_collector_is_due(self):
        import orchestrator

        with patch.object(orchestrator, "_last_successful_collection", return_value=None):
            self.assertTrue(
                orchestrator._collector_is_due(
                    "fred",
                    {"collectors": {"fred": {"schedule": "0 6 * * 1-5"}}},
                    now=datetime(2026, 7, 15, 5, tzinfo=timezone.utc),
                )
            )

    def test_named_and_posix_weekday_schedules_share_next_fire_semantics(self):
        import orchestrator

        last = datetime(2026, 7, 13, 6, 1, tzinfo=timezone.utc)  # Monday
        now = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)  # Tuesday
        for schedule in ("0 6 * * mon-fri", "0 6 * * 1-5"):
            with self.subTest(schedule=schedule), patch.object(
                orchestrator, "_last_successful_collection", return_value=last
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

        last = datetime(2026, 7, 14, 6, 1, tzinfo=timezone.utc)
        with patch.object(orchestrator, "_last_successful_collection", return_value=last):
            self.assertFalse(
                orchestrator._collector_is_due(
                    "fred",
                    {"collectors": {"fred": {"schedule": "0 6 * * *"}}},
                    now=datetime(2026, 7, 15, 5, 59, tzinfo=timezone.utc),
                )
            )

    def test_lookup_or_schedule_failure_fails_safe_due(self):
        import orchestrator

        for failure in (RuntimeError("db"),):
            with patch.object(
                orchestrator, "_last_successful_collection", side_effect=failure
            ):
                self.assertTrue(orchestrator._collector_is_due("fred", {}, now=datetime.now(timezone.utc)))
        with patch.object(
            orchestrator,
            "_last_successful_collection",
            return_value=datetime.now(timezone.utc),
        ):
            self.assertTrue(
                orchestrator._collector_is_due(
                    "fred",
                    {"collectors": {"fred": {"schedule": "not cron"}}},
                    now=datetime.now(timezone.utc),
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
        with patch.object(
            orchestrator, "get_all_collectors", return_value={"due": Mock(), "stale": Mock()}
        ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
            orchestrator, "_collector_is_due", side_effect=lambda source, *_a, **_k: source == "due"
        ), patch.object(
            orchestrator, "_last_successful_collection", return_value=datetime.now(timezone.utc)
        ), patch.object(
            orchestrator, "run_collector", return_value={"collector": "due", "status": "success"}
        ) as collect, patch.object(
            orchestrator,
            "_resolve_and_run_processors",
            side_effect=lambda **kwargs: availability_seen.update(kwargs) or {},
        ), patch.object(orchestrator, "update_run_progress"):
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
        with patch.object(orchestrator, "get_all_collectors", return_value={"due": Mock()}), patch.object(
            orchestrator, "get_all_processors", return_value={}
        ), patch.object(orchestrator, "_collector_is_due", return_value=True), patch.object(
            orchestrator, "_last_successful_collection", return_value=datetime.now(timezone.utc)
        ), patch.object(
            orchestrator, "run_collector", return_value={"collector": "due", "status": "failed"}
        ), patch.object(
            orchestrator,
            "_resolve_and_run_processors",
            side_effect=lambda **kwargs: seen.update(kwargs) or {},
        ), patch.object(orchestrator, "update_run_progress"):
            orchestrator._run_full_cycle_impl(
                {"collectors": {"due": {"enabled": True}}, "processors": {}},
                "cycle",
                manage_lifecycle=False,
                mode="refresh",
            )

        self.assertEqual(seen["successful_collectors"], set())

    def test_analyze_runs_no_collectors_or_history_lookups_and_bypasses_collector_gate(self):
        import orchestrator

        seen = {}
        with patch.object(
            orchestrator, "get_all_collectors", return_value={"old": Mock(), "empty": Mock()}
        ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
            orchestrator, "_last_successful_collection"
        ) as history, patch.object(orchestrator, "run_collector") as collect, patch.object(
            orchestrator,
            "_resolve_and_run_processors",
            side_effect=lambda **kwargs: seen.update(kwargs) or {},
        ), patch.object(orchestrator, "update_run_progress"):
            result = orchestrator._run_full_cycle_impl(
                {
                    "collectors": {"old": {"enabled": True}, "empty": {"enabled": True}},
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
        self.assertEqual(result["collectors"]["empty"]["reason"], "analyze_mode_no_collection")
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

        with patch.object(
            orchestrator,
            "get_all_processors",
            return_value={
                "macro_regime": macro,
                "event_impact": event,
                "briefing": briefing,
            },
        ), patch.object(
            orchestrator,
            "run_processor",
            side_effect=[
                {"processor": "macro_regime", "status": "success"},
                {"processor": "event_impact", "status": "success"},
                {"processor": "briefing", "status": "success"},
            ],
        ) as run:
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
        with patch.object(
            orchestrator, "get_all_collectors", return_value={"a": Mock(), "b": Mock()}
        ), patch.object(orchestrator, "get_all_processors", return_value={}), patch.object(
            orchestrator,
            "run_collector",
            side_effect=lambda source, **_kwargs: {"collector": source, "status": "success"},
        ) as collect, patch.object(
            orchestrator,
            "_resolve_and_run_processors",
            side_effect=lambda **kwargs: seen.update(kwargs) or {},
        ), patch.object(orchestrator, "update_run_progress"):
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
            with self.subTest(force=force), patch.object(
                orchestrator, "get_all_processors", return_value={"macro_regime": processor}
            ), patch.object(
                orchestrator,
                "run_processor",
                return_value={"processor": "macro_regime", "status": "success"},
            ) as run:
                orchestrator._resolve_and_run_processors(
                    config, "cycle", set(), force=force
                )
            self.assertEqual(run.call_args.kwargs["force"], force)

    def test_public_cycle_forwards_requested_mode_to_cycle_impl(self):
        import orchestrator

        with patch.object(orchestrator, "advisory_lock") as lock, patch.object(
            orchestrator, "_run_full_cycle_impl", return_value={"status": "success"}
        ) as run:
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
        background = Mock()
        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry", return_value=previous
        ), patch("main.accept_run", return_value=datetime.now(timezone.utc)) as accept:
            main.retry_abandoned_run(
                "11111111-1111-4111-8111-111111111111", background
            )

        self.assertEqual(background.add_task.call_args.args[2], "force_full")
        self.assertIsNone(background.add_task.call_args.args[3])
        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "force_full", "budget_confirmed": False, "retry": True},
        )

    def test_retry_malformed_mode_fails_safe_to_refresh_without_budget_context(self):
        import main

        previous = {
            "status": "abandoned",
            "run_kind": "cycle",
            "requested_component": None,
            "summary": {"mode": ["force_full"], "budget_confirmed": True},
        }
        background = Mock()
        with patch("main._get_config", return_value={}), patch(
            "main.get_run_for_retry", return_value=previous
        ), patch("main.accept_run", return_value=datetime.now(timezone.utc)) as accept:
            main.retry_abandoned_run(
                "11111111-1111-4111-8111-111111111111", background
            )

        self.assertEqual(background.add_task.call_args.args[2:], ("refresh", None))
        self.assertEqual(
            accept.call_args.kwargs["request_summary"],
            {"mode": "refresh", "budget_confirmed": False, "retry": True},
        )


if __name__ == "__main__":
    unittest.main()
