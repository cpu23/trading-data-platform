from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

CONFIG = {
    "market_state": {"enabled": True},
    "reaction_windows": {
        "max_event_age_minutes": 360,
        "session_close": "21:00:00",
        "backfill_limit": 100,
    },
    "macro_event_mappings": {
        "PAYEMS": {
            "event_name": "US nonfarm payrolls",
            "priority": 10,
            "instruments": ["EURUSD"],
            "expected_sensitivity": {"EURUSD": "negative"},
        }
    },
}


def event(event_type: str, **payload):
    now = datetime.now(UTC)
    return SimpleNamespace(
        event_id=uuid4(),
        event_type=event_type,
        source="fred" if event_type.startswith("macro") else "oanda",
        source_event_id="source-1",
        observed_at=now,
        effective_at=now,
        content_hash="a" * 64,
        correlation_id=uuid4(),
        payload=payload,
        metadata={},
        entities=[],
        markets=[],
        importance_hint=None,
    )


class EventRoutingTests(unittest.TestCase):
    def test_price_tick_updates_features_without_model_work(self):
        from events.routing import initial_handler

        source = event(
            "price_tick",
            symbol="EURUSD",
            timestamp=datetime.now(UTC),
            close=1.1,
        )
        decision = SimpleNamespace(should_route=False, score=0.1)
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=CONFIG),
            patch("analysis_jobs.enqueue_job", return_value=SimpleNamespace()),
            patch("materiality.assess_event_materiality", return_value=decision),
            patch(
                "market_state.update_price_features", return_value={"symbol": "EURUSD"}
            ) as update,
        ):
            result = initial_handler(MagicMock(), source)
        update.assert_called_once()
        self.assertEqual(result["market_state"]["symbol"], "EURUSD")
        self.assertNotIn("llm", result)

    def test_material_macro_release_gets_t0_and_bounded_stage_jobs(self):
        from events.routing import initial_handler

        source = event("macro_release", series_id="PAYEMS", value=180, consensus=160)
        routed = SimpleNamespace(should_route=True, score=0.9)
        enqueued = []

        def capture(*_args, **kwargs):
            enqueued.append(kwargs)
            return SimpleNamespace()

        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=CONFIG),
            patch("analysis_jobs.enqueue_job", side_effect=capture),
            patch("materiality.assess_event_materiality", return_value=routed),
            patch(
                "macro_releases.upsert_macro_release_card", return_value={"stage": "t0"}
            ),
            patch(
                "reaction_windows.initialize_reaction_windows",
                return_value={"created": 6},
            ) as initialize,
        ):
            result = initial_handler(MagicMock(), source)
        initialize.assert_called_once()
        job_types = [item["job_type"] for item in enqueued]
        self.assertEqual(job_types.count("publish_macro_release_snapshot"), 1)
        self.assertEqual(job_types.count("update_macro_release_reactions"), 6)
        self.assertEqual(result["macro_release"]["stage"], "t0")

    def test_immaterial_macro_release_publishes_card_but_skips_reactions(self):
        from events.routing import initial_handler

        source = event("macro_release", series_id="PAYEMS", value=160, consensus=160)
        decisions = [
            SimpleNamespace(should_route=False, score=0.1),
            SimpleNamespace(should_route=False, score=0.1),
        ]
        enqueued = []
        with (
            patch("events.freshness.record_event_observation", return_value={}),
            patch("events.routing._config", return_value=CONFIG),
            patch(
                "analysis_jobs.enqueue_job",
                side_effect=lambda *_args, **kwargs: enqueued.append(kwargs)
                or SimpleNamespace(),
            ),
            patch("materiality.assess_event_materiality", side_effect=decisions),
            patch(
                "macro_releases.upsert_macro_release_card", return_value={"stage": "t0"}
            ),
            patch("reaction_windows.initialize_reaction_windows") as initialize,
        ):
            result = initial_handler(MagicMock(), source)
        initialize.assert_not_called()
        self.assertTrue(result["reactions"]["suppressed"])
        self.assertIn(
            "publish_macro_release_snapshot", [item["job_type"] for item in enqueued]
        )
        self.assertNotIn(
            "update_macro_release_reactions", [item["job_type"] for item in enqueued]
        )


class MacroJobHandlerTests(unittest.TestCase):
    def test_reaction_job_backfills_advances_and_publishes(self):
        import analysis_job_handlers as handlers

        job = SimpleNamespace(
            source_event_id="event-1",
            payload={"event_id": "event-1", "stage": "reaction"},
        )
        snapshot = SimpleNamespace(section_key="macro_releases", changed=True)
        with (
            patch("analysis_job_handlers._config", return_value=CONFIG),
            patch(
                "reaction_windows.backfill_reaction_windows",
                return_value={"scanned": 1, "completed": 1, "unresolved": 0},
            ),
            patch(
                "reaction_windows.list_event_reactions",
                return_value=[
                    {
                        "instrument_symbol": "EURUSD",
                        "horizon": "1m",
                        "reaction_state": "persistence",
                        "missing_data_reason": None,
                    }
                ],
            ),
            patch("macro_releases.advance_macro_release_stage") as advance,
            patch(
                "analysis_job_handlers.publish_macro_release_snapshot",
                return_value=snapshot,
            ) as publish,
        ):
            result = handlers.update_macro_release_reactions(MagicMock(), job)
        advance.assert_called_once()
        publish.assert_called_once()
        self.assertIs(result, snapshot)


if __name__ == "__main__":
    unittest.main()
