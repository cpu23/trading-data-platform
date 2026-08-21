"""Lean dashboard template contract.

Focused, template-level contracts for the lean dashboard refactor:

* ``dashboard.html`` renders exactly the allowed surfaces in contract order
  (header, compact top strip, Since your last view, one lazy watchlist grid,
  one drawer/expansion target, one merged briefing) and no prohibited
  sections/includes.
* ``partials/top_strip.html`` renders exactly three cells (Current session,
  Current regime, Next catalyst) with truthful unavailable placeholders and a
  ``marketRefresh``-only refresh contract.
* ``partials/briefing_prose.html`` is the single merged briefing surface
  (What changed / Current interpretation / What would invalidate this,
  visible delta, atom counts and provenance behind disclosure, lazy evidence
  hooks) with the same refresh contract.
* ``partials/briefing_delta.html`` remains only as a compatible direct-hit
  presentation target; the dashboard must not include it separately.
* No legacy ``every 90s`` polling anywhere in the owned templates.
"""

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

API_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_DASHBOARD_INCLUDES = (
    "partials/header.html",
    "partials/top_strip.html",
    "partials/since_last_view.html",
    'hx-get="/partials/dashboard/watchlist-grid"',
    "partials/expansion_panel.html",
    "partials/briefing_prose.html",
)

PROHIBITED_DASHBOARD_MARKERS = (
    "partials/research_intelligence.html",
    "/partials/dashboard/change-feed",
    "/partials/dashboard/cross-asset",
    "/partials/dashboard/catalysts",
    "partials/macro_release_cards.html",
    "partials/news_section.html",
    "/partials/dashboard/briefing-delta",
    "partials/cards_section.html",
    "partials/regime_section.html",
    "partials/indicators_section.html",
    "partials/events_section.html",
    "Change feed",
    "Cross-asset",
    "Upcoming catalysts",
    "Briefing delta",
    "Research intelligence",
    "every 90s",
    "cycleComplete",
)

STRIP = {
    "available": True,
    "session_label": "London",
    "regime": {
        "regime": "Risk-on",
        "sub_regime": "broad-based",
        "confidence": "medium",
    },
    "next_catalyst": {
        "event_name": "US nonfarm payrolls",
        "countdown_display": "2d 04h",
        "country": "US",
    },
}

BRIEFING_SECTIONS = [
    {"label": "What changed", "body": "Markets rallied on softer CPI."},
    {"label": "Current interpretation", "body": "Risk-on tone into payrolls."},
    {"label": "What would invalidate this", "body": "A hawkish surprise."},
]

BRIEFING_DELTA = {
    "available": True,
    "latest_date": "2026-08-20",
    "bullets": ["Changed section: interpretation", "New section: what_changed"],
    "atoms": [{"claim_type": "macro_series", "count": 3}],
}

DELTA_UNAVAILABLE = {
    "available": False,
    "latest_date": None,
    "bullets": [],
    "atoms": [],
}


class LeanDashboardTemplateContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(
            loader=FileSystemLoader(API_ROOT / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        cls.dashboard = (API_ROOT / "templates/dashboard.html").read_text()
        cls.top_strip = (API_ROOT / "templates/partials/top_strip.html").read_text()
        cls.briefing_prose = (
            API_ROOT / "templates/partials/briefing_prose.html"
        ).read_text()
        cls.briefing_delta = (
            API_ROOT / "templates/partials/briefing_delta.html"
        ).read_text()

    def render(self, name, **context):
        return self.env.get_template(name).render(**context)

    # ── dashboard.html surfaces ──────────────────────────────────────────────

    def test_dashboard_renders_only_allowed_surfaces_in_contract_order(self):
        for marker in ALLOWED_DASHBOARD_INCLUDES:
            self.assertIn(marker, self.dashboard)
        positions = [self.dashboard.index(marker) for marker in ALLOWED_DASHBOARD_INCLUDES]
        self.assertEqual(
            positions, sorted(positions),
            "Dashboard surfaces must appear in contract order",
        )

    def test_dashboard_has_no_prohibited_surfaces_or_wording(self):
        for marker in PROHIBITED_DASHBOARD_MARKERS:
            self.assertNotIn(marker, self.dashboard)

    def test_dashboard_has_exactly_one_lazy_watchlist_grid(self):
        self.assertEqual(
            self.dashboard.count('hx-get="/partials/dashboard/watchlist-grid"'),
            1,
            "Exactly one lazy watchlist grid shell is allowed",
        )
        self.assertIn('hx-trigger="load, marketRefresh from:body"', self.dashboard)

    def test_dashboard_has_one_briefing_section_without_separate_delta(self):
        self.assertEqual(self.dashboard.count("partials/briefing_prose.html"), 1)
        self.assertNotIn('id="briefing-delta"', self.dashboard)
        self.assertNotIn("briefing_delta.html", self.dashboard)

    def test_dashboard_keeps_drawer_expansion_target(self):
        self.assertIn("partials/expansion_panel.html", self.dashboard)

    # ── compact top strip ────────────────────────────────────────────────────

    def test_top_strip_renders_exactly_three_cells(self):
        rendered = self.render(
            "partials/top_strip.html", strip=STRIP, live_updates_enabled=False
        )
        self.assertEqual(
            rendered.count('class="strip-cell"'),
            3,
            "Compact strip must expose exactly three cells",
        )
        for label in ("Current session", "Current regime", "Next catalyst"):
            self.assertIn(label, rendered)
        for cell_value in (
            "London",
            "Risk-on",
            "broad-based",
            "medium",
            "US nonfarm payrolls",
            "2d 04h",
            "US",
        ):
            self.assertIn(cell_value, rendered)

    def test_top_strip_omits_heavy_fields(self):
        rendered = self.render(
            "partials/top_strip.html", strip=STRIP, live_updates_enabled=False
        )
        for prohibited in (
            "Last price",
            "Last material event",
            "Direction",
            "direction-chip",
            "Sources",
            "source_health",
            "Budget",
        ):
            self.assertNotIn(prohibited, rendered)

    def test_top_strip_truthful_placeholders_when_values_missing(self):
        bare = {
            "available": True,
            "session_label": None,
            "regime": None,
            "next_catalyst": None,
        }
        rendered = self.render(
            "partials/top_strip.html", strip=bare, live_updates_enabled=False
        )
        self.assertEqual(rendered.count('class="strip-cell"'), 3)
        self.assertNotIn("Session snapshot unavailable.", rendered)
        self.assertEqual(rendered.count("—"), 3)

    def test_top_strip_unavailable_is_truthful(self):
        rendered = self.render(
            "partials/top_strip.html", strip={}, live_updates_enabled=False
        )
        self.assertIn("Session snapshot unavailable.", rendered)
        self.assertEqual(rendered.count('class="strip-cell"'), 0)

    def test_top_strip_preserves_accessible_heading(self):
        rendered = self.render(
            "partials/top_strip.html", strip=STRIP, live_updates_enabled=False
        )
        self.assertIn('aria-labelledby="top-strip-title"', rendered)
        self.assertIn('id="top-strip-title"', rendered)

    def test_top_strip_refresh_contract(self):
        non_sse = self.render(
            "partials/top_strip.html", strip=STRIP, live_updates_enabled=False
        )
        self.assertIn('hx-get="/partials/dashboard/top-strip"', non_sse)
        self.assertIn('hx-trigger="marketRefresh from:body"', non_sse)
        self.assertNotIn("every 90s", non_sse)
        self.assertNotIn("data-live-section", non_sse)

        sse = self.render(
            "partials/top_strip.html", strip=STRIP, live_updates_enabled=True
        )
        self.assertIn('data-live-section="top_strip"', sse)
        self.assertIn('data-live-event="section_changed"', sse)
        self.assertIn('data-live-url="/partials/dashboard/top-strip"', sse)
        self.assertNotIn("hx-get=", sse)
        self.assertNotIn("hx-trigger=", sse)
        self.assertNotIn("every 90s", sse)

    # ── merged briefing surface ──────────────────────────────────────────────

    def _render_briefing(self, live=False, briefing=None, sections=None, delta=None):
        return self.render(
            "partials/briefing_prose.html",
            briefing=(
                {"opinion_ids": ["op-1"], "sections": {"what_changed": "x"}}
                if briefing is None
                else briefing
            ),
            briefing_sections=sections if sections is not None else BRIEFING_SECTIONS,
            briefing_delta=delta if delta is not None else BRIEFING_DELTA,
            live_updates_enabled=live,
        )

    def test_briefing_merges_prose_delta_and_provenance(self):
        rendered = self._render_briefing(live=False)
        for label in (
            "What changed",
            "Current interpretation",
            "What would invalidate this",
        ):
            self.assertIn(label, rendered)
        # Visible changed-since-previous state.
        self.assertIn("Changed since previous briefing", rendered)
        for bullet in BRIEFING_DELTA["bullets"]:
            self.assertIn(bullet, rendered)
        self.assertIn("As of 2026-08-20", rendered)
        # One section, one disclosure.
        self.assertEqual(rendered.count('id="briefing-section"'), 1)
        self.assertEqual(rendered.count("<details"), 1)

    def test_briefing_disclosure_carries_atom_counts_and_evidence_hooks(self):
        rendered = self._render_briefing(live=False)
        self.assertIn("Briefing sources &amp; claims", rendered)
        # Atom counts live behind the disclosure.
        disclosure = rendered[rendered.index("<details"):]
        self.assertIn('class="atom-counts"', disclosure)
        self.assertIn("macro_series", disclosure)
        self.assertIn(">3<", disclosure)
        # Lazy evidence loading hooks preserved.
        self.assertIn("data-evidence-target", disclosure)
        self.assertIn('data-opinion-id="op-1"', disclosure)

    def test_briefing_refresh_contract(self):
        non_sse = self._render_briefing(live=False)
        self.assertIn('hx-get="/partials/briefing"', non_sse)
        self.assertIn('hx-trigger="marketRefresh from:body"', non_sse)
        self.assertNotIn("every 90s", non_sse)
        self.assertNotIn("cycleComplete", non_sse)
        self.assertNotIn("data-live-section", non_sse)

        sse = self._render_briefing(live=True)
        self.assertIn('data-live-section="briefing"', sse)
        self.assertIn('data-live-event="section_changed"', sse)
        self.assertIn('data-live-url="/partials/briefing"', sse)
        self.assertNotIn("hx-get=", sse)
        self.assertNotIn("hx-trigger=", sse)
        self.assertNotIn("every 90s", sse)

    def test_briefing_degrades_without_delta_context(self):
        # /partials/briefing route context today has no briefing_delta.
        rendered = self.render(
            "partials/briefing_prose.html",
            briefing={"opinion_ids": ["op-1"], "sections": {"what_changed": "x"}},
            briefing_sections=BRIEFING_SECTIONS,
            live_updates_enabled=False,
        )
        self.assertIn("What changed", rendered)
        self.assertIn("<details", rendered)
        self.assertIn("data-evidence-target", rendered)
        self.assertNotIn("delta-bullets", rendered)
        self.assertNotIn("atom-counts", rendered)
        self.assertNotIn("As of", rendered)

    def test_briefing_empty_state_is_truthful(self):
        rendered = self.render(
            "partials/briefing_prose.html",
            briefing=None,
            briefing_sections=[],
            briefing_delta=DELTA_UNAVAILABLE,
            live_updates_enabled=False,
        )
        self.assertIn("No briefing has been generated yet. Run a cycle to create one.", rendered)
        self.assertNotIn("delta-bullets", rendered)

    # ── briefing_delta.html compatibility target ─────────────────────────────

    def test_briefing_delta_remains_compatible_presentation_target(self):
        non_sse = self.render(
            "partials/briefing_delta.html",
            briefing_delta=BRIEFING_DELTA,
            live_updates_enabled=False,
        )
        self.assertIn("Briefing delta", non_sse)
        self.assertIn("Changed section: interpretation", non_sse)
        self.assertIn('class="atom-counts"', non_sse)
        self.assertIn('hx-get="/partials/dashboard/briefing-delta"', non_sse)
        self.assertIn('hx-trigger="marketRefresh from:body"', non_sse)
        self.assertNotIn("every 90s", non_sse)

        sse = self.render(
            "partials/briefing_delta.html",
            briefing_delta=BRIEFING_DELTA,
            live_updates_enabled=True,
        )
        self.assertIn('data-live-section="briefing_delta"', sse)
        self.assertIn('data-live-url="/partials/dashboard/briefing-delta"', sse)
        self.assertNotIn("hx-get=", sse)
        self.assertNotIn("hx-trigger=", sse)
        self.assertNotIn("every 90s", sse)

    def test_briefing_delta_unavailable_is_truthful(self):
        rendered = self.render(
            "partials/briefing_delta.html",
            briefing_delta=DELTA_UNAVAILABLE,
            live_updates_enabled=False,
        )
        self.assertIn("No briefing has been generated yet. Run a cycle to create one.", rendered)

    # ── cross-file refresh invariants ────────────────────────────────────────

    def test_no_legacy_polling_in_any_owned_template(self):
        for source in (self.dashboard, self.top_strip, self.briefing_prose, self.briefing_delta):
            self.assertNotIn("every 90s", source)
            self.assertNotIn("cycleComplete", source)


if __name__ == "__main__":
    unittest.main()
