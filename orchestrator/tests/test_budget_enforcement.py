import sys
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import (
    _authorize_claimed_run_budget,
    _consume_budget_override,
    run_processor,
)

FUTURE = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
PAST = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()


def _override(**overrides):
    override = {
        "requested": True,
        "reason": "manual review",
        "requested_by": "authenticated_api_user@testclient",
        "requested_at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        "expires_at": FUTURE,
        "scope": "one_run",
        "run_kind": "processor",
        "requested_component": "briefing",
    }
    override.update(overrides)
    return override


def _row(summary, run_kind="processor", requested_component="briefing"):
    return SimpleNamespace(
        _mapping={
            "summary": summary,
            "run_kind": run_kind,
            "requested_component": requested_component,
        }
    )


def _authorize_claimed_run_budget_context():
    from budgets import (
        mint_trusted_manual_authorization,
        trusted_manual_budget_context,
    )

    return trusted_manual_budget_context(
        force=True,
        manual_authorized=True,
        authorization=mint_trusted_manual_authorization(),
    )


class BudgetEnforcementTests(unittest.TestCase):
    @patch("orchestrator.get_session")
    def test_override_consumption_is_recorded_in_existing_run_summary(
        self,
        get_session,
    ):
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": _override()}
        )
        get_session.return_value.__enter__.return_value = session

        override = _consume_budget_override("run-id", {})

        self.assertEqual(override["consumed_by"], "run-id")
        update_params = session.execute.call_args_list[1].args[1]
        self.assertIn('"consumed_at"', update_params["summary"])
        self.assertIn('"consumed_by": "run-id"', update_params["summary"])

    @patch("orchestrator.get_session")
    def test_consumed_cycle_override_remains_valid_for_same_run(self, get_session):
        override = _override(
            consumed_at="2026-06-18T12:00:00+00:00",
            consumed_by="run-id",
        )
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": override}
        )
        get_session.return_value.__enter__.return_value = session

        result = _consume_budget_override("run-id", {})

        self.assertEqual(result, override)
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_already_consumed_by_another_run_is_not_reusable(self, get_session):
        override = _override(
            consumed_at="2026-06-18T12:00:00+00:00",
            consumed_by="other-run",
        )
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": override}
        )
        get_session.return_value.__enter__.return_value = session

        result = _consume_budget_override("run-id", {})

        self.assertIsNone(result)
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_expired_override_is_not_consumed(self, get_session):
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": _override(expires_at=PAST)}
        )
        get_session.return_value.__enter__.return_value = session

        self.assertIsNone(_consume_budget_override("run-id", {}))
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_missing_or_malformed_expiry_fails_closed(self, get_session):
        for expires_at in (None, "not-a-date", 12345):
            with self.subTest(expires_at=expires_at):
                session = Mock()
                session.execute.return_value.fetchone.return_value = _row(
                    {"budget_override": _override(expires_at=expires_at)}
                )
                get_session.return_value.__enter__.return_value = session
                self.assertIsNone(_consume_budget_override("run-id", {}))
                self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_wrong_run_kind_is_not_consumed(self, get_session):
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": _override(run_kind="cycle")}, run_kind="processor"
        )
        get_session.return_value.__enter__.return_value = session

        self.assertIsNone(_consume_budget_override("run-id", {}))
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_wrong_component_is_not_consumed(self, get_session):
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": _override(requested_component="briefing")},
            requested_component="macro_regime",
        )
        get_session.return_value.__enter__.return_value = session

        self.assertIsNone(_consume_budget_override("run-id", {}))
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_missing_requestor_is_not_consumed(self, get_session):
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row(
            {"budget_override": _override(requested_by=None)}
        )
        get_session.return_value.__enter__.return_value = session

        self.assertIsNone(_consume_budget_override("run-id", {}))
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_unknown_correlation_has_no_override(self, get_session):
        session = Mock()
        session.execute.return_value.fetchone.return_value = None
        get_session.return_value.__enter__.return_value = session

        self.assertIsNone(_consume_budget_override("other-run", {}))
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_malformed_summary_fails_closed(self, get_session):
        session = Mock()
        session.execute.return_value.fetchone.return_value = _row("not-json{")
        get_session.return_value.__enter__.return_value = session

        self.assertIsNone(_consume_budget_override("run-id", {}))
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.get_session")
    def test_consume_failure_fails_closed_without_trusted_context(self, get_session):
        get_session.side_effect = RuntimeError("db unavailable")

        context = _authorize_claimed_run_budget(
            {}, "run-id", run_kind="processor", component="briefing"
        )

        self.assertIsNone(context)

    @patch("orchestrator.finalize_run_safely", return_value=True)
    @patch("orchestrator._write_processing_log")
    @patch("orchestrator._persist_processor_result")
    @patch("orchestrator._authorize_claimed_run_budget", return_value=None)
    @patch("orchestrator.get_processor")
    @patch("orchestrator.advisory_lock", return_value=nullcontext())
    @patch("orchestrator.maintain_run_heartbeat", return_value=nullcontext())
    @patch("orchestrator.start_run", return_value=True)
    @patch("orchestrator.accept_run", return_value=datetime.now(UTC))
    def test_denied_processor_without_override_has_no_trusted_context(
        self,
        accept_run,
        start_run,
        heartbeat,
        advisory_lock,
        get_processor,
        authorize,
        persist_result,
        write_log,
        finalize_run_safely,
    ):
        processor = Mock()
        processor.process.return_value = {
            "opinions": [{"opinion_id": "opinion-1"}],
            "processing_log": {},
        }
        get_processor.return_value = processor

        result = run_processor("briefing", config={})

        self.assertEqual(result["status"], "success")
        authorize.assert_called_once()
        self.assertIsNone(processor.process.call_args.kwargs["budget_context"])

    @patch("orchestrator.finalize_run_safely", return_value=True)
    @patch("orchestrator._write_processing_log")
    @patch("orchestrator._persist_processor_result")
    @patch("orchestrator._authorize_claimed_run_budget")
    @patch("orchestrator.get_processor")
    @patch("orchestrator.advisory_lock", return_value=nullcontext())
    @patch("orchestrator.maintain_run_heartbeat", return_value=nullcontext())
    @patch("orchestrator.start_run", return_value=True)
    @patch("orchestrator.accept_run", return_value=datetime.now(UTC))
    def test_one_run_override_mints_trusted_context_propagated_into_paid_call(
        self,
        accept_run,
        start_run,
        heartbeat,
        advisory_lock,
        get_processor,
        authorize,
        persist_result,
        write_log,
        finalize_run_safely,
    ):
        authorize.return_value = _authorize_claimed_run_budget_context()
        processor = Mock()
        processor.process.return_value = {
            "opinions": [{"opinion_id": "opinion-1"}],
            "processing_log": {},
        }
        get_processor.return_value = processor

        result = run_processor("briefing", config={})

        self.assertEqual(result["status"], "success")
        processor.process.assert_called_once()
        context = processor.process.call_args.kwargs["budget_context"]
        self.assertIsNotNone(context)
        self.assertTrue(context.trusted_manual_force)


if __name__ == "__main__":
    unittest.main()
