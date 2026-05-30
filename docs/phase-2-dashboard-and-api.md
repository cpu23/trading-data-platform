# Phase 2 — Dashboard and API

**Version:** 1.0
**Status:** Ready for implementation
**Predecessor:** Phase 1 (data pipeline) — complete
**Operator:** Solo trader, local hardware (Kubuntu, Wayland)
**Companion document:** `docs/trading-data-infrastructure.md` (master architecture)

---

## 1. Purpose and Scope

### 1.1 What Phase 2 Delivers

A web-accessible interface for the trading data platform built in Phase 1. Three surfaces, served from a single FastAPI application:

1. **Dashboard** — single-page morning briefing view at `/`. HTMX-driven, server-rendered HTML. Loaded daily; surfaces current macro regime, upcoming catalysts, per-instrument briefing notes, and key macro indicators.
2. **JSON API** — read endpoints under `/api/*` plus a small number of write endpoints for triggering on-demand collection or processing. Designed to be consumed standalone (MT5 bridge, future automation).
3. **Logs view** — separate route at `/logs` for inspecting collection and processing history. Not part of the daily workflow; accessible when something looks wrong.

### 1.2 What Phase 2 Does Not Do

- Real-time data streaming or websockets (HTMX polling is sufficient at this update frequency)
- Multi-user authentication, user accounts, or role-based access (single operator)
- Mobile-optimised layouts (desktop-first; mobile is best-effort, not a target)
- Charting beyond the macro indicators section (per-instrument charts are deferred until market data lands in Phase 6)
- Thesis tracking (deferred indefinitely until use case clarifies)
- Alerting, notifications, or push (Phase 8)

### 1.3 Key Architectural Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Service split | Single FastAPI service, split routes (`routes/json/` and `routes/views/`) | MT5 bridge needs a real JSON API but doesn't justify a second container. Clean module boundary keeps the option open to lift the JSON API into its own service later. |
| Auth | HTTP Basic Auth via env var, applied to all routes | Localhost-only is fragile (Docker port-mapping mistakes, future Tailscale tunnel). Basic Auth covers the realistic risk envelope at near-zero cost. |
| Charting | Chart.js | Friendlier API, sufficient performance for the data volume (~2000 points per series). uPlot's performance edge doesn't matter at this scale. |
| Frontend | HTMX + Jinja2 + vanilla CSS | No build step, no SPA framework, server-rendered HTML fragments. Fast to ship, easy to debug, low memory footprint. |
| LLM-spend safety | `hx-confirm` on `/api/cycle` button only | "Be careful" doesn't extend to programmatic callers (MT5 bridge, future automation), but a confirmation modal on the cycle button covers the realistic browser failure modes. No server-side rate limiting. |
| Stale data handling | Visible warning per section when data exceeds configured thresholds | Silent staleness is the worst failure mode for an at-a-glance dashboard. Warning copy is generic ("data is X hours old") for simplicity. |
| Aesthetic | Restrained dark UI (Linear/Cursor/TradingView family) | Soft black background, hairline-bordered transparent cards, semantic colour reserved for meaning, traditional trading bias colours (green=bullish, red=bearish). |

### 1.4 Cross-cutting Dependency: Briefing Processor Restructure

The dashboard's per-instrument card grid requires the briefing processor to emit structured per-instrument output (bias, confidence, summary, note) rather than free-text. This is a Phase 1.5 prompt and schema change that lives inside Phase 2. See Section 5.

---

## 2. Repo Structure Changes

```
trading-system/
├── docker-compose.yml             # add `api` service
├── orchestrator/                  # unchanged
│   └── ...
├── api/                           # NEW
│   ├── Dockerfile
│   ├── pyproject.toml             # uv-managed
│   ├── main.py                    # FastAPI app, mounts routes
│   ├── auth.py                    # Basic Auth dependency
│   ├── config.py                  # loads same config.yaml as orchestrator
│   ├── db.py                      # SQLAlchemy session, query helpers
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── json/
│   │   │   ├── __init__.py        # router with prefix /api
│   │   │   ├── briefing.py
│   │   │   ├── regime.py
│   │   │   ├── opinions.py
│   │   │   ├── events.py
│   │   │   ├── macro.py
│   │   │   ├── watchlist.py
│   │   │   ├── system.py
│   │   │   └── triggers.py        # POST /api/collect, /process, /cycle
│   │   └── views/
│   │       ├── __init__.py        # router, no prefix
│   │       ├── dashboard.py       # GET /
│   │       ├── logs.py            # GET /logs
│   │       └── partials.py        # HTMX fragment endpoints
│   ├── templates/                 # Jinja2
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── logs.html
│   │   └── partials/
│   │       ├── regime_section.html
│   │       ├── events_section.html
│   │       ├── briefing_cards.html
│   │       ├── instrument_card.html
│   │       ├── indicators_section.html
│   │       ├── stale_warning.html
│   │       └── log_row.html
│   └── static/
│       ├── style.css              # single file, vanilla
│       ├── app.js                 # Chart.js init, minimal helpers
│       └── vendor/
│           ├── htmx.min.js        # vendored, not CDN-loaded
│           └── chart.min.js
└── docs/
    ├── trading-data-infrastructure.md
    └── phase-2-dashboard-and-api.md   # this document
```

**Notes on structure:**

- `api/` and `orchestrator/` are independent services that share `config.yaml` and the same database. They do not import each other.
- HTMX and Chart.js are vendored, not loaded from a CDN. This is a local-first system with no network dependency at page load; vendoring is consistent with that and avoids breakage if a CDN changes.
- The `routes/json/` modules return JSON via FastAPI's response model machinery. The `routes/views/` modules return `HTMLResponse` rendered through Jinja2.
- HTMX fragment endpoints live in `routes/views/partials.py` so the JSON API namespace stays clean for external consumers.

---

## 3. Database Changes

### 3.1 Briefing Processor Schema Change

The existing `daily_briefings.sections` JSONB column already accommodates structured data. The change is in what gets written into it, not the column shape.

**New section schema for `daily_briefings.sections`:**

```json
{
  "macro_summary": "...",
  "upcoming_events": "...",
  "regime_assessment": "...",
  "watchlist_notes": [
    {
      "symbol": "EURUSD",
      "asset_class": "forex",
      "bias": "bearish",
      "confidence": "moderate",
      "summary": "ECB doves outweigh; bearish into NFP risk.",
      "note": "Full paragraph with reasoning, context, and any caveats..."
    },
    ...
  ],
  "key_levels_context": "...",
  "action_items": "..."
}
```

`watchlist_notes` becomes an array of structured objects rather than a free-text section. Order matches the order in `config.yaml` watchlist.

**Migration:** Not required at the database level — the JSONB column accepts the new shape. Existing briefing records remain queryable but will not render correctly in the new UI. The dashboard handles missing-fields gracefully (see Section 6.4).

### 3.2 New Indices

For dashboard query performance:

```sql
-- Latest record per series (for indicators section)
CREATE INDEX IF NOT EXISTS idx_macro_series_latest
  ON macro_series (series_id, observed_at DESC);

-- Latest opinion per scope (for "current regime" lookup)
-- Already exists per arch doc Section 3.2 — verify in implementation.

-- Recent log entries (for /logs page)
CREATE INDEX IF NOT EXISTS idx_collection_log_started
  ON collection_log (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_processing_log_started
  ON processing_log (started_at DESC);
```

### 3.3 No Other Schema Work

No new tables. No column additions. The Phase 1 schema accommodates everything Phase 2 needs.

---

## 4. JSON API Specification

All endpoints are prefixed with `/api`. All responses are JSON. All endpoints require Basic Auth. Timestamps are ISO 8601 UTC. Numeric fields use tabular-friendly formats (no thousands separators in JSON).

### 4.1 Read Endpoints

```
GET  /api/briefing/latest
GET  /api/briefing/{date}
GET  /api/regime/current
GET  /api/regime/history?days=30
GET  /api/opinions/latest?limit=20
GET  /api/opinions/{type}?limit=20
GET  /api/events/upcoming?days=14
GET  /api/events/recent?days=7
GET  /api/macro/{series_id}?from=YYYY-MM-DD&to=YYYY-MM-DD
GET  /api/macro/dashboard
GET  /api/watchlist
GET  /api/system/health
GET  /api/system/logs?component=&status=&limit=50
```

### 4.2 Write Endpoints

```
POST /api/collect/{source_id}
POST /api/process/{processor_id}
POST /api/cycle
```

Write endpoints return `202 Accepted` with a job ID. The orchestrator handles execution; the API does not block waiting. Status of the run is queryable via `/api/system/logs`.

### 4.3 Response Shapes (Selected)

**`GET /api/briefing/latest`**

```json
{
  "briefing_id": "uuid",
  "briefing_date": "2026-04-26",
  "created_at": "2026-04-26T07:00:12Z",
  "stale": false,
  "stale_reason": null,
  "sections": {
    "macro_summary": "...",
    "regime_assessment": "...",
    "watchlist_notes": [
      {
        "symbol": "EURUSD",
        "asset_class": "forex",
        "bias": "bearish",
        "confidence": "moderate",
        "summary": "ECB doves outweigh; bearish into NFP risk.",
        "note": "..."
      }
    ],
    "action_items": "..."
  },
  "model_used": "qwen/qwen3.6-plus-04-02",
  "prompt_version": "briefing_v2"
}
```

**`GET /api/regime/current`**

```json
{
  "classification_id": "uuid",
  "created_at": "2026-04-26T06:30:01Z",
  "stale": false,
  "stale_reason": null,
  "scope": "global",
  "regime": "trending",
  "sub_regime": "risk_on",
  "direction": "bullish",
  "confidence": "high",
  "summary": "...",
  "key_factors": ["..."],
  "momentum_implications": "...",
  "caution_flags": ["..."],
  "opinion_id": "uuid"
}
```

**`GET /api/macro/dashboard`**

Returns latest values for the indicator set displayed in the dashboard's Key Indicators section. The indicator list is configurable (see Section 7.2):

```json
{
  "indicators": [
    {
      "series_id": "T10Y2Y",
      "label": "10Y-2Y spread",
      "category": "yield_curve",
      "latest_value": 0.42,
      "latest_observed_at": "2026-04-25",
      "previous_value": 0.38,
      "change_abs": 0.04,
      "change_pct": 10.5,
      "trend_5d": "up",
      "stale": false
    },
    ...
  ],
  "last_collector_run": "2026-04-26T06:00:14Z"
}
```

**`GET /api/system/health`**

```json
{
  "overall": "healthy",
  "components": [
    {
      "name": "fred",
      "kind": "collector",
      "last_run_at": "2026-04-26T06:00:14Z",
      "last_status": "success",
      "next_due_at": "2026-04-27T06:00:00Z",
      "stale": false
    },
    {
      "name": "macro_regime",
      "kind": "processor",
      "last_run_at": "2026-04-26T06:30:01Z",
      "last_status": "success",
      "stale": false
    },
    ...
  ],
  "today_llm_cost_usd": 0.32,
  "today_token_count": 18420
}
```

**`POST /api/cycle`**

Request body: empty. Response:

```json
{
  "job_id": "uuid",
  "accepted_at": "2026-04-26T08:14:22Z",
  "status_url": "/api/system/logs?correlation_id=<id>"
}
```

### 4.4 Error Responses

All errors return JSON in the standard FastAPI shape:

```json
{
  "detail": "Human-readable error message",
  "error_code": "STALE_DATA | NOT_FOUND | UPSTREAM_ERROR | ..."
}
```

`401 Unauthorized` on Basic Auth failure. `404` if a record is genuinely missing (no briefing for that date). `500` only for unexpected server errors — expected failures (a stale collector, a missing config entry) return `200` with a `stale: true` flag rather than failing the request.

### 4.5 Rate Limiting

None at the application level. The orchestrator's existing dependency-tracking ensures `/api/cycle` cannot run a second cycle while one is in flight; subsequent calls return `409 Conflict` with the running job's ID.

---

## 5. Briefing Processor Restructure

This is the only Phase 1 component that changes in Phase 2. The change is required for the dashboard's card grid to function.

### 5.1 Prompt Template Changes

`prompts/briefing_v2.txt` (new version, supersedes v1; v1 retained for audit trail). Bumps `prompt_version` field on the daily briefing record.

The watchlist section of the prompt is rewritten to request structured per-instrument output. The relevant excerpt:

```
For each instrument in the watchlist below, provide a structured assessment.

Watchlist:
{formatted_watchlist}

For each instrument, return a JSON object with:
- symbol: the instrument symbol
- asset_class: "forex" | "index" | "metal"
- bias: "bullish" | "bearish" | "neutral" | "mixed"
- confidence: "high" | "moderate" | "low"
- summary: ONE sentence, 8-15 words, the headline takeaway
- note: a paragraph (3-6 sentences) with reasoning, relevant macro context,
        upcoming catalysts, and any caveats. If there is nothing notable,
        return one sentence stating that explicitly — do not pad.

Return all instruments as an array under "watchlist_notes" in your response.
Maintain the order given above.
```

The rest of the briefing prompt template is unchanged.

### 5.2 Processor Code Changes

`orchestrator/processors/briefing.py`:

- Validate the `watchlist_notes` array shape after LLM response parsing. Required keys per item: `symbol`, `asset_class`, `bias`, `confidence`, `summary`, `note`.
- On validation failure, log the failure with full prompt and response (existing `processing_log` machinery handles this), then attempt one retry with a clarifying re-prompt before failing the run.
- Bias must be one of the four allowed values; coerce ambiguous values ("slightly bullish" → "bullish") and log the coercion.
- Confidence must be one of the three allowed values; coerce similarly.

### 5.3 Output Validation

Add a small validator in `orchestrator/processors/_validators.py`:

```python
def validate_briefing_sections(sections: dict, watchlist: list[dict]) -> tuple[bool, list[str]]:
    """Returns (is_valid, list_of_warnings). Used to log shape issues
    without failing the run. UI-level handling deals with missing data."""
```

### 5.4 Backwards Compatibility

The dashboard renders gracefully when:

- `watchlist_notes` is a string (old free-text format) — displays as a single prose block under a "Watchlist notes (legacy format)" header, no card grid.
- `watchlist_notes` is missing — displays nothing, no error.
- A specific instrument is missing fields — renders the card with what's present, falls back to "—" for the bias pill.

This handles the transition cleanly: the existing daily briefings remain readable, and the new structure powers the cards once the new prompt has run.

---

## 6. Dashboard Specification

### 6.1 Page Structure

Single page at `/`. Top-to-bottom order:

```
┌───────────────────────────────────────────────────────────┐
│  Header                                                   │
│    "Trading data" · last cycle: 26 Apr 06:30 UTC          │
│    [Run cycle] (with hx-confirm)                          │
├───────────────────────────────────────────────────────────┤
│  1. Macro regime                                          │
│     Headline + summary + caution flags                    │
├───────────────────────────────────────────────────────────┤
│  2. Upcoming events                                       │
│     Next 48h, high+medium impact, grouped by day          │
├───────────────────────────────────────────────────────────┤
│  3. Per-instrument cards                                  │
│     Card grid, click-to-expand                            │
├───────────────────────────────────────────────────────────┤
│  4. Key indicators                                        │
│     Configurable indicator strip with sparklines          │
├───────────────────────────────────────────────────────────┤
│  5. Daily briefing prose                                  │
│     macro_summary, regime_assessment, action_items        │
└───────────────────────────────────────────────────────────┘
```

No left navigation, no tabs. The page is a vertical scroll. There is one secondary route (`/logs`) reachable via a small footer link.

### 6.2 Section Specifications

#### 6.2.1 Header

- Compact strip across the top, ~48px tall.
- Left: page title "Trading data" in regular weight, plus secondary text showing the last full cycle's timestamp.
- Right: a single button "Run cycle" wired to `POST /api/cycle` via HTMX. `hx-confirm` attribute set to `"Run a full collection and analysis cycle? This will trigger LLM calls and incur cost."`. Button shows a spinner state via `hx-indicator` while the request is in flight.

#### 6.2.2 Macro regime

- Section header: "Macro regime" in 11px uppercase letter-spaced caption style (matching the aesthetic established in the card variants).
- Below: two-line headline displaying regime + sub_regime + direction + confidence in a single composed sentence — e.g. "Trending, risk-on · bullish · high confidence".
- Below that: the `summary` field as 14px body text.
- Below that: `key_factors` as a small bulleted list, three to five items.
- Below that: `caution_flags` as a list with subtle warning colour (amber accent), only rendered if non-empty.
- If stale: a 24px-tall warning strip appears at the top of the section: "Macro data is X hours old." No button.

#### 6.2.3 Upcoming events

- Section header: "Upcoming · next 48h".
- Events filtered to high + medium impact, grouped under day headers ("Today", "Tomorrow", weekday names beyond that).
- Each event row: `HH:MM UTC` (tabular numerals) · country flag or 2-letter code · event name · consensus · previous · impact dot.
- Row hover: subtle background lift to indicate interactivity (rows are not clickable yet — reserved for Phase 3+).
- If empty: a single muted line "No high-impact events in the next 48 hours."

#### 6.2.4 Per-instrument cards

The centrepiece. Render the watchlist (in config order) as a card grid using the **A — hairline border, transparent fill** treatment.

**Collapsed card:**
- Symbol (medium weight, 13px) + asset class (small caps, muted, 10px)
- Bias pill (right-aligned): bullish=green, bearish=red, neutral=muted grey, mixed=amber. Pill text is small caps, 10px, with subtle coloured fill at low opacity.
- Below: one-line `summary` (12px, line-height 1.5, slightly muted body colour).

**Expanded card (click to toggle, expanded card spans full grid row):**
- Everything from collapsed view, plus
- Full `note` paragraph below.

Nothing else expands. No upcoming-events filter, no macro context, no watch levels — those were considered and dropped from this phase.

**Grid behaviour:**
- `grid-template-columns: repeat(auto-fit, minmax(220px, 1fr))` with `gap: 10px`.
- Click anywhere on a collapsed card to expand. Click an expanded card again (or its dedicated chevron in the top-right) to collapse.
- Only one card can be expanded at a time? **No** — multiple expansions are allowed. Closing is manual. (Rationale: scanning multiple notes side-by-side is a real workflow.)
- Expansion state lives in the DOM only. Page reload collapses everything. No persistence.

**HTMX wiring:**
- The card grid is initially rendered server-side with all cards collapsed.
- Click does not trigger a server request; expansion is handled with a small client-side script (~10 lines vanilla JS, lives in `static/app.js`). HTMX is not needed for this interaction.

**Stale handling:** if the briefing is stale, a single warning strip appears above the entire card grid: "Briefing is X hours old. Run cycle to refresh."

#### 6.2.5 Key indicators

A compact horizontal strip showing the configured macro indicators (default set in Section 7.2) with:

- Label (e.g. "10Y-2Y spread")
- Latest value (tabular numerals, appropriate decimal places per indicator)
- Change since previous reading (small, signed, coloured: positive=green, negative=red — neutral colour if change is below a threshold)
- A 30-day sparkline rendered with Chart.js (line chart, no axes, no grid, no legend — pure shape)

Indicators are arranged in a CSS grid of 4-5 columns wide on a normal monitor, wrapping responsively. Clicking an indicator opens a modal with a full-size 1-year chart (Chart.js, with axes and gridlines this time). Modal close on backdrop click or escape.

**Stale handling:** if the FRED collector is stale, a warning strip above the section: "Macro data is X hours old."

#### 6.2.6 Daily briefing prose

- Section header: "Briefing".
- Renders the prose sections of the briefing in order: `macro_summary`, `regime_assessment`, `key_levels_context`, `action_items`. Each gets a small subheader.
- Generous reading width (max 65ch) — this is the only section in the dashboard that benefits from editorial-style layout.

### 6.3 Stale Data Thresholds

Configurable in `config.yaml`. Defaults:

```yaml
dashboard:
  stale_thresholds:
    briefing_hours: 18      # briefing older than 18h shows stale warning
    regime_hours: 18        # regime classification older than 18h
    macro_hours: 30         # FRED collector hasn't run in 30h
    events_hours: 8         # econ calendar collector hasn't run in 8h
```

Each section computes its own staleness from the latest record's `created_at` (for opinions/briefings) or the latest `collection_log` entry (for collectors). Warning copy is generic per the locked decision.

### 6.4 Empty and Error States

- **No briefing exists** (e.g. fresh database): regime, events, and indicators sections render normally. The cards section displays a single line: "No briefing has been generated yet. Run cycle to create one." The briefing prose section is hidden entirely.
- **Watchlist empty in config:** cards section displays "No watchlist instruments configured."
- **A specific section fails to load** (DB error, malformed data): that section renders a small error block with the error message and a "Retry" button (HTMX `hx-get` to the section's partial endpoint). The rest of the dashboard renders normally.

### 6.5 Refresh Behaviour

- **Page load:** server-renders the full page from the latest data in the DB.
- **No auto-refresh.** The dashboard does not poll. The user explicitly clicks "Run cycle" or reloads.
- Each section has its own partial endpoint (`/partials/regime`, `/partials/events`, etc.) that returns just that section's HTML. These are used by HTMX after a cycle completes (the `Run cycle` button's response triggers `hx-trigger="cycleComplete"` on each section), not for polling.

### 6.6 Dashboard Routes Summary

```
GET  /                          full dashboard
GET  /partials/regime           regime section HTML fragment
GET  /partials/events           events section fragment
GET  /partials/cards            cards section fragment
GET  /partials/indicators       indicators section fragment
GET  /partials/briefing         briefing prose fragment
GET  /partials/header           header fragment (last-cycle timestamp)
GET  /logs                      logs page
GET  /partials/logs             logs table fragment (for filter changes)
```

---

## 7. Visual Design System

### 7.1 Aesthetic Direction

**Reference family:** Linear, Cursor, TradingView, Finora landing page, X (dark mode), Claude Code desktop.

**Core principles:**

- **Restrained dark.** Soft black background (not pure black). Content carries the weight; chrome recedes.
- **Hairline-bordered, transparent surfaces.** Cards, panels, and section dividers are defined by 0.5px low-opacity borders with no fill change. Cards exist because of the line, not because they're trying to be physical objects.
- **Content-first hierarchy.** Type size, weight, and colour do the work. No decorative shadows, gradients, glows, or chrome.
- **Semantic colour only.** The dashboard is monochrome with the exception of: bias pills (green/red/grey/amber), warning strips (amber), interactive accents (none — hover states use brightness, not hue).
- **Dark by default.** No light-mode variant in this phase. (Adding one later is a CSS variable swap, but no automatic system-theme detection in v1.)

### 7.2 Colour Tokens

Defined as CSS custom properties on the `:root` selector:

```css
:root {
  /* Surfaces */
  --bg-base: #0F0F10;
  --bg-elevated: #161618;       /* used sparingly — modal backdrops, expanded card highlight */

  /* Borders */
  --border-hairline: rgba(255, 255, 255, 0.08);
  --border-emphasis: rgba(255, 255, 255, 0.14);

  /* Text */
  --text-primary: #E5E5E5;
  --text-secondary: #888888;
  --text-tertiary: #555555;
  --text-caption: #666666;

  /* Semantic — bias */
  --bull-fg: #4ADE80;
  --bull-bg: rgba(34, 197, 94, 0.12);
  --bear-fg: #F87171;
  --bear-bg: rgba(239, 68, 68, 0.12);
  --neutral-fg: #888888;
  --neutral-bg: rgba(255, 255, 255, 0.06);
  --mixed-fg: #FACC15;
  --mixed-bg: rgba(234, 179, 8, 0.12);

  /* Semantic — warnings & status */
  --warn-fg: #FACC15;
  --warn-bg: rgba(234, 179, 8, 0.08);
  --warn-border: rgba(234, 179, 8, 0.25);

  /* Semantic — change indicators */
  --change-up: #4ADE80;
  --change-down: #F87171;
  --change-flat: #888888;
}
```

### 7.3 Typography

- **Font stack:** `-apple-system, BlinkMacSystemFont, 'Inter', system-ui, sans-serif`. No web font loaded — system stack is fast, native-feeling, and consistent across the agents' likely test environments.
- **Sizes:**
  - Body: 13px (intentionally smaller than web default — reads as utilitarian, allows higher density without crowding)
  - Card summary: 12px
  - Section captions: 11px uppercase, letter-spacing 0.08em
  - Pills, tags: 10px uppercase, letter-spacing 0.04-0.06em
  - Headlines (regime headline): 16px medium weight
  - Modal titles: 15px medium weight
- **Weights:** 400 regular, 500 medium. Two weights only — heavier weights look out of place against the restrained backdrop.
- **Line height:** 1.5 for body, 1.6 for the briefing prose section, 1.4 for tight UI.
- **Numerals:** `font-variant-numeric: tabular-nums` applied globally to any element that displays a number. Non-negotiable — columns of figures must align.
- **Sentence case.** Never title case. Captions are uppercase but only for the small letter-spaced labels.

### 7.4 Spacing and Layout

- Page max-width: 1280px, centered, 24px horizontal padding.
- Section vertical rhythm: 32px between major sections.
- Card grid gap: 10px.
- Card internal padding: 14px 16px.
- Border radius: 8px on cards, 6px on smaller elements (pills, buttons).

### 7.5 Interactive States

- **Hover:** subtle brightness lift (`background: rgba(255, 255, 255, 0.02)` overlay). Borders may shift from `--border-hairline` to `--border-emphasis` on the affected element.
- **Active/pressed:** `transform: scale(0.99)` for buttons. Brief.
- **Focus:** 2px focus ring using `--border-emphasis`. Accessible without being decorative.
- **Disabled:** 40% opacity, cursor `not-allowed`.
- **In-flight HTMX request:** `aria-busy="true"` on the affected element, opacity drops to 60%, a small spinner appears (replaces the button label or sits inline next to a section header).

### 7.6 Charts

Chart.js global defaults overridden in `static/app.js`:

```javascript
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, Inter, sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.color = "#888888";
Chart.defaults.borderColor = "rgba(255, 255, 255, 0.08)";
Chart.defaults.scale.grid.color = "rgba(255, 255, 255, 0.05)";
Chart.defaults.scale.ticks.color = "#666666";
Chart.defaults.elements.point.radius = 0;       // no markers on lines
Chart.defaults.elements.line.borderWidth = 1.5;
Chart.defaults.plugins.legend.display = false;  // legends are noise; label inline
```

**Sparklines** in the indicators section: line only, no axes, no grid, no labels. 60px tall.

**Modal indicator charts:** axes shown, grid shown (very faint), x-axis time-formatted, y-axis numeric with appropriate precision per indicator.

**Line colour:** single-series charts use `--text-secondary` (`#888`). Avoid encoding direction in line colour — that's what the change indicator alongside the chart is for. If two series are shown together (e.g. 10Y vs 2Y yields), the second uses `--text-tertiary` (`#555`) to keep both quiet.

### 7.7 Logs Page Visual Style

The `/logs` page uses the same tokens but a denser layout:

- Tabular layout, no cards. Each log entry is a row in an HTML table.
- 12px body, 11px secondary columns.
- Status indicated by a leading coloured dot: success (muted green), partial (amber), failed (red).
- Row click expands inline to reveal: error traceback (collection_log) or full prompt + response + tokens + cost (processing_log).
- Filter controls at the top: component dropdown, status dropdown, date range.

---

## 8. Configuration Additions

Additions to `config.yaml`:

```yaml
api:
  host: 0.0.0.0          # bind address inside container; Docker maps to 127.0.0.1 only
  port: 8000
  basic_auth:
    username: ${DASHBOARD_USER}
    password: ${DASHBOARD_PASSWORD}

dashboard:
  stale_thresholds:
    briefing_hours: 18
    regime_hours: 18
    macro_hours: 30
    events_hours: 8

  indicators:
    # Order matters — controls display order in the indicators section.
    # `precision` controls decimal places. `category` is for grouping if needed later.
    - series_id: T10Y2Y
      label: "10Y-2Y spread"
      precision: 2
      category: yield_curve
    - series_id: VIXCLS
      label: "VIX"
      precision: 1
      category: volatility
    - series_id: DTWEXBGS
      label: "USD index (broad)"
      precision: 2
      category: usd
      note: "Trade-weighted; not the same as DXY"
    - series_id: BAMLH0A0HYM2
      label: "HY spread"
      precision: 2
      category: credit
    - series_id: DGS10
      label: "10Y yield"
      precision: 2
      category: rates
    - series_id: T5YIE
      label: "5Y breakeven"
      precision: 2
      category: inflation
```

The `note` field on an indicator surfaces as a small tooltip on hover, addressing the DXY-label issue flagged in the Phase 1 status notes.

Environment variables added to `.env`:

```
DASHBOARD_USER=trading
DASHBOARD_PASSWORD=<generate a strong password>
```

Docker Compose changes:

```yaml
api:
  build: ./api
  env_file: .env
  ports:
    - "127.0.0.1:8000:8000"     # bind to localhost only
  volumes:
    - ./config:/app/config
  depends_on:
    - postgres
  restart: unless-stopped
```

---

## 9. Build Sequence

The implementation work breaks into seven blocks, sequenced for incremental verification. Each block is small enough for a single agent session and ends in a verifiable state.

### Block 1: API skeleton

- Create `api/` directory, Dockerfile, `pyproject.toml`, `main.py`.
- Wire FastAPI app with Basic Auth dependency applied globally.
- Add Docker Compose entry, bound to 127.0.0.1.
- Implement `GET /api/system/health` end-to-end as the smoke test.
- **Verify:** `curl -u user:pass http://127.0.0.1:8000/api/system/health` returns valid JSON with collector statuses pulled from the live DB.

### Block 2: Read endpoints

- Implement all `GET /api/*` endpoints listed in Section 4.1.
- Each endpoint queries the database via SQLAlchemy and returns JSON. No business logic; the orchestrator owns analysis.
- Add the staleness computation as a shared helper called by relevant endpoints.
- **Verify:** every endpoint returns valid data when DB is populated, valid empty/stale responses when not.

### Block 3: Briefing processor restructure

- Write `prompts/briefing_v2.txt`.
- Update `orchestrator/processors/briefing.py` to validate the new structured output shape, with retry-on-malformed.
- Add the validator helper.
- Handle backwards compatibility (existing v1 records read as legacy format).
- **Verify:** `docker compose run orchestrator python cli.py process briefing` produces a daily briefing record where `sections.watchlist_notes` is an array of structured objects, all required fields present, bias values in the allowed enum.

### Block 4: Dashboard skeleton

- Set up Jinja2 templates: `base.html`, `dashboard.html`, partials for each section.
- Vendor HTMX and Chart.js into `static/vendor/`.
- Write `static/style.css` with the tokens from Section 7.
- Implement `GET /` returning a fully rendered dashboard with stub data (or live data if available).
- **Verify:** loading `http://127.0.0.1:8000/` after auth shows the dashboard layout with all sections visible (even if a section displays its empty state).

### Block 5: Dashboard interactivity

- Card expand/collapse with vanilla JS.
- Indicators sparklines via Chart.js.
- Modal full-size charts for indicator click.
- "Run cycle" button wired to `POST /api/cycle` with `hx-confirm` and the in-flight spinner.
- After-cycle partial refresh via `hx-trigger`.
- **Verify:** end-to-end — click "Run cycle", confirm, see the spinner, see sections refresh once the cycle completes (use a short test cycle for the first verification).

### Block 6: Logs page

- `GET /logs` template and route.
- `/partials/logs` for HTMX-driven filter changes.
- Row expansion (vanilla JS, same pattern as cards).
- **Verify:** loading `/logs` shows recent runs; filtering by component and status works; expanding a row shows the full detail.

### Block 7: Polish and bug fixes

- Stale-data warnings in their final positions and copy.
- Empty states verified for each section.
- Tabular numerals applied everywhere a number renders.
- DXY indicator note (tooltip on hover) implemented per config.
- Manual test pass against checklist in Section 10.

### Parallelisation Note

Blocks 1, 2, and 3 can run in parallel across two agents — backend (1+2) and orchestrator (3). Blocks 4 and 5 must run after Block 2 (the dashboard reads the API). Block 6 can run in parallel with Block 5. Block 7 is sequential and serves as a single-agent integration pass.

---

## 10. Validation Checklist

Phase 2 is complete when all of the following are true:

**Functional:**

- [ ] Dashboard loads at `http://127.0.0.1:8000/` after Basic Auth.
- [ ] All five dashboard sections render with live data.
- [ ] Per-instrument cards expand and collapse on click.
- [ ] "Run cycle" button triggers `POST /api/cycle` via HTMX with confirmation, and the dashboard sections refresh on completion.
- [ ] Each indicator in the strip shows a sparkline; clicking opens a full-size modal chart.
- [ ] `/logs` page loads, filters work, rows expand to reveal detail.
- [ ] All JSON API endpoints in Section 4.1 return valid data with correct shapes.
- [ ] Stale-data warnings appear when configured thresholds are exceeded; disappear when fresh.

**Briefing processor:**

- [ ] A run of `process briefing` produces a record where `sections.watchlist_notes` is an array of structured objects.
- [ ] Each watchlist instrument from config is present in the array.
- [ ] All required fields are populated and pass validation.
- [ ] Bias and confidence values fall within the allowed enums.
- [ ] Old (v1) briefing records render in the dashboard as a single legacy-format prose block, without errors.

**Aesthetic:**

- [ ] Cards use hairline borders only (no fill).
- [ ] Bias pills use traditional trading colours (green=bullish, red=bearish).
- [ ] Numbers use tabular numerals everywhere they appear.
- [ ] No drop shadows, gradients, or decorative effects anywhere.
- [ ] Dark-only; no light-mode toggle.

**Operational:**

- [ ] API service runs in Docker Compose alongside orchestrator and postgres.
- [ ] Port is bound to `127.0.0.1` only.
- [ ] Basic Auth credentials are loaded from `.env`, not hardcoded.
- [ ] HTMX and Chart.js are vendored, not loaded from CDN.
- [ ] Logs are written to the same structured-JSON format as Phase 1.

**Two-week soak test:**

After deployment, run the system for two weeks. Phase 2 is "validated" (not just "complete") when:

- The dashboard has been used as the primary morning briefing surface for at least ten trading sessions.
- No silent staleness has been observed (every actually-stale state surfaced visibly).
- No data has been displayed incorrectly (numbers truncated, fields swapped, dates in wrong timezone).
- The card grid and the briefing prose continue to be useful — if a section is being ignored after two weeks, that's a signal for redesign in Phase 3 or later.

---

## 11. Out-of-Scope Items (Recorded for Later)

These were considered during Phase 2 design and explicitly deferred. Recording them here so they're not lost:

- **Per-instrument expanded richness.** Watch levels, embedded charts, instrument-specific macro context, catalyst countdown timers. Deferred — start with the simple version, add based on what's actually missed.
- **Cost-tracking dashboard view.** Daily/weekly LLM spend trends. Add when spend grows or when a runaway cost incident makes it valuable.
- **Server-side rate limiting on POST endpoints.** Not added in this phase. Reconsider if MT5 bridge or other automation introduces real abuse risk.
- **Auto-refresh / polling.** Not added. The "Run cycle" button + stale warnings cover the workflow.
- **Light mode.** Not added. Token swap is straightforward when wanted.
- **Mobile-optimised layouts.** Not added. Responsive grid handles narrow viewports passably.
- **Thesis tracking.** Skipped per locked decision.
- **System health strip on the dashboard.** Skipped per locked decision — `/logs` and the stale warnings cover the use case.

---

## 12. Risk Register (Phase 2 Specific)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Briefing v2 prompt produces malformed structured output | Medium | High | Validation + one-retry-on-failure in processor; backwards-compatible UI handles both shapes |
| HTMX + vanilla JS interactions become unmaintainable as features grow | Low | Medium | Keep `app.js` small; if it exceeds ~200 lines, reconsider |
| Basic Auth feels annoying enough to disable | Low | Medium | Standard browser session caching is silent after first auth; resist the urge to disable |
| Chart.js performance issues with too many indicators | Low | Low | Cap indicators at ~12 visible at once; sparklines are pre-rendered SVG-equivalent |
| Stale thresholds tuned wrong (warnings too noisy or too quiet) | Medium | Low | Thresholds in config; tune based on two-week observation |
| Dashboard becomes a distraction from trading rather than a tool | Medium | Medium | Honest self-review at the two-week mark — if you're checking it more than 2-3 times daily, the design failed |

---

## 13. Decision Log (Phase 2 Additions)

| Decision | Choice | Rationale | Alternatives |
|---|---|---|---|
| Service split | Single FastAPI app, route-level split | MT5 needs JSON API but not a separate container; clean module boundary preserves future split option | Two services per arch doc (over-engineered now), no split (loses clean boundary for MT5) |
| Auth | Basic Auth | Cheapest defence-in-depth that survives Docker port-mapping mistakes and future Tailscale tunnels | None (fragile), token-on-POSTs (awkward middle ground) |
| Charting | Chart.js | Friendlier API, sufficient performance | uPlot (faster but quirky API at this scale), defer (would block dashboard) |
| Card border treatment | Variant A (hairline border, transparent fill) | Most disciplined match for the restrained dark aesthetic; cards as outlined regions, not objects | B (fill only — fades too much), C (fill + border — too much chrome), D (border-bottom only — too editorial) |
| Bias colour | Traditional trading: green/red | Matches the user's existing trading-tool mental model | Muted teal/coral (less alarming but unfamiliar), symbols only (loses at-a-glance readability) |
| LLM spend safety | `hx-confirm` on /api/cycle only | Covers realistic browser failure modes without server-side complexity | Server-side rate limit (overkill), nothing (vulnerable to MT5 bridge bugs) |
| Stale warning copy | Generic per section ("data is X hours old") | Simplicity over precision; user can investigate in /logs if needed | Section-specific copy (more nuanced but more strings to maintain), no warnings (silent staleness is the worst failure) |
| Briefing watchlist output | Restructured to array of typed objects | Card grid requires it; defer-and-parse-text was rejected for fragility | Free-text (current — fragile to parse), partial restructure (worse than full) |
| Auto-refresh | None — manual cycle only | Predictable; the user owns when to spend on LLM calls | Polling (cost), websockets (complexity), HTMX timed refresh (pointless without new data) |
| Light mode | Dark only | One mode well rather than two modes poorly | Auto / system-theme detection (deferred) |
| Logs view | Separate `/logs` route, not on dashboard | User specifically asked for accessible-but-not-visible | Dashboard widget (rejected by user), no view (fails Job A) |

---

## 14. Open Questions for Implementation

These were not nailed down in the spec phase and the implementing agents may need to make calls. Prefer the noted defaults; flag if substantively changed.

1. **Concurrent cycle handling:** if `POST /api/cycle` is called while a cycle is in flight, return `409 Conflict` with the running job's ID. Default: yes, return 409. Alternative: queue the second cycle.
2. **Card expand interaction on touchscreens:** click vs touch. Default: standard click event handles both. If touch behaviour is buggy, add explicit touch handlers.
3. **Time zone display:** all timestamps shown as UTC. Default: yes, per system. Local-time toggle deferred to a later phase.
4. **Modal indicator chart history range:** default 1 year. Alternatives: 6 months (less data, faster), 5 years (more context, slower). Tune after first use.
5. **Logs page row count:** default 50. Older entries via "Load more" (HTMX append) or pagination — implementor's choice, prefer the simpler option.
6. **Footer link to /logs:** small text-only link in the dashboard footer. Default: present, low-emphasis.

---

## 15. Glossary (Terms Used in This Document)

- **Briefing**: a `daily_briefings` record. One per day, generated by the briefing processor.
- **Card**: a per-instrument tile in the dashboard's main section. Renders one watchlist instrument's structured note.
- **Cycle**: one full run of all collectors followed by all dependent processors.
- **Partial**: an HTML fragment endpoint used by HTMX to refresh a section without reloading the page.
- **Stale**: a record whose `created_at` (or its source collector's last run) is older than the configured threshold for its data type.
- **Watchlist**: the set of instruments configured in `config.yaml` under `watchlist.trading`. Phase 2 renders these as per-instrument cards.
