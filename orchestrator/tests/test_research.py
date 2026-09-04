import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-research-test-state",
        "CONFIG_DIR": str(ORCH_ROOT.parent / "config"),
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "trading_data",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

from research import (  # noqa: E402
    get_theme,
    list_themes,
    portfolio_context,
    upsert_holdings,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
THEME_ID = UUID("11111111-1111-4111-8111-111111111111")
THESIS_ID = UUID("22222222-2222-4222-8222-222222222222")
ATOM_ID = UUID("33333333-3333-4333-8333-333333333333")
DOC_ID = UUID("44444444-4444-4444-8444-444444444444")


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
            return Result()
        return self.results.pop(0)


def theme_row(**overrides):
    value = {
        "id": THEME_ID,
        "name": "AI Compute",
        "definition": "Semiconductor demand supercycle.",
        "horizon": "multi_year",
        "macro_drivers": ["AI capex acceleration", "power constraints"],
        "key_indicators": ["CPIAUCSL", "INDPRO"],
        "status": "active",
        "review_at": None,
        "invalidation_conditions": ["Hyperscaler capex drops > 20%."],
        "confidence": 0.72,
        "confidence_components": {"evidence": 0.8, "macro": 0.64},
        "created_at": NOW,
        "updated_at": NOW,
    }
    value.update(overrides)
    return value


def thesis_row(**overrides):
    value = {
        "id": THESIS_ID,
        "theme_id": THEME_ID,
        "company": "Nvidia Corp",
        "symbol": "NVDA",
        "claim": "AI capex compounds.",
        "variant_perception": "Consensus under-models enterprise software pull-through.",
        "status": "active",
        "horizon": "multi_year",
        "review_at": None,
        "confidence": 0.7,
        "created_at": NOW,
        "updated_at": NOW,
    }
    value.update(overrides)
    return value


class PortfolioTests(unittest.TestCase):
    def test_weights_clamped_and_sources_validated(self):
        session = Session([Result()])
        count = upsert_holdings(
            session,
            holdings=[
                {
                    "symbol": "NVDA",
                    "company": "Nvidia Corp",
                    "sector": "Information Technology",
                    "country": "US",
                    "currency": "USD",
                    "weight": 1.5,  # clamped to 1.0
                    "theme_tags": ["AI Compute"],
                    "source": "manual",
                },
                {
                    "symbol": "TSM",
                    "company": "TSMC",
                    "sector": "Information Technology",
                    "country": "TW",
                    "currency": "TWD",
                    "weight": -0.5,  # clamped to 0.0
                    "theme_tags": ["AI Compute"],
                    "source": "import",
                },
            ],
        )
        self.assertEqual(count, 2)
        insert_sql = session.calls[0][0]
        self.assertIn("INSERT INTO portfolio_holdings", insert_sql)
        self.assertIn("ON CONFLICT (symbol) DO UPDATE", insert_sql)
        params = session.calls[0][1]
        self.assertEqual(params[0]["weight"], 1.0)
        self.assertEqual(params[1]["weight"], 0.0)

        invalid = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported source"):
            upsert_holdings(
                invalid,
                holdings=[{"symbol": "AAPL", "source": "provider_secret"}],
            )
        self.assertEqual(invalid.calls, [])

    def test_context_exposures_sum_within_band_per_dimension(self):
        session = Session(
            [
                Result(first={"total": 0.15}),  # total
                Result(
                    rows=[{"theme": "AI Compute", "exposure": 0.15, "holdings": 2}]
                ),  # themes
                Result(
                    rows=[
                        {
                            "id": UUID("55555555-5555-4555-8555-555555555555"),
                            "description": "FOMC",
                            "expected_at": NOW,
                            "state": "pending",
                            "theme_id": THEME_ID,
                            "company": "Nvidia Corp",
                            "symbol": "NVDA",
                        }
                    ]
                ),  # catalysts
                Result(
                    rows=[
                        {
                            "kind": "thesis",
                            "id": THESIS_ID,
                            "title": "AI capex compounds.",
                            "review_at": NOW,
                            "status": "active",
                            "created_at": NOW,
                        }
                    ]
                ),  # review_schedule
                Result(
                    rows=[
                        {
                            "bucket": "Information Technology",
                            "exposure": 0.15,
                            "holdings": 2,
                        }
                    ]
                ),  # sectors
                Result(
                    rows=[
                        {"bucket": "US", "exposure": 0.10, "holdings": 1},
                        {"bucket": "TW", "exposure": 0.05, "holdings": 1},
                    ]
                ),  # countries
                Result(
                    rows=[{"bucket": "USD", "exposure": 0.10, "holdings": 1}]
                ),  # currencies
            ]
        )
        context = portfolio_context(session)
        self.assertAlmostEqual(context["total_weight"], 0.15)
        self.assertEqual(context["sectors"][0]["bucket"], "Information Technology")
        self.assertEqual(len(context["countries"]), 2)
        self.assertEqual(context["themes"][0]["theme"], "AI Compute")
        self.assertEqual(len(context["catalysts"]), 1)
        self.assertEqual(context["review_schedule"][0]["kind"], "thesis")
        for sql, _ in session.calls:
            self.assertIn("LIMIT", sql)
        for sql, _ in session.calls[1:]:
            self.assertIn("LIMIT :limit", sql)


class LoaderTests(unittest.TestCase):
    def test_list_themes_is_bounded(self):
        session = Session(
            [
                Result(
                    rows=[
                        {
                            "id": THEME_ID,
                            "name": "AI Compute",
                            "definition": "d",
                            "horizon": "multi_year",
                            "status": "active",
                            "review_at": None,
                            "confidence": 0.7,
                            "created_at": NOW,
                            "updated_at": NOW,
                            "entity_count": 3,
                            "active_thesis_count": 1,
                        }
                    ]
                )
            ]
        )
        rows = list_themes(session, limit=9999)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_thesis_count"], 1)
        sql, params = session.calls[0]
        self.assertIn("LIMIT :limit", sql)
        self.assertEqual(params["limit"], 100)

    def test_get_theme_is_bounded_and_shaped(self):
        session = Session(
            [
                Result(first=theme_row()),
                Result(
                    rows=[
                        {
                            "entity_type": "macro_series",
                            "entity_id": "CPIAUCSL",
                            "display_name": "CPI",
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(rows=[thesis_row()]),
                Result(
                    first={
                        "version": 2,
                        "claim": "AI capex compounds.",
                        "variant_perception": None,
                        "confidence": 0.7,
                        "rationale": "Second revision.",
                        "changed_by": "operator",
                        "created_at": NOW,
                    }
                ),
                Result(rows=[{"relationship": "supports", "count": 1}]),
                Result(
                    rows=[
                        {
                            "id": UUID("66666666-6666-4666-8666-666666666666"),
                            "description": "FOMC",
                            "expected_at": NOW,
                            "state": "pending",
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "id": UUID("77777777-7777-4777-8777-777777777777"),
                            "description": "Rate shock",
                            "kind": "external",
                            "severity": "high",
                            "created_at": NOW,
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "id": ATOM_ID,
                            "claim": "Capex accelerates.",
                            "confidence": 0.8,
                            "status": "published",
                            "valid_from": NOW,
                            "thesis_id": THESIS_ID,
                            "relationship": "supports",
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "series_id": "CPIAUCSL",
                            "observed_at": NOW,
                            "value": 3.2,
                            "released_at": NOW,
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "event_id": "ev-1",
                            "event_name": "CPI release",
                            "country": "US",
                            "scheduled_at": NOW,
                            "impact_level": "high",
                            "consensus": None,
                            "previous": None,
                            "actual": None,
                        }
                    ]
                ),
            ]
        )
        theme = get_theme(session, str(THEME_ID))
        self.assertEqual(theme["name"], "AI Compute")
        self.assertEqual(len(theme["entities"]), 1)
        self.assertEqual(len(theme["theses"]), 1)
        thesis = theme["theses"][0]
        self.assertEqual(thesis["latest_version"]["version"], 2)
        self.assertEqual(
            thesis["evidence_counts"], [{"relationship": "supports", "count": 1}]
        )
        self.assertEqual(len(thesis["catalysts"]), 1)
        self.assertEqual(len(thesis["risks"]), 1)
        self.assertEqual(theme["atoms"][0]["relationship"], "supports")
        self.assertEqual(theme["key_indicator_values"][0]["series_id"], "CPIAUCSL")
        self.assertEqual(theme["upcoming_events"][0]["event_id"], "ev-1")
        for sql, _ in session.calls:
            self.assertIn("LIMIT", sql)
            self.assertNotIn("extracted_text", sql)

    def test_get_theme_unknown_returns_none(self):
        session = Session([Result(first=None)])
        self.assertIsNone(get_theme(session, str(THEME_ID)))


class ResearchSnapshotTests(unittest.TestCase):
    def test_handler_payload_is_bounded(self):
        import analysis_job_handlers as handlers

        rows = [
            {
                "id": THEME_ID,
                "name": "AI Compute",
                "definition": "d",
                "horizon": "multi_year",
                "status": "active",
                "review_at": None,
                "confidence": 0.7,
                "created_at": NOW,
                "updated_at": NOW,
                "entity_count": 1,
                "active_thesis_count": 0,
            }
        ]
        job = SimpleNamespace(source_event_id="event-1")
        with (
            patch("research.list_themes", return_value=rows) as themes,
            patch(
                "analysis_job_handlers._job_settings",
                return_value={"query": {"max_themes": 5}},
            ),
            patch(
                "section_snapshots.publish_section_snapshot",
                return_value=SimpleNamespace(changed=True),
            ) as publish,
        ):
            handlers.publish_research_snapshot(MagicMock(), job)
        themes.assert_called_once()
        self.assertEqual(themes.call_args.kwargs["limit"], 5)
        kwargs = publish.call_args.kwargs
        self.assertEqual(kwargs["section_key"], "research")
        self.assertEqual(kwargs["scope_key"], "global")
        self.assertEqual(kwargs["render_context"], {"row_limit": 5})
        self.assertEqual(len(kwargs["payload"]["themes"]), 1)
        self.assertEqual(kwargs["payload"]["themes"][0]["name"], "AI Compute")
        self.assertEqual(kwargs["payload"]["themes"][0]["id"], str(THEME_ID))
        self.assertEqual(kwargs["data_freshness_at"], NOW)
        self.assertIn("publish_research_snapshot", handlers._HANDLERS)
        self.assertIn("publish_research_snapshot", handlers.__all__)

    def test_handler_defaults_to_twenty_themes(self):
        import analysis_job_handlers as handlers

        with (
            patch("research.list_themes", return_value=[]) as themes,
            patch("analysis_job_handlers._job_settings", return_value={}),
            patch(
                "section_snapshots.publish_section_snapshot",
                return_value=SimpleNamespace(changed=False),
            ) as publish,
        ):
            handlers.publish_research_snapshot(MagicMock(), SimpleNamespace())
        self.assertEqual(themes.call_args.kwargs["limit"], 20)
        self.assertEqual(publish.call_args.kwargs["payload"], {"themes": []})
        self.assertIsNone(publish.call_args.kwargs["data_freshness_at"])


if __name__ == "__main__":
    unittest.main()
