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


def feed_row(
    observed_at,
    *,
    title="Fed holds rates",
    event_type="rate_decision",
    flags=(),
    windows=None,
    markets=None,
    importance=0.8,
    novelty=0.5,
    interpretation=True,
):
    if windows is None:
        windows = [
            {"timeframe": "PRICE", "horizon": "5m", "percentage_move": 0.4, "reaction_state": "persistence"},
            {"timeframe": "PRICE", "horizon": "30m", "percentage_move": -0.2, "reaction_state": "reversal"},
        ]
    if markets is None:
        markets = [
            {"symbol": "EURUSD"},
            {"symbol": "SP500"},
            {"symbol": "XAUUSD"},
            {"symbol": "DXY"},
            {"symbol": "USDJPY"},
            {"symbol": "GER40"},
        ]
    return {
        "event_id": "22222222-2222-4222-8222-222222222222",
        "observed_at": observed_at,
        "effective_at": observed_at,
        "published_at": observed_at,
        "event_type": event_type,
        "source": "reuters",
        "payload": {"title": title},
        "markets": markets,
        "importance": importance,
        "novelty": novelty,
        "reaction_windows": windows,
        "confirmation_flags": list(flags),
        "interpretation_available": interpretation,
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


class TopStripTests(unittest.TestCase):
    def test_no_llm_import_and_budget_regime_call_counts(self):
        import inspect
        import sys as sys_module

        import routes.views.cockpit_panels as cockpit_panels

        with (
            patch(
                "routes.views.cockpit_panels.get_regime_current",
                return_value={"regime": "risk_on"},
            ) as regime,
            patch(
                "routes.views.cockpit_panels.get_budget_status",
                return_value={"usage_pct": 42.0, "status": "ok"},
            ) as budget,
            patch("routes.views.cockpit_panels.query_one", return_value=None),
            patch("routes.views.cockpit_panels.query_many", return_value=[]),
        ):
            strip = cockpit_panels.load_top_strip(CONFIG)
        self.assertTrue(strip["available"])
        self.assertEqual(regime.call_count, 1)
        self.assertEqual(budget.call_count, 1)
        self.assertEqual(strip["regime"]["regime"], "risk_on")
        self.assertEqual(strip["budget"]["status"], "ok")
        # Deterministic session label from the primary display timezone hour.
        self.assertIn(strip["session_label"], {"Asia", "London", "New York"})
        # The loader itself is model-free: no llm_client anywhere in its source.
        source = inspect.getsource(cockpit_panels)
        self.assertNotIn("llm_client", source)
        self.assertFalse(any("llm_client" in name for name in sys_module.modules))
        self.assertEqual(cockpit_panels._session_label_for_hour(0), "Asia")
        self.assertEqual(cockpit_panels._session_label_for_hour(12), "London")
        self.assertEqual(cockpit_panels._session_label_for_hour(20), "New York")

    def test_subsections_fail_soft_independently(self):
        import routes.views.cockpit_panels as cockpit_panels

        def fake_query_one(sql, params=None, config=None):
            if "market_data" in sql and "MAX(timestamp)" in sql:
                return {"last_ts": NOW}
            if "market_events" in sql:
                raise RuntimeError("material event query exploded")
            if "econ_events" in sql:
                raise RuntimeError("catalyst query exploded")
            return None

        def fake_query_many(sql, params=None, config=None):
            if "market_data_1d" in sql or "market_data_5m" in sql:
                return []
            if "source_freshness_state" in sql:
                return [{"state": "current", "count": 2}]
            raise RuntimeError("unexpected query")

        with (
            patch("routes.views.cockpit_panels.query_one", side_effect=fake_query_one),
            patch(
                "routes.views.cockpit_panels.query_many", side_effect=fake_query_many
            ),
            patch(
                "routes.views.cockpit_panels.get_regime_current",
                side_effect=RuntimeError("regime down"),
            ),
            patch(
                "routes.views.cockpit_panels.get_budget_status",
                side_effect=RuntimeError("budget down"),
            ),
        ):
            strip = cockpit_panels.load_top_strip(CONFIG)
        self.assertTrue(strip["available"])
        self.assertIsNotNone(strip["session_label"])
        self.assertEqual(strip["last_price_update_display"], "06 Aug 12:00 UTC")
        self.assertIsNone(strip["last_material_event"])
        self.assertIsNone(strip["next_catalyst"])
        self.assertIsNone(strip["regime"])
        self.assertIsNone(strip["budget"])
        self.assertEqual(strip["source_health"]["total"], 2)
        # One failing market query leaves the other chips sub-sources intact.
        self.assertEqual(len(strip["direction_chips"]), 6)

    def test_direction_chips_classify_by_sign_threshold(self):
        import routes.views.cockpit_panels as cockpit_panels

        # market_data_1d: two buckets per symbol (newest first).
        day = datetime(2026, 8, 6, tzinfo=UTC)
        closes = {
            "US10Y": (4.25, 4.20),  # +1.19% -> up
            "DXY": (100.02, 100.00),  # +0.02% -> flat
            "XAUUSD": (2400.0, 2400.0),  # flat
            "WTICOUSD": (78.0, 80.0),  # -2.5% -> down
            "SP500": (5400.0, 5350.0),  # up
        }
        rows = []
        for symbol, (latest, previous) in closes.items():
            rows.append({"symbol": symbol, "bucket": day, "close": latest})
            rows.append(
                {"symbol": symbol, "bucket": day - timedelta(days=1), "close": previous}
            )

        with (
            patch("routes.views.cockpit_panels.query_one", return_value=None),
            patch("routes.views.cockpit_panels.query_many", return_value=rows),
            patch("routes.views.cockpit_panels.get_regime_current", return_value={}),
            patch("routes.views.cockpit_panels.get_budget_status", return_value={}),
        ):
            strip = cockpit_panels.load_top_strip(CONFIG)
        by_key = {chip["key"]: chip for chip in strip["direction_chips"]}
        self.assertEqual(by_key["rates"]["direction"], "up")
        self.assertEqual(by_key["dollar"]["direction"], "flat")
        self.assertEqual(by_key["gold"]["direction"], "flat")
        self.assertEqual(by_key["oil"]["direction"], "down")
        self.assertEqual(by_key["equities"]["direction"], "up")

    def test_vol_proxy_uses_5m_ranges(self):
        import routes.views.cockpit_panels as cockpit_panels

        bucket = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        rows = []
        # Prior window range: 1000..1002 (0.2%); current window: 1000..1004 (0.4%).
        for index in range(12):
            rows.append(
                {
                    "bucket": bucket - timedelta(minutes=5 * index),
                    "high": 1004.0,
                    "low": 1000.0,
                    "close": 1002.0,
                }
            )
        for index in range(12, 24):
            rows.append(
                {
                    "bucket": bucket - timedelta(minutes=5 * index),
                    "high": 1002.0,
                    "low": 1000.0,
                    "close": 1001.0,
                }
            )
        calls = {"count": 0}

        def fake_query_many(sql, params=None, config=None):
            if "market_data_5m" in sql:
                calls["count"] += 1
                return rows
            return []

        with (
            patch("routes.views.cockpit_panels.query_one", return_value=None),
            patch(
                "routes.views.cockpit_panels.query_many", side_effect=fake_query_many
            ),
        ):
            change = cockpit_panels._vol_proxy_change(CONFIG)
        self.assertIsNotNone(change)
        self.assertGreater(change, 0.05)  # range doubled -> up
        self.assertEqual(calls["count"], 1)


class ChangeFeedQueryTests(unittest.TestCase):
    def test_rejects_bad_before_before_database_access(self):
        from fastapi import HTTPException

        import routes.views.cockpit_panels as cockpit_panels

        with patch("routes.views.cockpit_panels.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                cockpit_panels.load_change_feed(CONFIG, before="not-a-timestamp")
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

        with patch("routes.views.cockpit_panels.query_many") as query:
            with self.assertRaises(HTTPException) as caught:
                cockpit_panels.load_change_feed(CONFIG, before="2026-08-06T12:00:00")
        self.assertEqual(caught.exception.status_code, 422)
        query.assert_not_called()

    def test_bounds_limit_and_signals_has_earlier(self):
        import routes.views.cockpit_panels as cockpit_panels

        rows = [feed_row(NOW - timedelta(minutes=index)) for index in range(31)]
        with patch(
            "routes.views.cockpit_panels.query_many", return_value=rows
        ) as query:
            feed = cockpit_panels.load_change_feed(CONFIG, limit=30)
        self.assertTrue(feed["available"])
        self.assertTrue(feed["has_earlier"])
        self.assertEqual(len(feed["rows"]), 30)
        self.assertEqual(query.call_args.kwargs["params"]["limit"], 31)

        with patch("routes.views.cockpit_panels.query_many", return_value=rows[:30]):
            feed = cockpit_panels.load_change_feed(CONFIG, limit=30)
        self.assertFalse(feed["has_earlier"])
        self.assertEqual(len(feed["rows"]), 30)

        many_rows = [feed_row(NOW - timedelta(minutes=index)) for index in range(60)]
        with patch(
            "routes.views.cockpit_panels.query_many", return_value=many_rows
        ) as query:
            feed = cockpit_panels.load_change_feed(CONFIG, limit=999)
        self.assertEqual(feed["limit"], 50)
        self.assertTrue(feed["has_earlier"])
        self.assertEqual(len(feed["rows"]), 50)
        self.assertEqual(query.call_args.kwargs["params"]["limit"], 51)

    def test_row_processing_caps_markets_and_windows(self):
        import routes.views.cockpit_panels as cockpit_panels

        windows = [
            {
                "timeframe": "PRICE" if index % 2 == 0 else "5m",
                "horizon": f"{index}m",
                "percentage_move": 0.1 * index,
                "reaction_state": "persistence",
            }
            for index in range(6)
        ]
        row = feed_row(
            NOW,
            flags=("confirmed_by_market", "market_moved_before_headline"),
            windows=windows,
        )
        with patch("routes.views.cockpit_panels.query_many", return_value=[row]):
            feed = cockpit_panels.load_change_feed(CONFIG, limit=30)
        processed = feed["rows"][0]
        self.assertEqual(processed["title"], "Fed holds rates")
        self.assertEqual(processed["source"], "reuters")
        self.assertEqual(processed["markets"], ["EURUSD", "SP500", "XAUUSD", "DXY"])
        self.assertEqual(len(processed["reaction_windows"]), 4)
        self.assertEqual(processed["reaction_windows"][0]["horizon"], "0m")
        self.assertEqual(processed["reaction_windows"][0]["timeframe"], "PRICE")
        self.assertEqual(processed["state"], "confirmed")
        self.assertEqual(processed["state_class"], "bullish")
        self.assertTrue(processed["interpretation_available"])
        self.assertEqual(processed["importance_label"], "High")
        self.assertEqual(processed["novelty_display"], "0.50")
        self.assertEqual(processed["observed_at"], NOW.isoformat())

    def test_title_falls_back_to_event_type(self):
        import routes.views.cockpit_panels as cockpit_panels

        raw = feed_row(NOW)
        raw["payload"] = {}
        raw["event_type"] = "central_bank_rate"
        with patch("routes.views.cockpit_panels.query_many", return_value=[raw]):
            feed = cockpit_panels.load_change_feed(CONFIG)
        self.assertEqual(feed["rows"][0]["title"], "central_bank_rate")

    def test_state_mapping_from_confirmation_flags(self):
        import routes.views.cockpit_panels as cockpit_panels

        cases = {
            "developing": (),
            "confirmed": ("confirmed_by_market",),
            "contradicted": ("initial_move_reversed",),
            "completed": ("no_material_reaction",),
        }
        for expected, flags in cases.items():
            with patch(
                "routes.views.cockpit_panels.query_many",
                return_value=[feed_row(NOW, flags=flags)],
            ):
                feed = cockpit_panels.load_change_feed(CONFIG)
            self.assertEqual(feed["rows"][0]["state"], expected)

    def test_query_failure_is_fail_soft(self):
        import routes.views.cockpit_panels as cockpit_panels

        with patch(
            "routes.views.cockpit_panels.query_many",
            side_effect=RuntimeError("secret sql"),
        ):
            feed = cockpit_panels.load_change_feed(CONFIG)
        self.assertFalse(feed["available"])
        self.assertEqual(feed["rows"], [])
        self.assertNotIn("secret sql", str(feed))


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
    def test_partials_require_auth(self):
        from fastapi.testclient import TestClient

        app = make_app(auth=True)
        client = TestClient(app)
        with patch(
            "routes.views.cockpit_panels.load_top_strip",
            return_value={"available": True},
        ):
            self.assertEqual(
                client.get("/partials/dashboard/top-strip").status_code, 401
            )
            with patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ):
                response = client.get("/partials/dashboard/top-strip", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Session snapshot", response.text)

    def test_top_strip_route_renders(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        strip = {
            "available": True,
            "session_label": "London",
            "last_price_update_display": "06 Aug 12:00 UTC",
            "last_material_event": None,
            "regime": None,
            "direction_chips": [
                {
                    "key": "rates",
                    "label": "Rates",
                    "display": "+1.19%",
                    "direction": "up",
                }
            ],
            "next_catalyst": None,
            "source_health": None,
            "budget": None,
        }
        with (
            patch("routes.views.cockpit_panels.load_top_strip", return_value=strip),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/top-strip")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Session snapshot", response.text)
        self.assertIn("London", response.text)
        self.assertIn("+1.19%", response.text)
        # Fallback polling contract when live updates are disabled.
        self.assertIn('hx-get="/partials/dashboard/top-strip"', response.text)
        self.assertIn('hx-trigger="every 90s"', response.text)
        self.assertNotIn("data-live-section", response.text)

    def test_live_attrs_vs_hx_fallback_wrapper(self):
        from fastapi.testclient import TestClient

        import routes.views.cockpit_panels as cockpit_panels

        app = make_app()
        client = TestClient(app)
        feed = {
            "available": True,
            "rows": [cockpit_panels._feed_row(feed_row(NOW))],
            "has_earlier": False,
            "limit": 30,
            "oldest_observed_at": NOW.isoformat(),
        }
        live_config = {"event_pipeline": {"sse": {"enabled": True}}}
        with (
            patch("routes.views.cockpit_panels.load_change_feed", return_value=feed),
            patch(
                "routes.views.cockpit_panels.app_config.load_config",
                return_value=live_config,
            ),
        ):
            live = client.get("/partials/dashboard/change-feed")
        self.assertIn('data-live-section="change_feed"', live.text)
        self.assertIn('data-live-event="section_changed"', live.text)
        self.assertIn('data-live-url="/partials/dashboard/change-feed"', live.text)
        with (
            patch("routes.views.cockpit_panels.load_change_feed", return_value=feed),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            polling = client.get("/partials/dashboard/change-feed")
        self.assertIn('hx-get="/partials/dashboard/change-feed"', polling.text)
        self.assertIn('hx-trigger="every 90s"', polling.text)
        self.assertNotIn("data-live-section", polling.text)

    def test_change_feed_renders_reaction_windows_and_load_earlier(self):
        from urllib.parse import quote

        from fastapi.testclient import TestClient

        import routes.views.cockpit_panels as cockpit_panels

        app = make_app()
        client = TestClient(app)
        feed = {
            "available": True,
            "rows": [
                cockpit_panels._feed_row(feed_row(NOW, flags=("confirmed_by_market",)))
            ],
            "has_earlier": True,
            "limit": 30,
            "oldest_observed_at": NOW.isoformat(),
        }
        with (
            patch("routes.views.cockpit_panels.load_change_feed", return_value=feed),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/change-feed")
        self.assertIn("Fed holds rates", response.text)
        self.assertIn("5m", response.text)
        self.assertIn("+0.40%", response.text)
        self.assertIn("confirmed", response.text)
        self.assertIn("interpretation", response.text)
        self.assertIn("Load earlier", response.text)
        self.assertIn('hx-swap="afterend"', response.text)
        self.assertIn("before=" + quote(NOW.isoformat(), safe=""), response.text)

        feed["has_earlier"] = False
        with (
            patch("routes.views.cockpit_panels.load_change_feed", return_value=feed),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/change-feed")
        self.assertNotIn("Load earlier", response.text)

    def test_change_feed_rejects_bad_before_with_422_pre_db(self):
        from fastapi.testclient import TestClient

        app = make_app()
        client = TestClient(app)
        with (
            patch("routes.views.cockpit_panels.query_many") as query,
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get("/partials/dashboard/change-feed?before=garbage")
        self.assertEqual(response.status_code, 422)
        query.assert_not_called()

    def test_append_mode_returns_fragment_not_section(self):
        from fastapi.testclient import TestClient

        import routes.views.cockpit_panels as cockpit_panels

        app = make_app()
        client = TestClient(app)
        feed = {
            "available": True,
            "rows": [cockpit_panels._feed_row(feed_row(NOW - timedelta(days=1)))],
            "has_earlier": False,
            "limit": 30,
            "oldest_observed_at": (NOW - timedelta(days=1)).isoformat(),
        }
        with (
            patch("routes.views.cockpit_panels.load_change_feed", return_value=feed),
            patch(
                "routes.views.cockpit_panels.app_config.load_config", return_value={}
            ),
        ):
            response = client.get(
                "/partials/dashboard/change-feed", params={"before": NOW.isoformat()}
            )
        self.assertNotIn("data-live-section", response.text)
        self.assertNotIn("<section", response.text)
        self.assertIn("change-feed-list", response.text)

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
