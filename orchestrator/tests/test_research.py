import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

ORCH_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ORCH_ROOT.parent / "api"
# Orchestrator root first: the main process runs orchestrator modules that
# import the orchestrator ``db``.  The api application is imported only inside
# the ResearchApiApiTests subprocess, which temporarily puts api/ first.
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(ORCH_ROOT))

os.environ.update(
    {
        "STATE_DIR": "/tmp/trading-data-research-test-state",
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
        "DEPLOYMENT_MODE": "test",
        "LEGACY_BASIC_AUTH": "1",
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
    add_catalyst,
    add_risk,
    add_thesis_evidence,
    add_watch_item,
    attach_theme_entities,
    create_theme,
    create_thesis,
    get_dossier,
    get_theme,
    list_themes,
    portfolio_context,
    revise_thesis,
    set_theme_status,
    set_thesis_status,
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
            raise AssertionError(f"unexpected SQL call: {statement}")
        return self.results.pop(0)


def theme_row(**overrides):
    value = {
        "id": THEME_ID,
        "name": "AI Compute",
        "definition": "Semiconductor demand supercycle.",
        "horizon": "multi_year",
        "macro_drivers": ["capex"],
        "key_indicators": ["CPIAUCSL"],
        "status": "active",
        "review_at": None,
        "invalidation_conditions": [],
        "confidence": 0.7,
        "confidence_components": {},
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
        "variant_perception": "Consensus lags.",
        "status": "active",
        "horizon": "multi_year",
        "review_at": None,
        "confidence": 0.7,
        "created_at": NOW,
        "updated_at": NOW,
    }
    value.update(overrides)
    return value


class ThemeHelperTests(unittest.TestCase):
    def test_duplicate_theme_name_raises_value_error(self):
        session = Session(
            [
                Result(first=None),  # duplicate probe
                Result(first={"id": THEME_ID}),  # INSERT RETURNING id
            ]
        )
        theme_id = create_theme(
            session,
            name="AI Compute",
            definition="Semiconductor demand supercycle.",
            key_indicators=["CPIAUCSL"],
        )
        self.assertEqual(theme_id, str(THEME_ID))
        insert_sql = session.calls[1][0]
        self.assertIn("INSERT INTO investment_themes", insert_sql)
        session.commit.assert_not_called()

        duplicate = Session([Result(first={"present": 1})])
        with self.assertRaisesRegex(ValueError, "duplicate theme"):
            create_theme(duplicate, name="AI Compute", definition="Another definition.")
        self.assertEqual(len(duplicate.calls), 1)
        duplicate.commit.assert_not_called()

    def test_attach_entities_is_idempotent_and_enum_validated(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(),  # INSERT ... ON CONFLICT DO NOTHING
            ]
        )
        count = attach_theme_entities(
            session,
            str(THEME_ID),
            [
                {
                    "entity_type": "macro_series",
                    "entity_id": "CPIAUCSL",
                    "display_name": "CPI",
                },
                {
                    "entity_type": "macro_series",
                    "entity_id": "CPIAUCSL",
                    "display_name": "CPI",
                },
            ],
        )
        self.assertEqual(count, 2)
        insert_sql = session.calls[1][0]
        self.assertIn("ON CONFLICT (theme_id, entity_type, entity_id)", insert_sql)

        invalid = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported entity_type"):
            attach_theme_entities(
                invalid,
                str(THEME_ID),
                [{"entity_type": "provider_secret", "entity_id": "x"}],
            )
        self.assertEqual(invalid.calls, [])


class ThesisHelperTests(unittest.TestCase):
    def test_create_thesis_writes_initial_version(self):
        session = Session(
            [
                Result(first={"present": 1}),  # theme exists
                Result(first={"id": THESIS_ID}),  # thesis INSERT RETURNING id
                Result(),  # version INSERT
            ]
        )
        thesis_id = create_thesis(
            session,
            theme_id=str(THEME_ID),
            company="Nvidia Corp",
            claim="AI capex compounds.",
            rationale="Founding rationale.",
        )
        self.assertEqual(thesis_id, str(THESIS_ID))
        version_sql = session.calls[2][0]
        self.assertIn("INSERT INTO investment_thesis_versions", version_sql)
        self.assertIn(", 1, :claim", version_sql)
        session.commit.assert_not_called()

    def test_revision_bumps_version_and_preserves_status(self):
        session = Session(
            [
                Result(first={"status": "active"}),  # thesis exists
                Result(first={"max_version": 2}),  # current max version
                Result(),  # UPDATE theses row
                Result(),  # INSERT new version
            ]
        )
        version = revise_thesis(
            session,
            str(THESIS_ID),
            claim="Revised claim after new evidence.",
            rationale="Q2 filing changed the picture.",
        )
        self.assertEqual(version, 3)
        update_sql = session.calls[2][0]
        self.assertIn("UPDATE investment_theses", update_sql)
        self.assertNotIn("status", update_sql)
        insert_params = session.calls[3][1]
        self.assertEqual(insert_params["version"], 3)
        self.assertEqual(insert_params["changed_by"], "operator")
        session.commit.assert_not_called()

    def test_invalid_relationship_rejected_pre_insert_without_rows(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported_relationship"):
            add_thesis_evidence(
                session,
                str(THESIS_ID),
                evidence=[
                    {
                        "evidence_type": "macro_series",
                        "evidence_id": "CPIAUCSL",
                        "relationship": "boosts",
                    },
                    {
                        "evidence_type": "macro_series",
                        "evidence_id": "UNRATE",
                        "relationship": "supports",
                    },
                ],
            )
        self.assertEqual(session.calls, [])

    def test_unsupported_evidence_type_rejected_pre_insert(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported_evidence_type"):
            add_thesis_evidence(
                session,
                str(THESIS_ID),
                evidence=[
                    {
                        "evidence_type": "provider_secret",
                        "evidence_id": "x",
                        "relationship": "supports",
                    }
                ],
            )
        self.assertEqual(session.calls, [])

    def test_valid_evidence_inserts_idempotently(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(),  # INSERT ... ON CONFLICT DO NOTHING
            ]
        )
        count = add_thesis_evidence(
            session,
            str(THESIS_ID),
            evidence=[
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "supports",
                    "excerpt": "CPI cooled.",
                },
                {
                    "evidence_type": "atom",
                    "evidence_id": str(uuid4()),
                    "relationship": "contradicts",
                },
            ],
        )
        self.assertEqual(count, 2)
        insert_sql = session.calls[1][0]
        self.assertIn("INSERT INTO investment_thesis_evidence", insert_sql)
        self.assertIn("ON CONFLICT", insert_sql)
        self.assertEqual(len(session.calls[1][1]), 2)

    def test_invalidation_relationship_accepted_by_legacy_path(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(),  # INSERT ... ON CONFLICT DO NOTHING
            ]
        )
        count = add_thesis_evidence(
            session,
            str(THESIS_ID),
            evidence=[
                {
                    "evidence_type": "macro_series",
                    "evidence_id": "CPIAUCSL",
                    "relationship": "invalidation",
                    "excerpt": "CPI target invalidated.",
                }
            ],
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            session.calls[1][1][0]["relationship"], "invalidation"
        )
        session.commit.assert_not_called()

    def test_unknown_thesis_rejected(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown thesis"):
            revise_thesis(session, str(THESIS_ID), claim="x", rationale="y")
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown thesis"):
            add_watch_item(session, str(THESIS_ID), label="Watch the guide")


class ChildObjectTests(unittest.TestCase):
    def test_catalyst_state_validated_before_db(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported catalyst state"):
            add_catalyst(session, str(THESIS_ID), description="FOMC", state="bogus")
        self.assertEqual(session.calls, [])

    def test_risk_kind_and_severity_validated_before_db(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported risk kind"):
            add_risk(session, str(THESIS_ID), description="d", kind="bogus")
        with self.assertRaisesRegex(ValueError, "unsupported risk severity"):
            add_risk(session, str(THESIS_ID), description="d", severity="bogus")
        self.assertEqual(session.calls, [])

    def test_catalyst_and_risk_insert_return_ids(self):
        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"id": ATOM_ID}),
            ]
        )
        catalyst_id = add_catalyst(
            session,
            str(THESIS_ID),
            description="FOMC decision",
            expected_at="2026-09-16T18:00:00Z",
        )
        self.assertEqual(catalyst_id, str(ATOM_ID))
        self.assertIn("INSERT INTO investment_catalysts", session.calls[1][0])
        session = Session(
            [
                Result(first={"present": 1}),
                Result(first={"id": ATOM_ID}),
            ]
        )
        risk_id = add_risk(session, str(THESIS_ID), description="Rate shock")
        self.assertEqual(risk_id, str(ATOM_ID))
        self.assertIn("INSERT INTO investment_risks", session.calls[1][0])

    def test_status_setters_validate_before_db(self):
        session = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported thesis status"):
            set_thesis_status(session, str(THESIS_ID), "bogus")
        with self.assertRaisesRegex(ValueError, "unsupported theme status"):
            set_theme_status(session, str(THEME_ID), "bogus")
        self.assertEqual(session.calls, [])

    def test_status_setters_update_unknown_ids_raise(self):
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown thesis"):
            set_thesis_status(session, str(THESIS_ID), "closed")
        session = Session([Result(first=None)])
        with self.assertRaisesRegex(ValueError, "unknown theme"):
            set_theme_status(session, str(THEME_ID), "paused")


class PortfolioTests(unittest.TestCase):
    def test_weights_clamped_and_sources_validated(self):
        session = Session([Result()])
        count = upsert_holdings(
            session,
            holdings=[
                {
                    "symbol": "NVDA",
                    "sector": "Semis",
                    "country": "US",
                    "currency": "USD",
                    "weight": 0.4,
                    "source": "manual",
                },
                {
                    "symbol": "MSFT",
                    "sector": "Semis",
                    "country": "US",
                    "currency": "USD",
                    "weight": 0.6,
                    "source": "import",
                },
                {
                    "symbol": "TLT",
                    "sector": "Rates",
                    "country": "US",
                    "currency": "USD",
                    "weight": 1.5,
                },
                {
                    "symbol": "CASH",
                    "sector": "Cash",
                    "country": "US",
                    "currency": "USD",
                    "weight": -0.2,
                },
            ],
        )
        self.assertEqual(count, 4)
        params = session.calls[0][1]
        weights = {row["symbol"]: row["weight"] for row in params}
        self.assertEqual(weights["NVDA"], 0.4)
        self.assertEqual(weights["MSFT"], 0.6)
        self.assertEqual(weights["TLT"], 1.0)
        self.assertEqual(weights["CASH"], 0.0)
        self.assertIn("ON CONFLICT (symbol)", session.calls[0][0])

        invalid = Session([])
        with self.assertRaisesRegex(ValueError, "unsupported source"):
            upsert_holdings(
                invalid, holdings=[{"symbol": "AAPL", "source": "provider_secret"}]
            )
        self.assertEqual(invalid.calls, [])

    def test_context_exposures_sum_within_band_per_dimension(self):
        session = Session(
            [
                Result(first={"total": 1.0}),  # total weight
                Result(
                    rows=[{"theme": "AI Compute", "exposure": 0.6, "holdings": 2}]
                ),  # theme concentration
                Result(
                    rows=[
                        {
                            "id": uuid4(),
                            "description": "FOMC",
                            "expected_at": NOW,
                            "state": "pending",
                            "theme_id": THEME_ID,
                            "company": "Nvidia Corp",
                            "symbol": "NVDA",
                        }
                    ]
                ),  # pending catalysts
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
                ),  # review schedule
                Result(
                    rows=[
                        {"bucket": "Semis", "exposure": 0.6, "holdings": 2},
                        {"bucket": "Rates", "exposure": 0.4, "holdings": 1},
                    ]
                ),  # sector exposure
                Result(
                    rows=[{"bucket": "US", "exposure": 1.0, "holdings": 3}]
                ),  # country exposure
                Result(
                    rows=[{"bucket": "USD", "exposure": 1.0, "holdings": 3}]
                ),  # currency exposure
            ]
        )
        context = portfolio_context(session)
        self.assertEqual(context["total_weight"], 1.0)
        self.assertEqual(len(context["sectors"]), 2)
        self.assertEqual(len(context["catalysts"]), 1)
        self.assertEqual(len(context["review_schedule"]), 1)
        for dimension in ("sectors", "countries", "currencies"):
            total = sum(row["exposure"] for row in context[dimension])
            self.assertLessEqual(total, 1.0001)
        theme_total = sum(row["exposure"] for row in context["themes"])
        self.assertLessEqual(theme_total, 1.0001)
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
                            "id": uuid4(),
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
                            "id": uuid4(),
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

    def test_dossier_is_bounded_and_excludes_extracted_text(self):
        long_excerpt = "x" * 600
        session = Session(
            [
                Result(
                    first={
                        "company": "Nvidia Corp",
                        "symbol": "NVDA",
                        "business_overview": "GPU maker",
                        "segments": [],
                        "key_operating_drivers": [],
                        "capital_allocation": None,
                        "valuation_assumptions": {},
                        "guidance": {},
                        "updated_at": NOW,
                    }
                ),
                Result(first={"document_id": DOC_ID}),
                Result(rows=[thesis_row()]),
                Result(
                    rows=[
                        {
                            "id": uuid4(),
                            "document_id": DOC_ID,
                            "category": "guidance",
                            "change_kind": "changed",
                            "section_hash": "h",
                            "previous_section_hash": "p",
                            "excerpt": long_excerpt,
                            "previous_excerpt": None,
                            "metrics": {"revenue": 1},
                            "created_at": NOW,
                            "report_date": None,
                            "document_type": "annual_report",
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "document_id": DOC_ID,
                            "company": "Nvidia Corp",
                            "symbol": "NVDA",
                            "document_type": "annual_report",
                            "report_date": None,
                            "created_at": NOW,
                            "status": "analyzed",
                            "analysis_id": uuid4(),
                            "model": "test/model",
                            "analyzed_at": NOW,
                        }
                    ]
                ),
                Result(
                    rows=[
                        {
                            "id": uuid4(),
                            "document_id": DOC_ID,
                            "category": "guidance",
                            "change_kind": "changed",
                            "excerpt": "outlook improved",
                            "previous_excerpt": None,
                            "metrics": {},
                            "created_at": NOW,
                        }
                    ]
                ),
            ]
        )
        dossier = get_dossier(session, "Nvidia Corp")
        self.assertEqual(dossier["company"], "Nvidia Corp")
        self.assertEqual(dossier["profile"]["symbol"], "NVDA")
        self.assertEqual(len(dossier["theses"]), 1)
        self.assertEqual(len(dossier["filing_deltas"]), 1)
        self.assertEqual(len(dossier["filing_deltas"][0]["excerpt"]), 500)
        self.assertEqual(len(dossier["evidence_timeline"]), 1)
        self.assertEqual(len(dossier["changes"]), 1)
        self.assertEqual(dossier["changes"][0]["change_kind"], "changed")
        for sql, _ in session.calls:
            self.assertIn("LIMIT", sql)
            self.assertNotIn("extracted_text", sql)

    def test_dossier_unknown_company_returns_none(self):
        session = Session([Result(first=None), Result(first=None)])
        self.assertIsNone(get_dossier(session, "Nobody Inc"))


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


class ResearchApiTests(unittest.TestCase):
    """Run the api-facing assertions in a subprocess with an api-first path.

    The orchestrator and api trees each ship their own ``db`` module, so both
    cannot live in one import namespace; the api contract tests execute in a
    dedicated interpreter where api/ shadows nothing it needs.
    """

    def test_api_contracts(self):
        env = dict(os.environ)
        env["RESEARCH_TEST_API_FIRST"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "tests.test_research.ResearchApiApiTests",
                "-v",
            ],
            cwd=ORCH_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "api contract tests failed:\n"
                f"--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            ),
        )


class ResearchApiApiTests(unittest.TestCase):
    """Main-app TestClient behaviour; runs only in the api-first subprocess."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("RESEARCH_TEST_API_FIRST") != "1":
            raise unittest.SkipTest("api contract tests run in a dedicated subprocess")
        cls._previous_cwd = Path.cwd()
        os.chdir(API_ROOT)
        sys.path.insert(0, str(API_ROOT))
        try:
            from auth import mint_csrf_token

            from main import app
        except Exception as exc:
            raise unittest.SkipTest(
                f"api main app unavailable in this environment: {exc}"
            ) from exc
        finally:
            try:
                sys.path.remove(str(API_ROOT))
            except ValueError:
                pass
            os.chdir(cls._previous_cwd)
        cls.app = app
        cls.csrf_token = mint_csrf_token()
        cls.auth = {
            "Authorization": "Basic dGVzdDp0ZXN0",
            "Origin": "http://testserver",
            "X-CSRF-Token": cls.csrf_token,
        }

    def _client(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.app)
        client.cookies.set("csrf-token", self.csrf_token)
        return client

    def test_requires_auth_without_headers(self):
        client = self._client()
        with patch("routes.json.research.get_session") as get_session:
            response = client.get("/api/research/themes")
            self.assertEqual(response.status_code, 401)
        get_session.assert_not_called()

    def test_bad_payloads_422_before_session_open(self):
        client = self._client()
        with patch("routes.json.research.get_session") as get_session:
            response = client.post(
                "/api/research/themes",
                headers=self.auth,
                json={"definition": "missing name"},
            )
            self.assertEqual(response.status_code, 422)
            response = client.post(
                "/api/research/portfolio/holdings",
                headers=self.auth,
                json={"holdings": [{"symbol": "AAPL", "source": "provider_secret"}]},
            )
            self.assertEqual(response.status_code, 422)
            response = client.post(
                "/api/research/themes",
                headers=self.auth,
                json={
                    "name": "AI",
                    "definition": "d",
                    "entities": [{"entity_type": "secret", "entity_id": "x"}],
                },
            )
            self.assertEqual(response.status_code, 422)
        get_session.assert_not_called()

    def test_invalid_uuid_422_before_session_open(self):
        client = self._client()
        with patch("routes.json.research.get_session") as get_session:
            response = client.get("/api/research/themes/not-a-uuid", headers=self.auth)
            self.assertEqual(response.status_code, 422)
        get_session.assert_not_called()

    def test_unknown_theme_404(self):
        client = self._client()
        with (
            patch("research.get_theme", return_value=None),
            patch("routes.json.research.get_session") as get_session,
        ):
            response = client.get(f"/api/research/themes/{THEME_ID}", headers=self.auth)
            self.assertEqual(response.status_code, 404)
        get_session.assert_called_once()

    def test_unknown_company_404(self):
        client = self._client()
        with (
            patch("research.get_dossier", return_value=None),
            patch("routes.json.research.get_session") as get_session,
        ):
            response = client.get(
                "/api/research/companies/Nobody%20Inc", headers=self.auth
            )
            self.assertEqual(response.status_code, 404)
        get_session.assert_called_once()

    def test_create_theme_returns_created_id(self):
        client = self._client()
        with (
            patch("research.create_theme", return_value=str(THEME_ID)) as create,
            patch("routes.json.research.get_session") as get_session,
        ):
            response = client.post(
                "/api/research/themes",
                headers=self.auth,
                json={"name": "AI Compute", "definition": "Capex supercycle."},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"theme_id": str(THEME_ID)})
        create.assert_called_once()
        get_session.assert_called_once()

    def test_themes_list_serialises_iso_datetimes_and_uuid_strings(self):
        client = self._client()
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
        with (
            patch("research.list_themes", return_value=rows),
            patch("routes.json.research.get_session") as get_session,
        ):
            response = client.get("/api/research/themes", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["themes"][0]["id"], str(THEME_ID))
        self.assertEqual(body["themes"][0]["created_at"], NOW.isoformat())
        get_session.assert_called_once()


class ThesisFusionDelegationTests(unittest.TestCase):
    """Narrow compatibility delegation: desk evidence rows flow through
    thesis_fusion.attach_evidence; legacy rows keep the legacy SQL path."""

    def test_desk_evidence_rows_delegate_to_thesis_fusion(self):
        desk_row = {
            "evidence_type": "source_claim",
            "evidence_id": "claim:capex-2026",
            "relationship": "supports",
            "source_family": "filings",
            "content": {"statement": "Capex guide raised."},
            "source_timestamp": "2026-08-01T00:00:00Z",
        }
        with patch("thesis_fusion.attach_evidence") as attach:
            attach.return_value = {
                "attached": 1,
                "skipped_duplicate_fingerprint": 0,
                "skipped_correlated": 0,
            }
            count = add_thesis_evidence(
                Session([]), str(THESIS_ID), evidence=[desk_row]
            )
        self.assertEqual(count, 1)
        attach.assert_called_once()
        self.assertEqual(attach.call_args.args[0].calls, [])
        self.assertEqual(attach.call_args.args[1], str(THESIS_ID))
        self.assertEqual(attach.call_args.kwargs["evidence"], [desk_row])

    def test_legacy_evidence_rows_keep_legacy_path(self):
        session = Session(
            [
                Result(first={"present": 1}),  # thesis exists
                Result(),  # INSERT ... ON CONFLICT DO NOTHING
            ]
        )
        with patch("thesis_fusion.attach_evidence") as attach:
            count = add_thesis_evidence(
                session,
                str(THESIS_ID),
                evidence=[
                    {
                        "evidence_type": "macro_series",
                        "evidence_id": "CPIAUCSL",
                        "relationship": "supports",
                        "excerpt": "CPI cooled.",
                    }
                ],
            )
        attach.assert_not_called()
        self.assertEqual(count, 1)
        insert_sql = session.calls[1][0]
        self.assertIn("INSERT INTO investment_thesis_evidence", insert_sql)
        self.assertIn("ON CONFLICT", insert_sql)
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
