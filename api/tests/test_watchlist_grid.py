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

WATCHLIST_CONFIG = {
    "watchlist": {
        "trading": [
            {"symbol": "EURUSD", "type": "forex"},
            {"symbol": "XAUUSD", "type": "metal"},
        ]
    }
}


def market_row(
    symbol,
    *,
    price=1.2000,
    timestamp=None,
    prior_5m=1.1950,
    day_open=1.1900,
    samples=42,
    recent=100.0,
    prior=50.0,
):
    return {
        "symbol": symbol,
        "last_price": price,
        "last_timestamp": timestamp or NOW - timedelta(minutes=2),
        "prior_5m_close": prior_5m,
        "day_open": day_open,
        "day_bucket": datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        "samples": samples,
        "vol_recent_range": recent,
        "vol_prior_range": prior,
    }


def catalyst_row(symbol, *, title="CPI print", horizon="15m", state="pending"):
    return {
        "instrument_symbol": symbol,
        "timeframe": "PRICE",
        "horizon": horizon,
        "reaction_state": state,
        "target_at": NOW + timedelta(minutes=10),
        "event_at": NOW - timedelta(minutes=5),
        "event_type": "macro_release",
        "event_title": title,
    }


def opinion_row(scope, direction="bullish"):
    return {
        "scope": scope,
        "direction": direction,
        "created_at": NOW - timedelta(hours=3),
        "published_at": NOW - timedelta(hours=3),
    }


def atom_row(claim="Bullish momentum building.", **overrides):
    atom = {
        "id": "11111111-1111-4111-8111-111111111111",
        "subject_type": "market",
        "subject_id": "EURUSD",
        "claim_type": "event_interpretation",
        "claim": claim,
        "interpretation_text": None,
        "observation_text": None,
        "published_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    atom.update(overrides)
    return atom


def summary_row(**overrides):
    row = {
        "last_price": 1.2000,
        "last_timestamp": NOW - timedelta(minutes=1),
        "day_open": 1.1900,
        "prior_day_close": 1.1800,
        "vol_recent_range": 50.0,
        "vol_prior_range": 50.0,
    }
    row.update(overrides)
    return row


def bucket_row(minutes_ago, close):
    return {"bucket": NOW - timedelta(minutes=minutes_ago), "close": close}


def build_app():
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates

    from routes.views.watchlist_grid import router

    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
    app.include_router(router)
    return app


class WatchlistGridQueryTests(unittest.TestCase):
    def test_invalid_view_sort_direction_rejected_before_database(self):
        from routes.views.watchlist_grid import load_watchlist_grid

        with patch("routes.views.watchlist_grid.query_many") as query:
            with self.assertRaises(ValueError):
                load_watchlist_grid({}, view="bogus")
            with self.assertRaises(ValueError):
                load_watchlist_grid({}, sort="nope")
            with self.assertRaises(ValueError):
                load_watchlist_grid({}, direction="sideways")
        query.assert_not_called()

    def test_database_failure_is_fail_soft_and_generic(self):
        from routes.views.watchlist_grid import load_watchlist_grid

        with patch(
            "routes.views.watchlist_grid.query_many",
            side_effect=RuntimeError("secret sql"),
        ):
            payload = load_watchlist_grid(WATCHLIST_CONFIG)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["rows"], [])
        self.assertNotIn("secret sql", str(payload))

    def test_runtime_config_watchlist_models_resolve_dashboard_symbols(self):
        from contracts.runtime_config import WatchlistConfig
        from routes.views.watchlist_grid import load_watchlist_grid

        config = {
            "watchlist": WatchlistConfig.model_validate(
                {
                    "trading": [{"symbol": "EURUSD", "type": "forex"}],
                    "investing": {
                        "watchlists": [{"name": "funds", "symbols": ["SPY"]}]
                    },
                }
            )
        }
        with (
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[[market_row("EURUSD"), market_row("SPY")], [], []],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"status": "published", "atoms": []},
            ),
        ):
            payload = load_watchlist_grid(config)

        self.assertEqual(
            {row["symbol"] for row in payload["rows"]}, {"EURUSD", "SPY"}
        )

    def test_grid_renders_one_row_per_symbol_with_moves_and_vol(self):
        from routes.views.watchlist_grid import load_watchlist_grid

        market_rows = [
            market_row("EURUSD"),
            market_row(
                "XAUUSD",
                price=2450.5,
                prior_5m=2460.0,
                day_open=2430.0,
                recent=10.0,
                prior=20.0,
            ),
        ]
        catalyst_rows = [catalyst_row("EURUSD")]
        opinion_rows = [opinion_row("asset:EURUSD", "bearish")]
        with (
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[market_rows, catalyst_rows, opinion_rows],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"status": "published", "atoms": []},
            ),
        ):
            payload = load_watchlist_grid(WATCHLIST_CONFIG)
        self.assertTrue(payload["available"])
        self.assertEqual(
            {row["symbol"] for row in payload["rows"]}, {"EURUSD", "XAUUSD"}
        )
        eurusd = next(row for row in payload["rows"] if row["symbol"] == "EURUSD")
        xau = next(row for row in payload["rows"] if row["symbol"] == "XAUUSD")

        # 5m/day pct moves
        self.assertAlmostEqual(eurusd["chg_5m"], 0.42, places=2)
        self.assertAlmostEqual(eurusd["chg_day"], 0.84, places=2)
        self.assertEqual(eurusd["chg_5m_text"], "+0.42%")
        # vol state from 12-vs-12 bucket range ratio
        self.assertEqual(eurusd["vol_state"], "elevated")  # 100 / 50
        self.assertEqual(xau["vol_state"], "quiet")  # 10 / 20
        # catalyst from reaction windows
        self.assertEqual(eurusd["catalyst_text"], "CPI print · 15m")
        self.assertEqual(eurusd["catalyst_timeframe"], "PRICE")
        # freshness cells: ages are vs wall-clock now, so assert the
        # deterministic relationship (analysis opinion is 178m older than the
        # 2m-old price tick) rather than absolute values.
        self.assertIsNotNone(eurusd["price_age_minutes"])
        self.assertIsNotNone(eurusd["analysis_age_minutes"])
        self.assertGreaterEqual(eurusd["price_age_minutes"], 0)
        self.assertEqual(
            eurusd["analysis_age_minutes"] - eurusd["price_age_minutes"], 178
        )
        self.assertIsNotNone(eurusd["price_age_text"])
        self.assertIsNotNone(eurusd["analysis_age_text"])
        self.assertIn("2026-08-06", eurusd["price_iso"])
        self.assertIn("2026-08-06", eurusd["analysis_iso"])
        # interpretation from published opinion, never an action word
        self.assertEqual(eurusd["interpretation"], "bearish")
        self.assertEqual(xau["interpretation"], "neutral")
        for row in payload["rows"]:
            label = row["interpretation"].lower()
            self.assertIn(label, {"neutral", "mixed", "bullish", "bearish"})
            for word in ("buy", "sell", "long", "short"):
                self.assertNotIn(word, label)

    def test_atom_fallback_maps_claim_to_interpretation(self):
        from routes.views.watchlist_grid import load_watchlist_grid

        market_rows = [market_row("EURUSD"), market_row("XAUUSD")]
        with (
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[market_rows, [], []],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                side_effect=[
                    {"status": "published", "atoms": [atom_row("Bullish momentum.")]},
                    {"status": "published", "atoms": [atom_row("Weakening growth.")]},
                ],
            ),
        ):
            payload = load_watchlist_grid(WATCHLIST_CONFIG)
        by_symbol = {row["symbol"]: row for row in payload["rows"]}
        self.assertEqual(by_symbol["EURUSD"]["interpretation"], "bullish")
        self.assertEqual(by_symbol["XAUUSD"]["interpretation"], "bearish")
        for row in payload["rows"]:
            for word in ("buy", "sell", "long", "short"):
                self.assertNotIn(word, row["interpretation"].lower())

    def test_rows_sort_in_python_with_none_last(self):
        from routes.views.watchlist_grid import load_watchlist_grid

        config = {
            "watchlist": {
                "trading": [
                    {"symbol": "EURUSD", "type": "forex"},
                    {"symbol": "XAUUSD", "type": "metal"},
                    {"symbol": "GER40", "type": "index"},
                ]
            }
        }
        # GER40 has no market row at all -> all metrics None
        market_rows = [
            market_row("EURUSD"),
            market_row("XAUUSD", price=2450.5, prior_5m=2460.0),
        ]
        with (
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[market_rows, [], []] * 2,
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"atoms": []},
            ),
        ):
            payload = load_watchlist_grid(config, sort="chg_5m", direction="desc")
            symbols = [row["symbol"] for row in payload["rows"]]
            self.assertEqual(symbols, ["EURUSD", "XAUUSD", "GER40"])
            self.assertIsNone(payload["rows"][-1]["last_price"])
            payload = load_watchlist_grid(config, sort="symbol", direction="asc")
            self.assertEqual(
                [row["symbol"] for row in payload["rows"]],
                ["EURUSD", "GER40", "XAUUSD"],
            )

    def test_unknown_drawer_symbol_rejected_without_database(self):
        from routes.views.watchlist_grid import load_asset_drawer

        with patch("routes.views.watchlist_grid.query_many") as query:
            with self.assertRaises(ValueError):
                load_asset_drawer(WATCHLIST_CONFIG, "NOPE")
            with self.assertRaises(ValueError):
                load_asset_drawer(WATCHLIST_CONFIG, "eur usd!")
        query.assert_not_called()

    def test_drawer_includes_atom_conditions_and_empty_notes(self):
        from routes.views.watchlist_grid import load_asset_drawer

        atom = atom_row("Hot CPI print supports the dollar.")
        atom_detail = {
            "id": atom["id"],
            "invalidation_conditions": ["CPI cools below 2%", "Fed turns dovish"],
            "created_at": NOW - timedelta(hours=2),
        }
        with (
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[
                    [bucket_row(5, 1.1950), bucket_row(0, 1.2000)],
                    [],  # related events
                    [atom_detail],  # atom enrichment
                ],
            ),
            patch("routes.views.watchlist_grid.query_one", return_value=summary_row()),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"status": "published", "atoms": [atom]},
            ),
            patch(
                "routes.views.watchlist_grid.get_events_upcoming_data",
                return_value={"events": []},
            ),
            patch(
                "routes.views.watchlist_grid.get_macro_dashboard",
                return_value={"indicators": []},
            ),
        ):
            drawer = load_asset_drawer(WATCHLIST_CONFIG, "EURUSD")
        self.assertTrue(drawer["available"])
        self.assertEqual(len(drawer["atoms"]), 1)
        self.assertEqual(
            drawer["atoms"][0]["invalidation_conditions"],
            ["CPI cools below 2%", "Fed turns dovish"],
        )
        self.assertEqual(drawer["notes"], [])
        self.assertEqual(drawer["chart_point_count"], 2)
        self.assertEqual(drawer["session_change_text"], "+0.84%")
        self.assertEqual(drawer["day_change_text"], "+1.69%")
        self.assertEqual(drawer["vol_state"], "normal")

    def test_drawer_without_atoms_keeps_notes_placeholder(self):
        from routes.views.watchlist_grid import load_asset_drawer

        with (
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[
                    [bucket_row(5, 1.1950)],
                    [],  # related events
                    [],  # atom enrichment
                ],
            ),
            patch("routes.views.watchlist_grid.query_one", return_value=summary_row()),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"status": "published", "atoms": []},
            ),
            patch(
                "routes.views.watchlist_grid.get_events_upcoming_data",
                return_value={"events": []},
            ),
            patch(
                "routes.views.watchlist_grid.get_macro_dashboard",
                return_value={"indicators": []},
            ),
        ):
            drawer = load_asset_drawer(WATCHLIST_CONFIG, "EURUSD")
        self.assertEqual(drawer["atoms"], [])
        self.assertEqual(drawer["notes"], [])


class WatchlistGridRouteTests(unittest.TestCase):
    def test_grid_route_rejects_invalid_params_before_database(self):
        from fastapi.testclient import TestClient

        app = build_app()
        with patch("routes.views.watchlist_grid.query_many") as query:
            client = TestClient(app)
            self.assertEqual(
                client.get("/partials/dashboard/watchlist-grid?view=bogus").status_code,
                422,
            )
            self.assertEqual(
                client.get("/partials/dashboard/watchlist-grid?sort=nope").status_code,
                422,
            )
            self.assertEqual(
                client.get(
                    "/partials/dashboard/watchlist-grid?direction=sideways"
                ).status_code,
                422,
            )
        query.assert_not_called()

    def test_grid_route_renders_rows_with_freshness_cells(self):
        from fastapi.testclient import TestClient

        app = build_app()
        market_rows = [market_row("EURUSD"), market_row("XAUUSD")]
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[
                    market_rows,
                    [],
                    [
                        opinion_row("asset:EURUSD", "bullish"),
                        opinion_row("asset:XAUUSD", "bearish"),
                    ],
                ],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"atoms": []},
            ),
        ):
            response = TestClient(app).get("/partials/dashboard/watchlist-grid")
        self.assertEqual(response.status_code, 200)
        self.assertIn("EURUSD", response.text)
        self.assertIn("XAUUSD", response.text)
        self.assertEqual(response.text.count('data-freshness-cell="price"'), 2)
        self.assertEqual(response.text.count('data-freshness-cell="analysis"'), 2)
        self.assertIn('title="2026-08-06T', response.text)
        self.assertIn("story-state story-state-confirmed", response.text)  # bullish
        self.assertIn("story-state story-state-contradicted", response.text)  # bearish
        lowered = response.text.lower()
        for word in ("buy", "sell", "long", "short"):
            self.assertNotIn(word, lowered)

    def test_grid_route_empty_view_renders_empty_state_row(self):
        from fastapi.testclient import TestClient

        app = build_app()
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value={"watchlist": {}},
            ),
            patch("routes.views.watchlist_grid.query_many") as query,
        ):
            response = TestClient(app).get("/partials/dashboard/watchlist-grid")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No instruments with data in this view.", response.text)
        query.assert_not_called()

    def test_drawer_route_unknown_symbol_404_without_database(self):
        from fastapi.testclient import TestClient

        app = build_app()
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch("routes.views.watchlist_grid.query_many") as query,
        ):
            response = TestClient(app).get("/partials/dashboard/asset/NOPE")
        self.assertEqual(response.status_code, 404)
        query.assert_not_called()

    def test_drawer_route_db_failure_returns_503_without_sql_text(self):
        from fastapi.testclient import TestClient

        app = build_app()
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=RuntimeError("secret sql"),
            ),
        ):
            response = TestClient(app).get("/partials/dashboard/asset/EURUSD")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret sql", response.text)

    def test_drawer_route_renders_conditions_and_notes_empty_state(self):
        from fastapi.testclient import TestClient

        app = build_app()
        atom = atom_row("Hot CPI print supports the dollar.")
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[
                    [bucket_row(5, 1.1950), bucket_row(0, 1.2000)],
                    [],  # related events
                    [
                        {
                            "id": atom["id"],
                            "invalidation_conditions": ["CPI cools below 2%"],
                            "created_at": NOW - timedelta(hours=2),
                        }
                    ],
                ],
            ),
            patch("routes.views.watchlist_grid.query_one", return_value=summary_row()),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"status": "published", "atoms": [atom]},
            ),
            patch(
                "routes.views.watchlist_grid.get_events_upcoming_data",
                return_value={"events": []},
            ),
            patch(
                "routes.views.watchlist_grid.get_macro_dashboard",
                return_value={"indicators": []},
            ),
        ):
            response = TestClient(app).get("/partials/dashboard/asset/EURUSD")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="expansion-panel"', response.text)
        self.assertIn('data-symbol="EURUSD"', response.text)
        self.assertIn("CPI cools below 2%", response.text)
        self.assertIn("What would change this view", response.text)
        self.assertIn("No operator notes yet", response.text)
        self.assertIn('data-chart="asset-intraday"', response.text)

    def test_grid_partial_has_sse_identity_and_market_refresh_contract(self):
        from fastapi.testclient import TestClient

        app = build_app()
        market_rows = [market_row("EURUSD")]
        live_config = dict(WATCHLIST_CONFIG)
        live_config["event_pipeline"] = {"sse": {"enabled": True}}
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=live_config,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[market_rows, [], []],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"atoms": []},
            ),
        ):
            live = TestClient(app).get("/partials/dashboard/watchlist-grid")
        self.assertIn('data-live-section="watchlist_grid"', live.text)
        self.assertIn('data-live-event="section_changed"', live.text)
        self.assertIn('data-live-url="/partials/dashboard/watchlist-grid"', live.text)
        # SSE sections declare only data-live attributes: no self-fetch and
        # no marketRefresh/poll trigger on the section.
        self.assertNotIn('hx-get="/partials/dashboard/watchlist-grid"', live.text)
        self.assertNotIn("hx-trigger", live.text)
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[market_rows, [], []],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"atoms": []},
            ),
        ):
            polling = TestClient(app).get("/partials/dashboard/watchlist-grid")
        # Non-SSE refresh is the shared marketRefresh heartbeat, never a poll.
        self.assertIn('hx-get="/partials/dashboard/watchlist-grid"', polling.text)
        self.assertIn('hx-trigger="marketRefresh from:body"', polling.text)
        self.assertNotIn("every 90s", polling.text)
        self.assertNotIn('data-live-section="watchlist_grid"', polling.text)

    def test_grid_renders_one_row_per_symbol_with_keyboard_asset_buttons(self):
        from fastapi.testclient import TestClient

        app = build_app()
        market_rows = [market_row("EURUSD"), market_row("XAUUSD")]
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[market_rows, [], []],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"atoms": []},
            ),
        ):
            response = TestClient(app).get("/partials/dashboard/watchlist-grid")
        self.assertEqual(response.status_code, 200)
        # Exactly one representation per symbol: one row control and one
        # drawer endpoint per symbol, no duplicated rows or data source.
        self.assertEqual(response.text.count('class="asset-row-link"'), 2)
        self.assertEqual(
            response.text.count('hx-get="/partials/dashboard/asset/EURUSD"'), 1
        )
        self.assertEqual(
            response.text.count('hx-get="/partials/dashboard/asset/XAUUSD"'), 1
        )
        # Semantic table headings: caption plus scope'd column/row headers.
        self.assertIn('<caption class="sr-only">', response.text)
        self.assertEqual(response.text.count('scope="col"'), 8)
        self.assertEqual(response.text.count("scope=\"row\""), 2)
        # Each row control is a native button (keyboard-operable) targeting
        # the single shared drawer panel.
        self.assertEqual(response.text.count('type="button"'), 2)
        self.assertEqual(response.text.count('hx-target="#expansion-panel"'), 2)

    def test_grid_cells_carry_mobile_labels(self):
        from fastapi.testclient import TestClient

        app = build_app()
        with (
            patch(
                "routes.views.watchlist_grid.app_config.load_config",
                return_value=WATCHLIST_CONFIG,
            ),
            patch(
                "routes.views.watchlist_grid.query_many",
                side_effect=[[market_row("EURUSD")], [], []],
            ),
            patch(
                "routes.views.watchlist_grid.load_atom_context",
                return_value={"atoms": []},
            ),
        ):
            response = TestClient(app).get("/partials/dashboard/watchlist-grid")
        for label in (
            "Last",
            "5m",
            "Day",
            "Vol state",
            "Current catalyst",
            "Interpretation",
            "Price age",
            "Analysis age",
        ):
            self.assertIn(f'data-label="{label}"', response.text)
        # Values are rendered once per symbol: no duplicated mobile markup.
        # EURUSD appears exactly twice: the button label and the drawer URL.
        self.assertEqual(response.text.count("EURUSD"), 2)
        self.assertEqual(response.text.count(">1.2<"), 1)

    def test_mobile_layout_stacks_grid_without_page_overflow(self):
        css = (API_ROOT / "static/style.css").read_text()
        # Page-level guard: html/body never scroll horizontally.
        self.assertIn("overflow-x: hidden", css)
        # Desktop: the grid scrolls inside its contained wrapper.
        self.assertIn(".watchlist-scroll", css)
        self.assertIn("overflow-x: auto", css)
        # Mobile: rows reflow into labelled stacks; the forced table width is
        # reset so the reflowed content cannot widen the page at 390px.
        self.assertIn("@media (max-width: 640px)", css)
        self.assertIn("attr(data-label)", css)
        self.assertIn(".watchlist-grid-section table { min-width: 0; }", css)


class WatchlistGridAuthTests(unittest.TestCase):
    def test_dashboard_partials_require_auth(self):
        from fastapi import Depends, FastAPI
        from fastapi.templating import Jinja2Templates
        from fastapi.testclient import TestClient

        from auth import verify_credentials
        from routes.views.watchlist_grid import router

        app = FastAPI(dependencies=[Depends(verify_credentials)])
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        client = TestClient(app)
        self.assertEqual(
            client.get("/partials/dashboard/watchlist-grid").status_code, 401
        )
        self.assertEqual(
            client.get("/partials/dashboard/asset/EURUSD").status_code, 401
        )


if __name__ == "__main__":
    unittest.main()
