import json
import sys
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processors.base import canonical_fingerprint

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
ATOM_ID = UUID("33333333-3333-4333-8333-333333333333")
PRIOR_ATOM_ID = UUID("44444444-4444-4444-8444-444444444444")
REGIME_OPINION_ID = UUID("55555555-5555-4555-8555-555555555555")

ATOM_CONFIG = {
    "analysis_atoms": {
        "enabled": True,
        "event_interpretation_hours": 48,
        "regime_hours": 168,
    }
}

ROUTING_CONFIG = {
    "market_state": {"enabled": True},
    "reaction_windows": {"max_event_age_minutes": 360},
    "macro_event_mappings": {},
    **ATOM_CONFIG,
}


class Result:
    def __init__(self, *, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commit = MagicMock()

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.results:
            raise AssertionError(f"unexpected SQL call: {statement}")
        return self.results.pop(0)


def atom(**overrides):
    value = {
        "subject_type": "econ_event",
        "subject_id": "event-a",
        "claim_type": "event_interpretation",
        "claim": "Event A interpretation.",
        "observation_text": "Scheduled: 2026-08-07T13:30:00Z. Consensus: 0.2%.",
        "interpretation_text": "In-line outcome is neutral.",
        "scenario_text": None,
        "unknowns": [],
        "affected_assets": ["EURUSD"],
        "time_horizon": "48h",
        "confidence": 0.6,
        "confidence_components": {"source": "llm_event_interpretation"},
        "valid_from": NOW,
        "expires_at": NOW + timedelta(hours=48),
        "carry_forward": False,
        "invalidation_conditions": [],
        "input_fingerprint": canonical_fingerprint({"case": "atom"}),
    }
    value.update(overrides)
    return value


def _session_factory(fake_session):
    """Return a get_session-style context manager yielding one fixed session."""

    @contextmanager
    def factory(_config=None):
        yield fake_session

    return factory


def _stage_mock(parsed: dict) -> MagicMock:
    stage = MagicMock()
    stage.call.return_value = {"content": json.dumps(parsed), "model": "test-model"}
    stage.policy.model = "test-model"
    stage.telemetry.tokens_input_total = 10
    stage.telemetry.tokens_output_total = 20
    stage.telemetry.cost_usd_total = 0.001
    stage.telemetry.as_dict.return_value = {}
    return stage


def _parsed_events() -> dict:
    return {
        "events": [
            {
                "event_name": "US CPI (MoM)",
                "scheduled_at": "2026-08-07T13:30:00Z",
                "consensus": "0.2%",
                "previous": "0.3%",
                "context": "Core inflation watch for the Fed.",
                "consensus_met_scenario": {
                    "direction": "neutral",
                    "volatility": "low",
                    "narrative": "In-line print keeps policy on hold.",
                },
                "upside_surprise_scenario": {
                    "direction": "bullish_usd",
                    "volatility": "high",
                    "narrative": "Hot print hardens the Fed.",
                },
                "downside_surprise_scenario": {
                    "direction": "bearish_usd",
                    "volatility": "high",
                    "narrative": "Soft print eases policy odds.",
                },
                "affected_instruments": [
                    {
                        "symbol": "EURUSD",
                        "sensitivity": "high",
                        "expected_reaction": "USD-sensitive two-way flow.",
                    }
                ],
                "market_implications": "Two-way USD risk into the print.",
            }
        ],
        "overall_volatility_outlook": "Elevated volatility around the release.",
        "catalyst_summary": "CPI is the dominant scheduled catalyst.",
        "risk_management_note": "Sizing discipline around the release window.",
    }


def _event_rows() -> list[dict]:
    return [
        {
            "event_id": "abc123event",
            "event_name": "US CPI (MoM)",
            "country": "US",
            "scheduled_at": NOW + timedelta(days=1),
            "impact_level": "high",
            "consensus": "0.2%",
            "previous": "0.3%",
            "actual": None,
        }
    ]


class EventImpactAtomTests(unittest.TestCase):
    def test_atom_publish_uses_caller_session_and_processor_still_succeeds(self):
        from processors.event_impact import EventImpactProcessor

        processor = EventImpactProcessor()
        session = MagicMock()
        stage = _stage_mock(_parsed_events())
        with (
            patch.object(
                EventImpactProcessor,
                "_fetch_upcoming_events",
                return_value=_event_rows(),
            ),
            patch.object(
                EventImpactProcessor, "_format_watchlist", return_value="EURUSD"
            ),
            patch.object(
                EventImpactProcessor,
                "_get_current_regime",
                return_value="Current macro regime: expansion (risk_on), high confidence.",
            ),
            patch.object(
                EventImpactProcessor,
                "_current_regime_opinion_id",
                return_value=str(REGIME_OPINION_ID),
            ),
            patch.object(EventImpactProcessor, "_build_prompt", return_value="prompt"),
            patch("processors.event_impact.LLMStage", return_value=stage),
            patch("processors.event_impact.get_session", _session_factory(session)),
            patch(
                "atoms.publish_atom",
                return_value={"status": "published", "atom_id": ATOM_ID, "evidence": 2},
            ) as publish,
        ):
            result = processor.process(ATOM_CONFIG, "corr-1")
        self.assertEqual(result["processing_log"]["status"], "success")
        self.assertIsNotNone(result["atoms"])
        self.assertEqual(len(result["atoms"]["published"]), 2)
        self.assertTrue(publish.called)
        for call in publish.call_args_list:
            self.assertIs(call.args[0], session)
        atom_payload = publish.call_args_list[0].args[1]
        self.assertEqual(atom_payload["subject_type"], "econ_event")
        self.assertEqual(atom_payload["claim_type"], "event_interpretation")
        self.assertEqual(atom_payload["time_horizon"], "48h")
        self.assertEqual(
            atom_payload["expires_at"] - atom_payload["valid_from"],
            timedelta(hours=48),
        )
        self.assertEqual(atom_payload["prompt_version"], "event_impact_v1")
        self.assertEqual(atom_payload["model_slug"], "test-model")
        self.assertRegex(atom_payload["input_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertIn("observation_text", atom_payload)
        self.assertIn("interpretation_text", atom_payload)
        evidence = publish.call_args_list[0].args[2]
        evidence_ids = [
            item["evidence_id"]
            for item in evidence
            if item["evidence_type"] == "econ_events"
        ]
        self.assertEqual(evidence_ids, ["abc123event"])
        self.assertIn(
            {
                "evidence_type": "opinion",
                "evidence_id": str(REGIME_OPINION_ID),
                "relationship": "context",
            },
            evidence,
        )

    def test_atom_failure_fails_soft_while_processor_succeeds(self):
        from processors.event_impact import EventImpactProcessor

        processor = EventImpactProcessor()
        session = MagicMock()
        stage = _stage_mock(_parsed_events())
        with (
            patch.object(
                EventImpactProcessor,
                "_fetch_upcoming_events",
                return_value=_event_rows(),
            ),
            patch.object(
                EventImpactProcessor, "_format_watchlist", return_value="EURUSD"
            ),
            patch.object(
                EventImpactProcessor,
                "_get_current_regime",
                return_value="Current macro regime: expansion (risk_on), high confidence.",
            ),
            patch.object(
                EventImpactProcessor,
                "_current_regime_opinion_id",
                return_value=None,
            ),
            patch.object(EventImpactProcessor, "_build_prompt", return_value="prompt"),
            patch("processors.event_impact.LLMStage", return_value=stage),
            patch("processors.event_impact.get_session", _session_factory(session)),
            patch(
                "atoms.publish_atom",
                side_effect=ValueError("atom evidence validation failed"),
            ),
        ):
            result = processor.process(ATOM_CONFIG, "corr-2")
        self.assertEqual(result["processing_log"]["status"], "success")
        self.assertIsNone(result["atoms"])
        self.assertEqual(result["opinion"]["opinion_type"], "event_impact")

    def test_unknown_evidence_ids_fail_publication_with_value_error(self):
        from atoms import publish_atom

        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "evidence validation failed"):
            publish_atom(
                session,
                atom(),
                [
                    {
                        "evidence_type": "econ_events",
                        "evidence_id": "missing-event",
                        "relationship": "context",
                    }
                ],
                now=NOW,
            )
        session.commit.assert_not_called()


class MacroRegimeAtomTests(unittest.TestCase):
    def test_supersedes_prior_regime_atom_only_when_classification_changed(self):
        from processors.macro_regime import MacroRegimeProcessor

        processor = MacroRegimeProcessor()
        session = MagicMock()
        prior = [
            {
                "id": PRIOR_ATOM_ID,
                "claim": "Regime: expansion (risk_on) — bullish, high confidence",
            }
        ]
        changed = {
            "regime": "expansion",
            "sub_regime": "risk_off",
            "direction": "bearish",
            "confidence": "high",
            "timeframe": "medium_term",
            "summary": "Risk appetite is deteriorating.",
        }
        with (
            patch("processors.macro_regime.get_session", _session_factory(session)),
            patch("atoms.current_atoms", return_value=prior) as current,
            patch(
                "atoms.publish_atom",
                return_value={"status": "published", "atom_id": ATOM_ID, "evidence": 2},
            ) as publish,
        ):
            result = processor._publish_regime_atom(
                ATOM_CONFIG,
                parsed=changed,
                series_ids_used=["CPIAUCSL", "GDP"],
                llm_result={"model": "test-model"},
                model="test-model",
                correlation_id="corr-1",
                now=NOW,
            )
        current.assert_called_once_with(
            session, subject_type="regime", subject_id="global", limit=1
        )
        self.assertEqual(result["supersedes_atom_id"], PRIOR_ATOM_ID)
        publish.assert_called_once()
        atom_payload = publish.call_args.args[1]
        self.assertEqual(atom_payload["supersedes_atom_id"], PRIOR_ATOM_ID)
        self.assertEqual(atom_payload["subject_type"], "regime")
        self.assertEqual(atom_payload["subject_id"], "global")
        self.assertEqual(atom_payload["expires_at"], NOW + timedelta(hours=168))
        self.assertRegex(atom_payload["input_fingerprint"], r"^[0-9a-f]{64}$")
        evidence = publish.call_args.args[2]
        self.assertEqual(
            evidence,
            [
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "supports",
                },
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "GDP",
                    "relationship": "supports",
                },
            ],
        )

    def test_identical_classification_does_not_supersede(self):
        from processors.macro_regime import MacroRegimeProcessor

        processor = MacroRegimeProcessor()
        unchanged = {
            "regime": "expansion",
            "sub_regime": "risk_on",
            "direction": "bullish",
            "confidence": "high",
            "timeframe": "medium_term",
            "summary": "Growth is broadening.",
        }
        prior = [
            {
                "id": PRIOR_ATOM_ID,
                "claim": "Regime: expansion (risk_on) — bullish, high confidence",
            }
        ]
        with (
            patch("processors.macro_regime.get_session", _session_factory(MagicMock())),
            patch("atoms.current_atoms", return_value=prior),
            patch(
                "atoms.publish_atom",
                return_value={"status": "published", "atom_id": ATOM_ID, "evidence": 2},
            ) as publish,
        ):
            processor._publish_regime_atom(
                ATOM_CONFIG,
                parsed=unchanged,
                series_ids_used=["CPIAUCSL"],
                llm_result={"model": "test-model"},
                model="test-model",
                correlation_id="corr-2",
                now=NOW,
            )
        self.assertIsNone(publish.call_args.args[1]["supersedes_atom_id"])


class AtomSnapshotHandlerTests(unittest.TestCase):
    def test_snapshot_payload_is_bounded_and_includes_evidence(self):
        import analysis_job_handlers as handlers

        job = SimpleNamespace(source_event_id="event-1")
        rows = [
            {
                "id": ATOM_ID,
                "subject_type": "econ_event",
                "subject_id": "event-a",
                "claim_type": "event_interpretation",
                "claim": "Event A interpretation.",
                "observation_text": "Scheduled: 2026-08-07T13:30:00Z.",
                "interpretation_text": "Neutral in-line outcome.",
                "scenario_text": None,
                "unknowns": [],
                "affected_assets": ["EURUSD"],
                "time_horizon": "48h",
                "confidence": 0.6,
                "confidence_components": {},
                "valid_from": NOW,
                "expires_at": NOW + timedelta(hours=48),
                "carry_forward": False,
                "invalidation_conditions": [],
                "status": "published",
                "supersedes_atom_id": None,
                "source_event_id": None,
                "prompt_version": "event_impact_v1",
                "model_slug": "test-model",
                "generation_attempt_id": None,
                "input_fingerprint": "a" * 64,
                "created_at": NOW,
                "published_at": NOW,
                "updated_at": NOW,
                "evidence": [
                    {
                        "evidence_type": "econ_events",
                        "evidence_id": "abc123event",
                        "relationship": "context",
                        "excerpt": None,
                        "source_timestamp": NOW,
                    }
                ],
            }
        ]
        with (
            patch("atoms.current_atoms", return_value=rows) as current,
            patch(
                "analysis_job_handlers._job_settings",
                return_value={"query": {"max_atoms": 5}},
            ),
            patch(
                "section_snapshots.publish_section_snapshot",
                return_value=SimpleNamespace(changed=True),
            ) as publish,
        ):
            handlers.publish_analysis_atoms_snapshot(MagicMock(), job)
        current.assert_called_once()
        self.assertEqual(current.call_args.kwargs["limit"], 5)
        kwargs = publish.call_args.kwargs
        self.assertEqual(kwargs["section_key"], "analysis_atoms")
        self.assertEqual(kwargs["scope_key"], "global")
        self.assertEqual(kwargs["render_context"], {"row_limit": 5})
        self.assertEqual(len(kwargs["payload"]["atoms"]), 1)
        atom_row = kwargs["payload"]["atoms"][0]
        self.assertEqual(atom_row["evidence"][0]["evidence_id"], "abc123event")
        self.assertEqual(atom_row["evidence"][0]["relationship"], "context")
        self.assertEqual(kwargs["data_freshness_at"], NOW)

    def test_expire_handler_expires_then_republishes_snapshot(self):
        import analysis_job_handlers as handlers

        job = SimpleNamespace(source_event_id="event-1")
        snapshot = SimpleNamespace(section_key="analysis_atoms", changed=True)
        with (
            patch(
                "analysis_job_handlers._config",
                return_value={"analysis_atoms": {"enabled": True}},
            ),
            patch("atoms.expire_atoms", return_value={"expired": 2}) as expire,
            patch(
                "analysis_job_handlers.publish_analysis_atoms_snapshot",
                return_value=snapshot,
            ) as publish,
        ):
            result = handlers.expire_analysis_atoms(MagicMock(), job)
        expire.assert_called_once()
        publish.assert_called_once()
        self.assertIs(result, snapshot)
        self.assertIn("publish_analysis_atoms_snapshot", handlers._HANDLERS)
        self.assertIn("expire_analysis_atoms", handlers._HANDLERS)


class AtomRoutingTests(unittest.TestCase):
    def _event(self, event_type: str, **payload) -> SimpleNamespace:
        return SimpleNamespace(
            event_id="event-1",
            event_type=event_type,
            source="fred" if event_type.startswith("macro") else "oanda",
            source_event_id="source-1",
            observed_at=NOW,
            effective_at=NOW,
            content_hash="a" * 64,
            correlation_id="corr-1",
            payload=payload,
            metadata={},
            entities=[],
            markets=[],
            importance_hint=None,
        )

    def test_material_event_enqueues_atom_snapshot_job(self):
        from events.routing import initial_handler

        source = self._event(
            "macro_release", series_id="PAYEMS", value=180, consensus=160
        )
        enqueued = []

        def capture(*_args, **kwargs):
            enqueued.append(kwargs)
            return SimpleNamespace()

        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=ROUTING_CONFIG),
            patch("jobs.enqueue_job", side_effect=capture),
            patch(
                "materiality.assess_event_materiality",
                side_effect=[
                    SimpleNamespace(should_route=True, score=0.9),
                    SimpleNamespace(should_route=True, score=0.9),
                ],
            ),
            patch(
                "macro_releases.upsert_macro_release_card",
                return_value={"stage": "t0"},
            ),
            patch(
                "reaction_windows.initialize_reaction_windows",
                return_value={"created": 6},
            ),
        ):
            initial_handler(MagicMock(), source)
        atom_jobs = [
            job
            for job in enqueued
            if job["job_type"] == "publish_analysis_atoms_snapshot"
        ]
        self.assertEqual(len(atom_jobs), 1)
        self.assertEqual(atom_jobs[0]["dedupe_key"], "analysis_atoms:global")
        self.assertEqual(atom_jobs[0]["input_fingerprint"], f"{'a' * 64}:atoms")
        self.assertEqual(atom_jobs[0]["priority"], 500)
        self.assertEqual(atom_jobs[0]["source_event_id"], "event-1")

    def test_repeated_immaterial_event_does_not_enqueue_atom_job(self):
        from events.routing import initial_handler

        source = self._event(
            "macro_release", series_id="PAYEMS", value=160, consensus=160
        )
        decisions = [
            SimpleNamespace(should_route=False, score=0.1),
            SimpleNamespace(should_route=False, score=0.1),
        ]
        enqueued = []

        def capture(*_args, **kwargs):
            enqueued.append(kwargs)
            return SimpleNamespace()

        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=ROUTING_CONFIG),
            patch("jobs.enqueue_job", side_effect=capture),
            patch("materiality.assess_event_materiality", side_effect=decisions),
            patch(
                "macro_releases.upsert_macro_release_card",
                return_value={"stage": "t0"},
            ),
            patch(
                "reaction_windows.initialize_reaction_windows",
                return_value={"created": 6},
            ),
        ):
            initial_handler(MagicMock(), source)
        self.assertNotIn(
            "publish_analysis_atoms_snapshot",
            [job["job_type"] for job in enqueued],
        )


class ReconciliationAtomTests(unittest.TestCase):
    class SessionContext:
        def __enter__(self):
            return MagicMock()

        def __exit__(self, *args):
            return False

    def test_reconciliation_records_atoms_expired_isolated_by_class(self):
        import reconciliation

        with (
            patch.object(
                reconciliation, "get_session", return_value=self.SessionContext()
            ),
            patch("jobs.reconcile_jobs", return_value=[]),
            patch(
                "section_snapshots.reconcile_snapshots", return_value={"repaired": 0}
            ),
            patch(
                "reaction_windows.backfill_reaction_windows",
                return_value={"completed": 0},
            ),
            patch(
                "story_confirmation.backfill_story_confirmations",
                return_value={"updated": 0},
            ),
            patch(
                "events.freshness.refresh_freshness_states",
                return_value={"changed": 0},
            ),
            patch("atoms.expire_atoms", return_value={"expired": 3}) as expire,
        ):
            result = reconciliation.reconcile_event_pipeline(
                {"event_pipeline": {"jobs": {}}}
            )
        self.assertEqual(result["atoms_expired"], 3)
        expire.assert_called_once()

    def test_reconciliation_atom_failure_is_isolated(self):
        import reconciliation

        with (
            patch.object(
                reconciliation, "get_session", return_value=self.SessionContext()
            ),
            patch("jobs.reconcile_jobs", return_value=[]),
            patch(
                "section_snapshots.reconcile_snapshots", return_value={"repaired": 0}
            ),
            patch(
                "reaction_windows.backfill_reaction_windows",
                return_value={"completed": 0},
            ),
            patch(
                "story_confirmation.backfill_story_confirmations",
                return_value={"updated": 0},
            ),
            patch(
                "events.freshness.refresh_freshness_states",
                return_value={"changed": 0},
            ),
            patch("atoms.expire_atoms", side_effect=RuntimeError("boom")),
        ):
            result = reconciliation.reconcile_event_pipeline(
                {"event_pipeline": {"jobs": {}}}
            )
        self.assertEqual(result["atoms_expired"], 0)
        self.assertIn("atoms:RuntimeError", result["errors"])
        self.assertNotIn("boom", str(result))

    def test_reconciliation_real_expiry_against_empty_session_is_harmless(self):
        import reconciliation

        with (
            patch.object(
                reconciliation, "get_session", return_value=self.SessionContext()
            ),
            patch("jobs.reconcile_jobs", return_value=[]),
            patch(
                "section_snapshots.reconcile_snapshots", return_value={"repaired": 0}
            ),
            patch(
                "reaction_windows.backfill_reaction_windows",
                return_value={"completed": 0},
            ),
            patch(
                "story_confirmation.backfill_story_confirmations",
                return_value={"updated": 0},
            ),
            patch(
                "events.freshness.refresh_freshness_states",
                return_value={"changed": 0},
            ),
        ):
            result = reconciliation.reconcile_event_pipeline(
                {"event_pipeline": {"jobs": {}}}
            )
        self.assertEqual(result["atoms_expired"], 0)
        self.assertNotIn(
            "atoms:",
            [error for error in result["errors"] if error.startswith("atoms:")],
        )


class CliCommandTests(unittest.TestCase):
    def test_reconcile_analysis_atoms_command_is_registered(self):
        from cli import cli

        self.assertIn("reconcile-analysis-atoms", cli.commands)


if __name__ == "__main__":
    unittest.main()
