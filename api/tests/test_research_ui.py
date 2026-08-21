import os
import sys
import unittest
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

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
        "DEPLOYMENT_MODE": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["CONFIG_DIR"] = str(API_ROOT.parent / "config")

AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
THEME_ID = UUID("11111111-1111-4111-8111-111111111111")
THESIS_ID = UUID("22222222-2222-4222-8222-222222222222")
DOCUMENT_ID = UUID("33333333-3333-4333-8333-333333333333")
ATOM_SUPPORTS_ID = UUID("44444444-4444-4444-8444-444444444444")
ATOM_CONTRADICTS_ID = UUID("55555555-5555-4555-8555-555555555555")

FUNNEL_LABELS = [
    "Structural trend",
    "Affected industries",
    "Candidate companies",
    "Evidence",
    "Expectations and valuation",
    "Catalysts",
    "Risks and counter-thesis",
]


class _DomProbe(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def has(self, tag, **attrs):
        def matches(node_attrs):
            for name, value in attrs.items():
                key = name.replace("__", "-")
                if key not in node_attrs or node_attrs[key] != value:
                    return False
            return True

        return any(
            node_tag == tag and matches(node_attrs)
            for node_tag, node_attrs in self.nodes
        )


def dom_probe(markup):
    probe = _DomProbe()
    probe.feed(markup)
    return probe


def funnel_steps():
    return [
        {"key": key, "label": label, "detail": f"Step about {label.lower()}."}
        for key, label in zip(
            (
                "structural_trend",
                "affected_industries",
                "candidate_companies",
                "evidence",
                "expectations_valuation",
                "catalysts",
                "risks_counter_thesis",
            ),
            FUNNEL_LABELS,
            strict=False,
        )
    ]


def theme_row():
    return {
        "id": THEME_ID,
        "name": "Energy transition",
        "definition": "Structural shift in energy supply.",
        "horizon": "multi_year",
        "status": "active",
        "confidence": 0.7,
        "review_at": NOW,
        "updated_at": NOW,
        "macro_drivers": ["electrification", "grid investment"],
        "key_indicators": ["CPIAUCSL"],
        "invalidation_conditions": ["Policy reversal", "Technology plateau"],
        "confidence_components": {"evidence": {"score": 0.8}, "consensus": 0.6},
    }


def entity_rows():
    return [
        {
            "entity_type": "industry",
            "entity_id": "utilities",
            "display_name": "Utilities",
        },
        {"entity_type": "symbol", "entity_id": "EXC", "display_name": "EXC"},
    ]


def thesis_row():
    return {
        "id": THESIS_ID,
        "company": "Example Corp",
        "symbol": "EXC",
        "claim": "Grid capex accelerates.",
        "variant_perception": "Market underweights electrification.",
        "status": "active",
        "horizon": "multi_year",
        "review_at": NOW,
        "confidence": 0.65,
        "invalidation_conditions": ["Grid spending stalls"],
        "updated_at": NOW,
        "version": 2,
        "latest_claim": "Grid capex accelerates.",
        "rationale": "Utility budgets are up.",
        "changed_by": "operator",
        "version_created_at": NOW,
        "supports": 2,
        "contradicts": 1,
        "context": 0,
    }


def catalyst_row():
    return {
        "id": THESIS_ID,
        "description": "Grid capex guidance update",
        "expected_at": NOW,
        "state": "pending",
    }


def risk_row():
    return {
        "id": THESIS_ID,
        "description": "Rates rise sharply",
        "kind": "counter_thesis",
        "severity": "moderate",
    }


def atom_row(*, relationship, claim, atom_id):
    return {
        "relationship": relationship,
        "id": atom_id,
        "claim": claim,
        "confidence": 0.8,
        "status": "published",
        "published_at": NOW,
    }


def indicator_row():
    return {
        "series_id": "CPIAUCSL",
        "observed_at": NOW,
        "value": 320.1,
        "title": "Consumer Price Index",
        "units": "Index",
    }


def event_row():
    return {
        "event_id": "CPI",
        "event_name": "CPI Release",
        "country": "US",
        "scheduled_at": NOW,
        "impact_level": "high",
        "consensus": "0.3%",
        "previous": "0.2%",
        "actual": None,
    }


def index_context():
    return {
        "status": "published",
        "themes": [
            {
                "id": str(THEME_ID),
                "name": "Energy transition",
                "definition": "Structural shift in energy supply.",
                "horizon": "multi_year",
                "status": "active",
                "confidence": 0.7,
                "review_at": NOW.isoformat(),
                "entity_count": 2,
                "thesis_count": 1,
            }
        ],
        "funnel": funnel_steps(),
    }


def theme_context():
    return {
        "status": "published",
        "theme": {
            "id": str(THEME_ID),
            "name": "Energy transition",
            "definition": "Structural shift in energy supply.",
            "horizon": "multi_year",
            "status": "active",
            "confidence": 0.7,
            "review_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
            "macro_drivers": ["electrification", "grid investment"],
            "key_indicators": ["CPIAUCSL"],
            "invalidation_conditions": ["Policy reversal", "Technology plateau"],
            "confidence_components": [
                {"name": "evidence", "score": 0.8, "label": None},
                {"name": "consensus", "score": 0.6, "label": "Partial agreement"},
            ],
            "entities": {
                "available": True,
                "groups": {
                    "industry": [
                        {"entity_id": "utilities", "display_name": "Utilities"},
                        {
                            "entity_id": "semiconductors",
                            "display_name": "Semiconductors",
                        },
                    ],
                    "symbol": [{"entity_id": "EXC", "display_name": "EXC"}],
                },
            },
            "theses": {
                "available": True,
                "rows": [
                    {
                        "id": str(THESIS_ID),
                        "company": "Example Corp",
                        "symbol": "EXC",
                        "claim": "Grid capex accelerates.",
                        "variant_perception": "Market underweights electrification.",
                        "status": "active",
                        "horizon": "multi_year",
                        "review_at": NOW.isoformat(),
                        "confidence": 0.65,
                        "invalidation_conditions": ["Grid spending stalls"],
                        "version": 2,
                        "latest_claim": "Grid capex accelerates.",
                        "rationale": "Utility budgets are up.",
                        "version_created_at": NOW.isoformat(),
                        "evidence_counts": {
                            "supports": 2,
                            "contradicts": 1,
                            "context": 0,
                        },
                        "catalysts": {
                            "available": True,
                            "rows": [
                                {
                                    "id": str(THESIS_ID),
                                    "description": "Grid capex guidance update",
                                    "expected_at": NOW.isoformat(),
                                    "state": "pending",
                                }
                            ],
                        },
                        "risks": {
                            "available": True,
                            "rows": [
                                {
                                    "id": str(THESIS_ID),
                                    "description": "Rates rise sharply",
                                    "kind": "counter_thesis",
                                    "severity": "moderate",
                                }
                            ],
                        },
                        "atoms": {
                            "available": True,
                            "supporting": [
                                {
                                    "id": str(ATOM_SUPPORTS_ID),
                                    "claim": "Utility capex guidance raised across the sector.",
                                    "confidence": 0.8,
                                    "status": "published",
                                    "published_at": NOW.isoformat(),
                                }
                            ],
                            "contradicting": [
                                {
                                    "id": str(ATOM_CONTRADICTS_ID),
                                    "claim": "Grid spending growth decelerated last quarter.",
                                    "confidence": 0.6,
                                    "status": "published",
                                    "published_at": NOW.isoformat(),
                                }
                            ],
                        },
                    }
                ],
            },
            "indicators": {
                "available": True,
                "rows": [
                    {
                        "series_id": "CPIAUCSL",
                        "title": "Consumer Price Index",
                        "units": "Index",
                        "value": 320.1,
                        "observed_at": NOW.isoformat(),
                    }
                ],
            },
            "events": {
                "available": True,
                "rows": [
                    {
                        "event_id": "CPI",
                        "event_name": "CPI Release",
                        "country": "US",
                        "scheduled_at": NOW.isoformat(),
                        "impact_level": "high",
                        "consensus": "0.3%",
                        "previous": "0.2%",
                        "actual": None,
                    }
                ],
            },
        },
    }


def dossier_context():
    return {
        "status": "published",
        "dossier": {
            "company": "Example Corp",
            "profile": {
                "company": "Example Corp",
                "symbol": "EXC",
                "business_overview": "Utility holding company.",
                "segments": ["Regulated distribution", "Renewables"],
                "key_operating_drivers": ["Load growth", "Rate cases"],
                "capital_allocation": "Dividend growth and grid capex.",
                "valuation_assumptions": {"wacc": "7.5%", "growth": "4%"},
                "guidance": {"revenue": "$12B"},
                "updated_at": NOW.isoformat(),
            },
            "latest_document": {
                "document_id": str(DOCUMENT_ID),
                "company": "Example Corp",
                "symbol": "EXC",
                "region": "US",
                "industry": "Utilities",
                "document_type": "annual_report",
                "report_date": "2025-12-31",
                "filename": "exc-10k.pdf",
                "status": "analyzed",
                "created_at": NOW.isoformat(),
            },
            "theses": {
                "available": True,
                "rows": [
                    {
                        "id": str(THESIS_ID),
                        "theme_id": str(THEME_ID),
                        "theme_name": "Energy transition",
                        "claim": "Grid capex accelerates.",
                        "status": "active",
                        "horizon": "multi_year",
                        "confidence": 0.65,
                        "updated_at": NOW.isoformat(),
                    }
                ],
            },
            "deltas": {
                "available": True,
                "rows": [
                    {
                        "category": "guidance",
                        "change_kind": "changed",
                        "excerpt": "Full-year revenue guidance raised to $12B.",
                        "previous_excerpt": "Prior guidance was $11B.",
                        "created_at": NOW.isoformat(),
                    },
                    {
                        "category": "capital_allocation",
                        "change_kind": "new",
                        "excerpt": "New buyback authorization added.",
                        "previous_excerpt": None,
                        "created_at": NOW.isoformat(),
                    },
                ],
            },
            "changes_since_previous": [
                {
                    "category": "guidance",
                    "change_kind": "changed",
                    "excerpt": "Full-year revenue guidance raised to $12B.",
                    "previous_excerpt": "Prior guidance was $11B.",
                    "created_at": NOW.isoformat(),
                },
                {
                    "category": "capital_allocation",
                    "change_kind": "new",
                    "excerpt": "New buyback authorization added.",
                    "previous_excerpt": None,
                    "created_at": NOW.isoformat(),
                },
            ],
            "financial_trends": {
                "available": True,
                "rows": [
                    {
                        "analysis_id": str(DOCUMENT_ID),
                        "document_type": "annual_report",
                        "report_date": "2025-12-31",
                        "created_at": NOW.isoformat(),
                        "model": "deepseek/test",
                        "metrics": [
                            {
                                "name": "revenue",
                                "value": 12000.0,
                                "unit": "USD M",
                                "period": "FY2025",
                                "change_pct": 8.5,
                            }
                        ],
                    }
                ],
            },
            "sources": {
                "available": True,
                "rows": [
                    {
                        "document_id": str(DOCUMENT_ID),
                        "document_type": "annual_report",
                        "report_date": "2025-12-31",
                        "status": "analyzed",
                        "created_at": NOW.isoformat(),
                        "evidence_count": 3,
                    }
                ],
            },
        },
    }


class ResearchLoaderTests(unittest.TestCase):
    def test_load_research_index_fail_soft_on_database_error(self):
        from routes.views.research import load_research_index

        with patch(
            "routes.views.research.query_many", side_effect=RuntimeError("secret sql")
        ):
            payload = load_research_index({})
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["themes"], [])
        self.assertNotIn("secret sql", str(payload))

    def test_load_theme_page_returns_none_for_unknown_theme(self):
        from routes.views.research import load_theme_page

        with patch("routes.views.research.query_one", return_value=None) as query:
            payload = load_theme_page({}, str(THEME_ID))
        self.assertIsNone(payload)
        query.assert_called_once()

    def test_load_theme_page_assembles_sections_and_groups_atoms(self):
        from routes.views.research import load_theme_page

        side_effects = [
            entity_rows(),
            [thesis_row()],
            [catalyst_row()],
            [risk_row()],
            [
                atom_row(
                    relationship="supports",
                    claim="Utility capex guidance raised across the sector.",
                    atom_id=ATOM_SUPPORTS_ID,
                ),
                atom_row(
                    relationship="contradicts",
                    claim="Grid spending growth decelerated last quarter.",
                    atom_id=ATOM_CONTRADICTS_ID,
                ),
            ],
            [indicator_row()],
            [event_row()],
        ]
        with (
            patch("routes.views.research.query_one", return_value=theme_row()),
            patch("routes.views.research.query_many", side_effect=side_effects),
        ):
            payload = load_theme_page({}, str(THEME_ID))
        self.assertEqual(payload["status"], "published")
        theme = payload["theme"]
        self.assertEqual(theme["name"], "Energy transition")
        self.assertEqual(theme["review_at"], NOW.isoformat())
        self.assertEqual(
            theme["invalidation_conditions"], ["Policy reversal", "Technology plateau"]
        )
        self.assertEqual(set(theme["entities"]["groups"]), {"industry", "symbol"})
        thesis = theme["theses"]["rows"][0]
        self.assertEqual(thesis["evidence_counts"]["contradicts"], 1)
        self.assertEqual(thesis["version"], 2)
        self.assertEqual(thesis["rationale"], "Utility budgets are up.")
        atoms = thesis["atoms"]
        self.assertEqual(
            [atom["claim"] for atom in atoms["supporting"]],
            ["Utility capex guidance raised across the sector."],
        )
        self.assertEqual(
            [atom["claim"] for atom in atoms["contradicting"]],
            ["Grid spending growth decelerated last quarter."],
        )
        self.assertEqual(
            [catalyst["description"] for catalyst in thesis["catalysts"]["rows"]],
            ["Grid capex guidance update"],
        )
        self.assertEqual(
            [risk["description"] for risk in thesis["risks"]["rows"]],
            ["Rates rise sharply"],
        )
        self.assertEqual(theme["indicators"]["rows"][0]["series_id"], "CPIAUCSL")
        self.assertEqual(theme["events"]["rows"][0]["event_name"], "CPI Release")
        self.assertEqual(theme["confidence_components"][0]["score"], 0.8)
        self.assertNotIn("secret sql", str(payload))

    def test_load_theme_page_sections_fail_soft(self):
        from routes.views.research import load_theme_page

        with (
            patch("routes.views.research.query_one", return_value=theme_row()),
            patch(
                "routes.views.research.query_many",
                side_effect=RuntimeError("secret sql"),
            ),
        ):
            payload = load_theme_page({}, str(THEME_ID))
        self.assertEqual(payload["status"], "published")
        theme = payload["theme"]
        self.assertEqual(theme["entities"]["available"], False)
        self.assertEqual(theme["theses"]["available"], False)
        self.assertEqual(theme["indicators"]["available"], False)
        self.assertEqual(theme["events"]["available"], False)
        self.assertNotIn("secret sql", str(payload))

    def test_load_dossier_returns_none_for_unknown_company(self):
        from routes.views.research import load_dossier

        with patch("routes.views.research.query_one", return_value=None) as query:
            payload = load_dossier({}, "NoSuchCompany")
        self.assertIsNone(payload)
        self.assertEqual(query.call_count, 2)

    def test_load_dossier_assembles_sections_and_filters_changes(self):
        from routes.views.research import load_dossier

        profile = {
            "company": "Example Corp",
            "symbol": "EXC",
            "business_overview": "Utility holding company.",
            "segments": ["Regulated distribution", "Renewables"],
            "key_operating_drivers": ["Load growth"],
            "capital_allocation": "Dividend growth and grid capex.",
            "valuation_assumptions": {"wacc": "7.5%"},
            "guidance": {"revenue": "$12B"},
            "updated_at": NOW,
        }
        latest = {
            "document_id": DOCUMENT_ID,
            "company": "Example Corp",
            "symbol": "EXC",
            "region": "US",
            "industry": "Utilities",
            "document_type": "annual_report",
            "report_date": "2025-12-31",
            "filename": "exc-10k.pdf",
            "status": "analyzed",
            "created_at": NOW,
        }
        theses = [
            {
                "id": THESIS_ID,
                "theme_id": THEME_ID,
                "theme_name": "Energy transition",
                "claim": "Grid capex accelerates.",
                "status": "active",
                "horizon": "multi_year",
                "confidence": 0.65,
                "updated_at": NOW,
            }
        ]
        deltas = [
            {
                "category": "guidance",
                "change_kind": "changed",
                "excerpt": "Full-year revenue guidance raised to $12B.",
                "previous_excerpt": None,
                "created_at": NOW,
            },
            {
                "category": "segments",
                "change_kind": "unchanged",
                "excerpt": "Same segments as before.",
                "previous_excerpt": None,
                "created_at": NOW,
            },
        ]
        financial = [
            {
                "analysis_id": DOCUMENT_ID,
                "facts": {
                    "metrics": {
                        "revenue": {
                            "value": 12000.0,
                            "unit": "USD M",
                            "period": "FY2025",
                            "change_pct": 8.5,
                        }
                    }
                },
                "model": "deepseek/test",
                "created_at": NOW,
                "document_type": "annual_report",
                "report_date": "2025-12-31",
            }
        ]
        sources = [
            {
                "document_id": DOCUMENT_ID,
                "document_type": "annual_report",
                "report_date": "2025-12-31",
                "status": "analyzed",
                "created_at": NOW,
                "evidence_count": 3,
            }
        ]
        with (
            patch(
                "routes.views.research.query_one",
                side_effect=[profile, latest],
            ),
            patch(
                "routes.views.research.query_many",
                side_effect=[deltas, theses, financial, sources],
            ),
        ):
            payload = load_dossier({}, "Example Corp")
        self.assertEqual(payload["status"], "published")
        self.assertEqual(payload["profile"]["symbol"], "EXC")
        self.assertEqual(payload["latest_document"]["document_type"], "annual_report")
        self.assertEqual(
            [t["theme_name"] for t in payload["theses"]["rows"]], ["Energy transition"]
        )
        self.assertEqual(
            [d["change_kind"] for d in payload["changes_since_previous"]], ["changed"]
        )
        self.assertEqual(len(payload["deltas"]["rows"]), 2)
        self.assertEqual(
            payload["financial_trends"]["rows"][0]["metrics"][0]["name"], "revenue"
        )
        self.assertEqual(payload["sources"]["rows"][0]["evidence_count"], 3)
        self.assertNotIn("secret sql", str(payload))


class ResearchRouteTests(unittest.TestCase):
    def _app(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates

        from routes.views.research import router

        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        return app

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(self._app())

    def test_research_evaluation_helpers_survive_api_budget_import(self):
        import budgets  # noqa: F401
        from routes.json import research as json_research
        from routes.views import research as view_research

        self.assertIsNotNone(json_research._research_queries)
        self.assertIsNotNone(json_research._list_benchmarks)
        self.assertIsNotNone(json_research._live_case_cohorts)
        self.assertIsNotNone(view_research._research_queries)
        self.assertIsNotNone(view_research._list_benchmarks)
        self.assertIsNotNone(view_research._live_case_cohorts)
        previous = json_research._annotate_benchmark_scorecard
        try:
            json_research._annotate_benchmark_scorecard = None
            self.assertTrue(callable(json_research._annotation_helper()))
        finally:
            json_research._annotate_benchmark_scorecard = previous

    def test_pages_require_auth(self):
        from fastapi.testclient import TestClient

        from main import app

        client = TestClient(app)
        with patch("routes.views.research.load_research_index") as loader:
            self.assertEqual(client.get("/research").status_code, 401)
            self.assertEqual(client.get("/research/theses").status_code, 401)
            self.assertEqual(
                client.get(f"/research/theses/{THESIS_ID}").status_code, 401
            )
        loader.assert_not_called()

    def test_index_renders_funnel_steps_in_order(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch(
                "routes.views.research.load_research_index",
                return_value=index_context(),
            ),
        ):
            response = client.get("/research")
        self.assertEqual(response.status_code, 200)
        text = response.text
        positions = [text.find(label) for label in FUNNEL_LABELS]
        self.assertTrue(all(position >= 0 for position in positions))
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Energy transition", text)
        self.assertNotIn("data-live-section", text)
        dom = dom_probe(text)
        self.assertTrue(dom.has("section", data__thesis__view="research-preview"))
        self.assertTrue(dom.has("a", href="/research/theses"))
        script = (API_ROOT / "static" / "app.js").read_text()
        self.assertIn("groupQuery.set('status', 'active');", script)

    def test_thesis_tournament_route_renders_operator_dom_contract(self):
        response = self._client().get("/research/theses")

        self.assertEqual(response.status_code, 200)
        dom = dom_probe(response.text)
        self.assertTrue(dom.has("main", data__thesis__view="desk"))
        self.assertTrue(dom.has("button", type="button", data__thesis__run=None))
        self.assertTrue(dom.has("tbody", data__thesis__opportunities=None))
        self.assertTrue(dom.has("strong", data__status__value="model-cost"))
        self.assertTrue(dom.has("strong", data__status__value="calibration"))
        self.assertIn("Linked theses", response.text)
        script = (API_ROOT / "static" / "app.js").read_text()
        self.assertIn("return 'Brier ' + brier.toFixed(3)", script)
        self.assertIn("function thesisLatestCycleAt(status)", script)
        self.assertIn(
            "thesisDate(thesisLatestCycleAt(status))",
            script,
        )
        self.assertTrue(dom.has("div", role="status", aria__live="polite"))

    def test_thesis_tournament_defaults_to_eligible_opportunities(self):
        response = self._client().get("/research/theses")

        self.assertEqual(response.status_code, 200)
        dom = dom_probe(response.text)
        self.assertTrue(dom.has("option", value="0"))
        self.assertTrue(dom.has("option", value="0.25", selected=None))

        script = (API_ROOT / "static" / "app.js").read_text()
        self.assertIn("var thesisDefaultMinimumScore = '0.25';", script)
        self.assertIn(
            "score: score ? score.value : thesisDefaultMinimumScore",
            script,
        )
        self.assertIn(
            "opportunityQuery.set('minimum_score', selected.score);",
            script,
        )
        self.assertIn(
            "if (selected.score === '0') opportunityQuery.set('include_ineligible', 'true');",
            script,
        )
        self.assertNotIn(
            "if (selected.score) opportunityQuery.set('minimum_score'",
            script,
        )
        self.assertIn(
            "No eligible opportunities meet these filters. Choose Any score to inspect ineligible theses.",
            script,
        )
        self.assertIn(
            "? 'No eligible or ineligible opportunities meet these filters.'",
            script,
        )

    def test_ineligible_opportunity_rows_are_visibly_non_rankable(self):
        script = (API_ROOT / "static" / "app.js").read_text()
        self.assertIn("blockers.length && labels.length < 8", script)

        self.assertIn("if (item.eligible !== false) return '';", script)
        self.assertIn("row.classList.add('thesis-opportunity-ineligible');", script)
        self.assertIn("row.dataset.eligible = 'false';", script)
        self.assertIn(
            "eligibilityNote.setAttribute('aria-label', 'Ranking eligibility: ' + ineligibilityLabel);",
            script,
        )
        self.assertIn(
            "return 'Not eligible' + (labels.length ? ' · ' + labels.join(', ') : '');",
            script,
        )
        style = (API_ROOT / "static" / "style.css").read_text()
        self.assertIn("tr.thesis-opportunity-ineligible", style)
        self.assertIn("box-shadow: inset 3px 0 0", style)

    def test_thesis_detail_route_validates_uuid_before_render(self):
        client = self._client()

        with patch.object(
            client.app.state.templates,
            "TemplateResponse",
            wraps=client.app.state.templates.TemplateResponse,
        ) as renderer:
            invalid = client.get("/research/theses/not-a-uuid")
        self.assertEqual(invalid.status_code, 404)
        renderer.assert_not_called()

        response = client.get(f"/research/theses/{THESIS_ID}")
        self.assertEqual(response.status_code, 200)
        dom = dom_probe(response.text)
        self.assertTrue(
            dom.has(
                "main",
                data__thesis__view="detail",
                data__thesis__id=str(THESIS_ID),
            )
        )
        self.assertTrue(dom.has("details", data__evidence__relationship="contradicts"))
        self.assertTrue(dom.has("div", data__thesis__playbooks=None))
        self.assertTrue(dom.has("tbody", data__thesis__playbook__matches=None))
        self.assertTrue(dom.has("dd", data__thesis__field="trend-context"))
        self.assertTrue(dom.has("dd", data__thesis__field="valuation-context"))
        self.assertTrue(dom.has("dd", data__thesis__field="sentiment-context"))
        self.assertTrue(dom.has("ul", data__thesis__citation__map=None))
        self.assertTrue(dom.has("ul", data__thesis__risks=None))
        script = (API_ROOT / "static" / "app.js").read_text()
        self.assertIn(
            "renderThesisCitationMap(root, version.citation_map || core.citation_map);",
            script,
        )
        self.assertIn("status: 'status'", script)
        self.assertIn("actionability: 'actionability'", script)
        self.assertIn("opposition: 'opposition'", script)
        self.assertIn("function renderThesisRisks(root, risks)", script)
        self.assertIn("function thesisStructuredFindings(value)", script)
        self.assertIn("' · available ' + thesisDate(item.available_at)", script)
        self.assertIn("var severity = risk.severity;", script)
        self.assertNotIn("run.summary || run.result || run.findings", script)
        self.assertFalse(dom.has("button", data__thesis__run=None))

    def test_evaluation_page_shows_variants_regressions_and_resource_use(self):
        client = self._client()
        variant_a = "a" * 64
        variant_b = "b" * 64
        queries = MagicMock()
        queries.list_replay_runs.return_value = [
            {
                "replay_as_of": NOW,
                "benchmark_id": "episode",
                "evidence_source": "synthetic_benchmark",
                "status": "completed",
                "variant_fingerprint": variant_b,
                "variant_identity": {
                    "pattern_discovery": {"model": "provider/model-b"}
                },
                "stage_metrics": [{"duration_ms": 1250}],
                "result_summary": {"case_count": 1, "errors": []},
                "dimensions": {
                    "discovery": {"status": "pass"},
                    "lead_time": {"status": "pass"},
                    "point_in_time_integrity": {"status": "pass"},
                },
                "cost_usd": 0.0123,
                "human_annotations": {"overall_label": "partial"},
                "annotation_version": 2,
            }
        ]
        queries.list_quality_metrics.return_value = [
            {
                "metrics": {
                    "variant_fingerprints": {
                        "left": variant_a,
                        "right": variant_b,
                    },
                    "dimension_status_changes": {
                        "specificity": {"left": "pass", "right": "fail"}
                    },
                    "resource_usage": {"delta": {"cost_usd": 0.002, "latency_ms": 150}},
                }
            }
        ]
        queries.research_status.return_value = {}
        session_scope = MagicMock()
        session_scope.__enter__.return_value = object()
        benchmark = SimpleNamespace(
            episode_id="episode",
            version=1,
            episode_kind="development",
            synthetic=True,
            description="Bounded episode.",
            replay_dates=(NOW,),
            evidence=(object(),),
        )
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch("routes.views.research.get_session", return_value=session_scope),
            patch("routes.views.research._research_queries", queries),
            patch(
                "routes.views.research._list_benchmarks",
                return_value=(benchmark,),
            ),
            patch("routes.views.research._live_case_cohorts", return_value=[]),
        ):
            response = client.get("/research/evaluation")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Variant regression checks", response.text)
        self.assertIn("provider/model-b", response.text)
        self.assertIn(variant_b[:10], response.text)
        self.assertIn("specificity: pass", response.text)
        self.assertIn("1.25s", response.text)
        self.assertIn("partial · v2", response.text)
        self.assertIn("testable-hypothesis discovery", response.text)

    def test_index_database_failure_is_fail_soft(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch(
                "routes.views.research.query_many",
                side_effect=RuntimeError("secret sql"),
            ),
        ):
            response = client.get("/research")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unavailable", response.text)
        self.assertNotIn("secret sql", response.text)

    def test_theme_page_shows_atoms_review_date_and_invalidation_conditions(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch(
                "routes.views.research.load_theme_page", return_value=theme_context()
            ),
        ):
            response = client.get(f"/research/themes/{THEME_ID}")
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("Utility capex guidance raised across the sector.", text)
        self.assertIn("Grid spending growth decelerated last quarter.", text)
        self.assertIn("Supports", text)
        self.assertIn("Contradicts", text)
        self.assertIn("review due", text)
        self.assertIn(NOW.isoformat(), text)
        self.assertIn("Policy reversal", text)
        self.assertIn("Technology plateau", text)
        self.assertIn("Grid spending stalls", text)
        self.assertNotIn("data-live-section", text)

    def test_theme_page_404_for_unknown_and_422_for_invalid_id(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch("routes.views.research.load_theme_page", return_value=None),
        ):
            missing = client.get(f"/research/themes/{THEME_ID}")
        self.assertEqual(missing.status_code, 404)
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch("routes.views.research.load_theme_page") as loader,
        ):
            invalid = client.get("/research/themes/not-a-uuid")
        self.assertEqual(invalid.status_code, 422)
        loader.assert_not_called()

    def test_theme_page_database_failure_is_fail_soft(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch(
                "routes.views.research.query_one",
                side_effect=RuntimeError("secret sql"),
            ),
        ):
            response = client.get(f"/research/themes/{THEME_ID}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unavailable", response.text)
        self.assertNotIn("secret sql", response.text)

    def test_dossier_shows_delta_categories_and_changes_section(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch("routes.views.research.load_dossier", return_value=dossier_context()),
        ):
            response = client.get("/research/companies/Example%20Corp")
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("Changes since previous analysis", text)
        self.assertIn("guidance", text)
        self.assertIn("capital_allocation", text)
        self.assertIn("changed", text)
        self.assertIn("new", text)
        self.assertIn("Full-year revenue guidance raised to $12B.", text)
        self.assertIn("Utility holding company.", text)
        self.assertNotIn("data-live-section", text)

    def test_dossier_404_for_unknown_and_422_for_invalid_company(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch("routes.views.research.load_dossier", return_value=None),
        ):
            missing = client.get("/research/companies/NoSuchCompany")
        self.assertEqual(missing.status_code, 404)
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch("routes.views.research.load_dossier") as loader,
        ):
            invalid = client.get("/research/companies/" + "X" * 65)
        self.assertEqual(invalid.status_code, 422)
        loader.assert_not_called()

    def test_dossier_database_failure_is_fail_soft(self):
        client = self._client()
        with (
            patch("routes.views.research.load_config", return_value={}),
            patch(
                "routes.views.research.query_one",
                side_effect=RuntimeError("secret sql"),
            ),
        ):
            response = client.get("/research/companies/Example%20Corp")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unavailable", response.text)
        self.assertNotIn("secret sql", response.text)


if __name__ == "__main__":
    unittest.main()
