import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.update(
    {
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["CONFIG_DIR"] = str(API_ROOT.parent / "config")

AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

CONFIG = {
    "timezone": {"primary": {"name": "Europe/London", "label": "London"}},
    "watchlist": {
        "trading": [
            {"symbol": "EURUSD", "type": "forex"},
            {"symbol": "DXY", "type": "index"},
            {"symbol": "SP500", "type": "index"},
            {"symbol": "XAUUSD", "type": "metal"},
            {"symbol": "WTICOUSD", "type": "commodity"},
        ]
    },
    "dashboard": {
        "indicators": [
            {
                "series_id": "DGS10",
                "label": "10Y yield",
                "precision": 2,
                "category": "rates",
            },
            {
                "series_id": "T10Y2Y",
                "label": "10Y-2Y spread",
                "precision": 2,
                "category": "yield_curve",
            },
            {
                "series_id": "DFII10",
                "label": "10Y real yield",
                "precision": 2,
                "category": "real_yields",
            },
        ]
    },
    "event_pipeline": {"sse": {"enabled": True}},
}


def make_app(auth=False):
    from fastapi import Depends, FastAPI
    from fastapi.templating import Jinja2Templates

    from auth import verify_credentials
    from routes.views.cockpit_panels import router

    dependencies = [Depends(verify_credentials)] if auth else None
    app = FastAPI(dependencies=dependencies)
    app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
    app.include_router(router)
    return app


class CrossAssetQueryTests(unittest.TestCase):
    def test_all_panels_hide_when_no_data(self):
        import routes.views.cockpit_panels as cockpit_panels

        with (
            patch("routes.views.cockpit_panels.query_many", return_value=[]),
            patch("routes.views.cockpit_panels.query_one", return_value=None),
        ):
            payload = cockpit_panels.load_cross_asset(CONFIG)
        self.assertFalse(payload["available"])
        self.assertEqual(len(payload["panels"]), 8)
        for panel in payload["panels"]:
            self.assertFalse(panel["available"])
            self.assertEqual(panel["rows"], [])

    def test_yield_curve_panel_with_spread(self):
        import routes.views.cockpit_panels as cockpit_panels

        config = {
            "dashboard": {
                "indicators": [
                    {
                        "series_id": "DGS10",
                        "label": "10Y yield",
                        "precision": 2,
                        "category": "yield_curve",
                    },
                    {
                        "series_id": "T10Y2Y",
                        "label": "10Y-2Y spread",
                        "precision": 2,
                        "category": "yield_curve",
                    },
                ]
            }
        }

        def fake_query_many(sql, params=None, config=None):
            if "macro_series" in sql:
                return [
                    {"series_id": "DGS10", "value": 4.25, "observed_at": NOW},
                    {"series_id": "T10Y2Y", "value": 0.35, "observed_at": NOW},
                ]
            return []

        with patch(
            "routes.views.cockpit_panels.query_many", side_effect=fake_query_many
        ):
            payload = cockpit_panels.load_cross_asset(config)
        panel = next(p for p in payload["panels"] if p["key"] == "yield_curve")
        self.assertTrue(panel["available"])
        labels = [row["label"] for row in panel["rows"]]
        self.assertIn("Spread", labels)
        spread = next(row for row in panel["rows"] if row["label"] == "Spread")
        self.assertEqual(spread["display"], "3.90")

    def test_equity_breadth_counts_advancing_declining(self):
        import routes.views.cockpit_panels as cockpit_panels

        day = datetime(2026, 8, 6, tzinfo=UTC)
        rows = [
            {"symbol": "DXY", "bucket": day, "close": 100.0},
            {"symbol": "DXY", "bucket": day - timedelta(days=1), "close": 99.0},
            {"symbol": "SP500", "bucket": day, "close": 5000.0},
            {"symbol": "SP500", "bucket": day - timedelta(days=1), "close": 5100.0},
        ]
        with patch("routes.views.cockpit_panels.query_many", return_value=rows):
            payload = cockpit_panels.load_cross_asset(CONFIG)
        panel = next(p for p in payload["panels"] if p["key"] == "equity_breadth")
        self.assertTrue(panel["available"])
        self.assertEqual(panel["summary"], "1 advancing · 1 declining · 0 flat")

    def test_volatility_term_structure_skips_without_symbols(self):
        import routes.views.cockpit_panels as cockpit_panels

        with patch("routes.views.cockpit_panels.query_many") as query:
            payload = cockpit_panels.load_cross_asset(CONFIG)
        panel = next(
            p for p in payload["panels"] if p["key"] == "volatility_term_structure"
        )
        self.assertFalse(panel["available"])
        sql_calls = [call.args[0] for call in query.call_args_list]
        self.assertFalse(any("market_data_1m" in sql for sql in sql_calls))

    def test_rolling_correlation_hides_below_20_buckets(self):
        import routes.views.cockpit_panels as cockpit_panels

        day = datetime(2026, 8, 6, tzinfo=UTC)
        rows = []
        for symbol, start in (("EURUSD", 1.0), ("SP500", 5000.0)):
            for index in range(10):
                rows.append(
                    {
                        "symbol": symbol,
                        "bucket": day - timedelta(days=index),
                        "close": start * (1 + 0.01 * index),
                    }
                )
        with patch("routes.views.cockpit_panels.query_many", return_value=rows):
            payload = cockpit_panels.load_cross_asset(CONFIG)
        panel = next(p for p in payload["panels"] if p["key"] == "rolling_correlation")
        self.assertFalse(panel["available"])

    def test_rolling_correlation_current_vs_prior(self):
        import routes.views.cockpit_panels as cockpit_panels

        day = datetime(2026, 8, 6, tzinfo=UTC)
        rows = []
        # Perfectly correlated (proportional, variable) return profiles.
        for index in range(40):
            factor = 1 + 0.005 * index + 0.0001 * index * index
            rows.append(
                {
                    "symbol": "EURUSD",
                    "bucket": day - timedelta(days=index),
                    "close": 1.0 * factor,
                }
            )
            rows.append(
                {
                    "symbol": "SP500",
                    "bucket": day - timedelta(days=index),
                    "close": 5000.0 * factor,
                }
            )
        with patch("routes.views.cockpit_panels.query_many", return_value=rows):
            payload = cockpit_panels.load_cross_asset(CONFIG)
        panel = next(p for p in payload["panels"] if p["key"] == "rolling_correlation")
        self.assertTrue(panel["available"])
        labels = [row["label"] for row in panel["rows"]]
        self.assertEqual(labels, ["Current 20d", "Prior 20d", "Change"])
        self.assertEqual(panel["rows"][0]["display"], "1.00")

    def test_commodity_impulse_and_heatmap_rows(self):
        import routes.views.cockpit_panels as cockpit_panels

        day = datetime(2026, 8, 6, tzinfo=UTC)
        rows = [
            {"symbol": "WTICOUSD", "bucket": day, "close": 80.0},
            {"symbol": "WTICOUSD", "bucket": day - timedelta(days=1), "close": 78.0},
            {"symbol": "XAUUSD", "bucket": day, "close": 2400.0},
            {"symbol": "XAUUSD", "bucket": day - timedelta(days=1), "close": 2400.0},
            {"symbol": "EURUSD", "bucket": day, "close": 1.10},
            {"symbol": "EURUSD", "bucket": day - timedelta(days=1), "close": 1.09},
        ]
        with patch("routes.views.cockpit_panels.query_many", return_value=rows):
            payload = cockpit_panels.load_cross_asset(CONFIG)
        commodity = next(
            p for p in payload["panels"] if p["key"] == "commodity_impulse"
        )
        self.assertTrue(commodity["available"])
        self.assertEqual(commodity["rows"][0]["display"], "+2.56%")
        heatmap = next(p for p in payload["panels"] if p["key"] == "session_heatmap")
        self.assertTrue(heatmap["available"])
        self.assertIn("EURUSD", [row["label"] for row in heatmap["rows"]])

    def test_change_since_event_uses_percentage_move_keys(self):
        import routes.views.cockpit_panels as cockpit_panels

        row = {
            "symbol": "EURUSD",
            "as_of": NOW,
            "features": {"percentage_move": {"5m": 0.3, "30m": -0.1}},
        }
        with patch("routes.views.cockpit_panels.query_many", return_value=[row]):
            payload = cockpit_panels.load_cross_asset(CONFIG)
        panel = next(p for p in payload["panels"] if p["key"] == "change_since_event")
        self.assertTrue(panel["available"])
        self.assertEqual(
            [r["label"] for r in panel["rows"]], ["EURUSD 30m", "EURUSD 5m"]
        )


class CatalystsTests(unittest.TestCase):
    def test_bounded_to_six_and_spread_across_days(self):
        import routes.views.cockpit_panels as cockpit_panels

        rows = []
        for index in range(20):
            rows.append(
                {
                    "event_id": f"event-{index}",
                    "event_name": f"Event {index}",
                    "country": "US",
                    "scheduled_at": NOW + timedelta(days=index // 3, hours=index % 24),
                    "impact_level": "high",
                    "source": "forex_factory",
                    "metadata": {"currency": "USD"},
                }
            )
        with patch("routes.views.cockpit_panels.query_many", return_value=rows):
            payload = cockpit_panels.load_catalysts(CONFIG)
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["catalysts"]), 6)
        # Round-robin spread: no two picks share a day until every day is used.
        days = [event["day_key"] for event in payload["catalysts"]]
        self.assertEqual(len(set(days)), len(days))
        # Impacted symbols resolved through ASSET_EVENT_RULES (US event).
        first = payload["catalysts"][0]
        self.assertIn("DXY", first["impacted_symbols"])
        self.assertIn("SP500", first["impacted_symbols"])
        self.assertIsNotNone(first["countdown_minutes"])
        self.assertIsNotNone(first["countdown_display"])

    def test_only_high_impact_events_and_countdown(self):
        from datetime import datetime as real_datetime

        import routes.views.cockpit_panels as cockpit_panels

        class _FixedNow(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)

        rows = [
            {
                "event_id": "low-1",
                "event_name": "Low event",
                "country": "US",
                "scheduled_at": (NOW + timedelta(hours=1)).isoformat(),
                "impact_level": "low",
                "source": "forex_factory",
                "metadata": {"currency": "USD"},
            },
            {
                "event_id": "high-1",
                "event_name": "High event",
                "country": "JP",
                "scheduled_at": (NOW + timedelta(hours=3)).isoformat(),
                "impact_level": "high",
                "source": "forex_factory",
                "metadata": {"currency": "JPY"},
            },
        ]
        with (
            patch("routes.views.cockpit_panels.query_many", return_value=rows),
            patch("routes.views.cockpit_panels.datetime", _FixedNow),
        ):
            payload = cockpit_panels.load_catalysts(CONFIG)
        self.assertEqual(len(payload["catalysts"]), 1)
        event = payload["catalysts"][0]
        self.assertEqual(event["event_name"], "High event")
        self.assertEqual(event["countdown_minutes"], 180)
        self.assertEqual(event["countdown_display"], "3h 0m")

    def test_query_failure_is_fail_soft(self):
        import routes.views.cockpit_panels as cockpit_panels

        with patch(
            "routes.views.cockpit_panels.query_many", side_effect=RuntimeError("boom")
        ):
            payload = cockpit_panels.load_catalysts(CONFIG)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["catalysts"], [])


class BriefingDeltaTests(unittest.TestCase):
    def test_deterministic_delta_bullets_and_atom_counts(self):
        import routes.views.cockpit_panels as cockpit_panels

        latest = {
            "briefing_date": "2026-08-06",
            "sections": {
                "what_changed": "Markets rallied on softer CPI.",
                "interpretation": "Risk-on tone into the payrolls print.",
            },
        }

        def fake_query_many(sql, params=None, config=None):
            if "structured_opinions" in sql:
                return [
                    {
                        "opinion_id": "o-2",
                        "scope": "daily_2026-08-06",
                        "summary": "Risk-on tone.",
                        "created_at": NOW,
                        "reasoning": "...",
                    },
                    {
                        "opinion_id": "o-1",
                        "scope": "daily_2026-08-05",
                        "summary": "Risk-off tone.",
                        "created_at": NOW - timedelta(days=1),
                        "reasoning": "...",
                    },
                ]
            if "analysis_atoms" in sql:
                return [
                    {"claim_type": "market", "count": 3},
                    {"claim_type": "regime", "count": 1},
                ]
            return []

        with (
            patch(
                "routes.views.cockpit_panels.get_briefing_latest", return_value=latest
            ),
            patch(
                "routes.views.cockpit_panels.query_many", side_effect=fake_query_many
            ),
        ):
            first = cockpit_panels.load_briefing_delta(CONFIG)
            second = cockpit_panels.load_briefing_delta(CONFIG)
        self.assertTrue(first["available"])
        # interpretation changed vs the previous briefing; what_changed is new.
        self.assertIn("New section: what_changed", first["bullets"])
        self.assertIn("Changed section: interpretation", first["bullets"])
        self.assertEqual(first["bullets"], second["bullets"])  # deterministic
        self.assertEqual(
            first["atoms"],
            [
                {"claim_type": "market", "count": 3},
                {"claim_type": "regime", "count": 1},
            ],
        )
        self.assertEqual(first["latest_date"], "2026-08-06")

    def test_no_previous_briefing_emits_present_sections(self):
        import routes.views.cockpit_panels as cockpit_panels

        latest = {"briefing_date": "2026-08-06", "sections": {"interpretation": "A"}}
        with (
            patch(
                "routes.views.cockpit_panels.get_briefing_latest", return_value=latest
            ),
            patch(
                "routes.views.cockpit_panels.query_many",
                side_effect=lambda sql, params=None, config=None: [],
            ),
        ):
            payload = cockpit_panels.load_briefing_delta(CONFIG)
        self.assertEqual(payload["bullets"], ["Section: interpretation"])

    def test_missing_briefing_is_fail_soft(self):
        from fastapi import HTTPException

        import routes.views.cockpit_panels as cockpit_panels

        with (
            patch(
                "routes.views.cockpit_panels.get_briefing_latest",
                side_effect=HTTPException(status_code=404, detail="No briefing found"),
            ),
            patch("routes.views.cockpit_panels.query_many", return_value=[]),
        ):
            payload = cockpit_panels.load_briefing_delta(CONFIG)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["bullets"], [])
        self.assertEqual(payload["atoms"], [])


class RouteTests(unittest.TestCase):
    def test_cross_asset_hides_absent_panels(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        payload = {
            "available": False,
            "panels": [
                {
                    "key": "yield_curve",
                    "title": "Yield curve",
                    "rows": [],
                    "available": False,
                    "summary": None,
                }
            ],
        }
        with (
            patch("routes.views.cockpit_panels.load_cross_asset", return_value=payload),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/cross-asset")
        self.assertNotIn("data-panel-key", response.text)
        self.assertNotIn("Yield curve", response.text)
        self.assertIn("No cross-asset data available.", response.text)

    def test_cross_asset_renders_available_panels_only(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        payload = {
            "available": True,
            "panels": [
                {
                    "key": "commodity_impulse",
                    "title": "Commodity impulse",
                    "rows": [
                        {
                            "label": "XAUUSD",
                            "display": "+0.50%",
                            "direction": "up",
                            "detail": None,
                        }
                    ],
                    "available": True,
                    "summary": None,
                },
                {
                    "key": "session_heatmap",
                    "title": "Session heat map",
                    "rows": [],
                    "available": False,
                    "summary": None,
                },
            ],
        }
        with (
            patch("routes.views.cockpit_panels.load_cross_asset", return_value=payload),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/cross-asset")
        self.assertIn('data-panel-key="commodity_impulse"', response.text)
        self.assertIn("+0.50%", response.text)
        self.assertNotIn("Session heat map", response.text)

    def test_catalysts_route_bounded_and_rendered(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        payload = {
            "available": True,
            "days": 7,
            "catalysts": [
                {
                    "event_name": "FOMC decision",
                    "day_label": "Today",
                    "countdown_display": "3h 0m",
                    "impacted_symbols": ["DXY", "SP500"],
                }
            ],
        }
        with (
            patch("routes.views.cockpit_panels.load_catalysts", return_value=payload),
            patch(
                "routes.views.cockpit_panels.app_config.load_config",
                return_value={"event_pipeline": {"sse": {"enabled": True}}},
            ),
        ):
            response = client.get("/partials/dashboard/catalysts")
        self.assertIn("FOMC decision", response.text)
        self.assertIn("in 3h 0m", response.text)
        self.assertIn("DXY", response.text)
        self.assertIn('data-live-section="catalysts"', response.text)

    def test_briefing_delta_route_renders(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        payload = {
            "available": True,
            "latest_date": "2026-08-06",
            "bullets": ["Changed section: interpretation"],
            "atoms": [{"claim_type": "market", "count": 3}],
        }
        with (
            patch(
                "routes.views.cockpit_panels.load_briefing_delta", return_value=payload
            ),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/briefing-delta")
        self.assertIn("Changed section: interpretation", response.text)
        self.assertIn("market", response.text)
        self.assertIn("3", response.text)


if __name__ == "__main__":
    unittest.main()
