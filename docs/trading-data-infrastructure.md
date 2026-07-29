# Trading Data Infrastructure — Architecture Document

> Historical foundation document. Several items described as future work here
> are now implemented, and authentication, storage, source, and processor
> contracts have changed. For current behavior, use
> [Current Architecture and Operations](current-architecture-and-operations.md).

**Version:** 1.0
**Purpose:** Modular data platform for discretionary trading context and investment research
**Operator:** Solo trader, local hardware
**Hardware:** Ryzen 7 7800X3D, 32GB DDR5 6000, RTX 5070 Ti, Kubuntu (Wayland)

---

## 1. System Overview

### 1.1 What This System Does

Collates, normalises, and analyses financial data to produce **structured macro opinions** and **catalyst awareness** for a momentum/expansion trader operating across forex, indices, and metals. Supports investment research across equities, index funds, commodities, and bonds.

The system does not make trading decisions. It provides context, regime classification, and event awareness that informs discretionary trading and position sizing for future automated strategies.

### 1.2 What This System Does Not Do

- Execute trades or manage orders (future scope, separate system)
- Process social sentiment (excluded by design — noise over signal)
- Ingest alternative data (excluded — poor cost/utility at retail scale)
- Operate in real-time or low-latency (updates a few times daily, on-demand available)
- Serve multiple users or run in the cloud

### 1.3 Core Design Principles

1. **Modular and replaceable** — each component has a defined interface; swap any data source, processor, or output without touching others
2. **Configuration-driven** — sources, schedules, API keys, model selections, and processing parameters live in config files, not code
3. **Idempotent ingestion** — every collector is safe to re-run; upsert logic prevents duplicates; backfills work the same as live runs
4. **Schema-on-write** — data is normalised into consistent formats at ingestion time
5. **Local-first** — everything runs on your machine; no cloud dependencies except upstream APIs
6. **Free where possible** — prioritise free/open data sources; LLM API costs are the accepted spend
7. **Verbose structured logging** — every API call, processing step, and LLM invocation is logged with timestamps, inputs, outputs, and durations

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         CONFIG (YAML)                          │
│  sources, schedules, API keys, model pins, logging levels      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                      COLLECTION LAYER                          │
│                                                                │
│  Each collector is a Python class implementing:                │
│    collect() -> list[dict]    (fetch + normalise)               │
│    source_id -> str           (unique identifier)              │
│    schedule -> str            (cron expression from config)    │
│                                                                │
│  Phase 1:          Future:                                     │
│  ┌──────────┐      ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │   FRED   │      │  EDGAR   │ │   News   │ │   CFTC   │     │
│  │Collector │      │Collector │ │Collector │ │Collector │     │
│  └────┬─────┘      └──────────┘ └──────────┘ └──────────┘     │
│  ┌────┴─────┐      ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│  │Econ Cal  │      │ Central  │ │  Broker  │ │ (custom) │     │
│  │Collector │      │ Bank     │ │  Market  │ │          │     │
│  └────┬─────┘      │ Comms    │ │  Data    │ │          │     │
│       │            └──────────┘ └──────────┘ └──────────┘     │
└───────┼─────────────────────────────────────────────────────────┘
        │
        │  normalised records
        │
┌───────▼─────────────────────────────────────────────────────────┐
│                       STORAGE LAYER                            │
│                                                                │
│  PostgreSQL 16 + TimescaleDB                                   │
│                                                                │
│  Raw data tables     │  Derived tables (LLM outputs)           │
│  ─────────────────   │  ──────────────────────────────          │
│  macro_series        │  structured_opinions                    │
│  econ_events         │  regime_classifications                 │
│  market_data         │  daily_briefings                        │
│  filings             │  analysis_runs (audit log)              │
│  news_items          │                                         │
│  cot_reports         │                                         │
│  cb_communications   │                                         │
│                      │                                         │
│  System tables       │                                         │
│  ─────────────────   │                                         │
│  collection_log      │                                         │
│  processing_log      │                                         │
└───────┬─────────────────────────────────────────────────────────┘
        │
        │  raw data read by
        │
┌───────▼─────────────────────────────────────────────────────────┐
│                      ANALYSIS LAYER                            │
│                                                                │
│  LLM-powered processors that read raw data and produce         │
│  structured opinions. Each processor:                          │
│    process(data) -> StructuredOpinion                           │
│    Uses pinned model via OpenRouter                            │
│    Logs full prompt, response, tokens, cost, duration          │
│                                                                │
│  ┌─────────────────┐  ┌─────────────────┐                      │
│  │ Macro Regime    │  │ Event Impact    │                      │
│  │ Classifier      │  │ Assessor        │                      │
│  └─────────────────┘  └─────────────────┘                      │
│  ┌─────────────────┐  ┌─────────────────┐                      │
│  │ Briefing        │  │ (future         │                      │
│  │ Generator       │  │  processors)    │                      │
│  └─────────────────┘  └─────────────────┘                      │
└───────┬─────────────────────────────────────────────────────────┘
        │
        │  structured opinions stored back in DB
        │
┌───────▼─────────────────────────────────────────────────────────┐
│                      OUTPUT LAYER                              │
│                                                                │
│  Phase 1: CLI briefing script                                  │
│  Phase 2: FastAPI + lightweight web dashboard                  │
│  Future:  Alerting, MT5 bridge, position sizing API            │
│                                                                │
│  All outputs read from derived tables — never from live APIs   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Database Schema

All timestamps stored as UTC. All tables include `created_at` and `updated_at` columns.

### 3.1 Raw Data Tables

#### macro_series (TimescaleDB hypertable, partitioned on observed_at)

| Column | Type | Notes |
|---|---|---|
| series_id | TEXT | FRED series code, e.g. "GDP", "CPIAUCSL" |
| observed_at | TIMESTAMPTZ | Observation date |
| value | DOUBLE PRECISION | |
| source | TEXT | "fred" |
| metadata | JSONB | Units, seasonal adjustment, frequency |
| **PK** | | (series_id, observed_at) |

#### econ_events

| Column | Type | Notes |
|---|---|---|
| event_id | TEXT | Generated hash of name + scheduled_at |
| event_name | TEXT | "US Non-Farm Payrolls" |
| country | TEXT | ISO 3166-1 alpha-2 |
| scheduled_at | TIMESTAMPTZ | When the release happens |
| impact_level | TEXT | "high", "medium", "low" |
| consensus | TEXT | Expected value (text — formats vary) |
| previous | TEXT | Previous release value |
| actual | TEXT | Actual value (null until released) |
| source | TEXT | |
| **PK** | | (event_id) |
| **Index** | | (scheduled_at) for calendar queries |

#### market_data (hypertable, partitioned on timestamp — future phase)

| Column | Type | Notes |
|---|---|---|
| symbol | TEXT | "EURUSD", "SPX500", "XAUUSD" |
| timestamp | TIMESTAMPTZ | |
| timeframe | TEXT | "1h", "4h", "1d" |
| open | DOUBLE PRECISION | |
| high | DOUBLE PRECISION | |
| low | DOUBLE PRECISION | |
| close | DOUBLE PRECISION | |
| volume | DOUBLE PRECISION | |
| source | TEXT | |
| **PK** | | (symbol, timeframe, timestamp) |

#### filings (future phase)

| Column | Type | Notes |
|---|---|---|
| filing_id | TEXT | EDGAR accession number |
| entity | TEXT | Company name |
| ticker | TEXT | Nullable — not all filers have tickers |
| filing_type | TEXT | "10-K", "10-Q", "8-K", "13F", "4" |
| filed_at | TIMESTAMPTZ | |
| period_end | DATE | Reporting period |
| raw_url | TEXT | EDGAR URL |
| raw_text | TEXT | Full text content |
| source | TEXT | |
| **PK** | | (filing_id) |

#### news_items (future phase)

| Column | Type | Notes |
|---|---|---|
| item_id | TEXT | Generated hash of url + published_at |
| headline | TEXT | |
| source | TEXT | "reuters", "ft", etc. |
| published_at | TIMESTAMPTZ | |
| url | TEXT | |
| body_snippet | TEXT | First ~500 chars or lead paragraph |
| full_text | TEXT | Nullable — depends on source access |
| relevance_tags | TEXT[] | Populated by LLM: asset classes, tickers |
| **PK** | | (item_id) |

#### cot_reports (future phase)

| Column | Type | Notes |
|---|---|---|
| report_date | DATE | |
| market | TEXT | "EURO FX", "GOLD", "E-MINI S&P 500" |
| category | TEXT | "commercial", "non_commercial", "nonreportable" |
| long_positions | INTEGER | |
| short_positions | INTEGER | |
| net_position | INTEGER | Computed: long - short |
| change_long | INTEGER | Week-over-week change |
| change_short | INTEGER | |
| open_interest | INTEGER | |
| source | TEXT | "cftc" |
| **PK** | | (report_date, market, category) |

#### cb_communications (future phase)

| Column | Type | Notes |
|---|---|---|
| comm_id | TEXT | Generated hash |
| institution | TEXT | "fed", "ecb", "boe", "boj" |
| comm_type | TEXT | "minutes", "speech", "statement", "dot_plot" |
| published_at | TIMESTAMPTZ | |
| speaker | TEXT | Nullable — for speeches |
| title | TEXT | |
| raw_text | TEXT | |
| raw_url | TEXT | |
| source | TEXT | |
| **PK** | | (comm_id) |

### 3.2 Derived Tables (LLM Outputs)

#### structured_opinions

| Column | Type | Notes |
|---|---|---|
| opinion_id | UUID | |
| created_at | TIMESTAMPTZ | When the analysis ran |
| opinion_type | TEXT | "macro_regime", "event_impact", "filing_summary", etc. |
| scope | TEXT | What this opinion covers — "global_macro", "EURUSD", "AAPL_10K" |
| direction | TEXT | "bullish", "bearish", "neutral", "mixed" |
| confidence | TEXT | "high", "moderate", "low" |
| timeframe | TEXT | "short_term", "medium_term", "long_term" |
| summary | TEXT | 2-3 sentence plain language summary |
| key_factors | JSONB | Array of factors driving the opinion |
| reasoning | TEXT | Longer form reasoning from the LLM |
| data_inputs | JSONB | References to the raw data used (table, IDs, date range) |
| model_used | TEXT | Exact model string from OpenRouter |
| prompt_version | TEXT | Version tag for the prompt template used |
| tokens_used | INTEGER | Total tokens consumed |
| cost_usd | DOUBLE PRECISION | Estimated API cost |
| **PK** | | (opinion_id) |
| **Index** | | (opinion_type, scope, created_at DESC) |

#### regime_classifications

| Column | Type | Notes |
|---|---|---|
| classification_id | UUID | |
| created_at | TIMESTAMPTZ | |
| scope | TEXT | "global", "us_equities", "forex_majors", etc. |
| regime | TEXT | "trending", "ranging", "volatile", "quiet" |
| sub_regime | TEXT | Nullable — "risk_on", "risk_off", "tightening", "easing" |
| confidence | TEXT | |
| supporting_data | JSONB | Which indicators drove this classification |
| opinion_id | UUID | FK to structured_opinions |
| **PK** | | (classification_id) |
| **Index** | | (scope, created_at DESC) for "current regime" queries |

#### daily_briefings

| Column | Type | Notes |
|---|---|---|
| briefing_id | UUID | |
| briefing_date | DATE | |
| created_at | TIMESTAMPTZ | |
| content | TEXT | Full briefing text |
| sections | JSONB | Structured: macro_summary, upcoming_events, regime, key_levels |
| opinion_ids | UUID[] | FKs to structured_opinions used |
| model_used | TEXT | |
| prompt_version | TEXT | |
| **PK** | | (briefing_id) |
| **Unique** | | (briefing_date) — one per day |

### 3.3 System Tables

#### collection_log

| Column | Type | Notes |
|---|---|---|
| log_id | UUID | |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | |
| collector | TEXT | source_id of the collector |
| status | TEXT | "success", "partial", "failed" |
| records_fetched | INTEGER | |
| records_written | INTEGER | |
| error_message | TEXT | Nullable |
| error_traceback | TEXT | Nullable |
| duration_ms | INTEGER | |
| api_calls_made | INTEGER | Number of upstream API calls |
| config_snapshot | JSONB | Relevant config at time of run |
| **PK** | | (log_id) |

#### processing_log

| Column | Type | Notes |
|---|---|---|
| log_id | UUID | |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | |
| processor | TEXT | "macro_regime", "event_impact", etc. |
| status | TEXT | |
| input_summary | JSONB | What data was fed in (table, date range, count) |
| output_id | UUID | FK to the opinion/classification produced |
| prompt_text | TEXT | Full prompt sent to LLM |
| raw_response | TEXT | Full LLM response |
| model_used | TEXT | |
| tokens_input | INTEGER | |
| tokens_output | INTEGER | |
| cost_usd | DOUBLE PRECISION | |
| duration_ms | INTEGER | |
| error_message | TEXT | Nullable |
| **PK** | | (log_id) |

---

## 4. Component Specifications

### 4.1 Collector Interface

Every collector implements this interface. This is deliberately simple — just a Python class with a few methods. No framework, no base class inheritance chain.

```python
class Collector:
    """Interface contract — all collectors implement these."""

    source_id: str          # unique identifier, e.g. "fred", "econ_calendar"

    def collect(self, config: dict) -> list[dict]:
        """
        Fetch data from upstream source.
        Returns list of normalised records ready for DB insertion.
        Each record is a dict matching the target table schema.

        Must be idempotent — safe to re-run without duplicates.
        Must handle its own error cases and return partial results
        if some requests fail.
        """
        ...

    def get_schedule(self, config: dict) -> str:
        """Return cron expression from config for this collector."""
        ...

    def health_check(self, config: dict) -> dict:
        """
        Verify upstream API is reachable and credentials are valid.
        Returns {"healthy": bool, "message": str, "latency_ms": int}
        """
        ...
```

**Key rules for collectors:**

- Each collector normalises its own output. No separate normalisation layer — that's unnecessary abstraction at this scale.
- Collectors never write directly to the database. They return data to the orchestrator, which handles writing, deduplication, and logging.
- All HTTP requests use a shared utility with retry logic, timeout handling, and response logging.
- Config (API keys, series lists, endpoints) comes from the YAML config, never hardcoded.

### 4.2 Phase 1 Collectors

#### FRED Collector

**Source:** FRED API (https://fred.stlouisfed.org/docs/api/)
**API key:** Free, register at FRED
**Rate limits:** 120 requests per minute (generous for our use)
**Schedule:** Daily at 06:00 UTC (most releases happen US morning)
**Target table:** `macro_series`

**Series to collect (configured in YAML, start with these):**

```yaml
fred:
  api_key: ${FRED_API_KEY}
  schedule: "0 6 * * *"
  series:
    # GDP and growth
    - id: GDP
      frequency: quarterly
    - id: GDPC1           # Real GDP
      frequency: quarterly

    # Inflation
    - id: CPIAUCSL         # CPI All Urban Consumers
      frequency: monthly
    - id: PCEPILFE         # Core PCE (Fed's preferred measure)
      frequency: monthly
    - id: T5YIE            # 5-Year Breakeven Inflation Rate
      frequency: daily
    - id: T10YIE           # 10-Year Breakeven Inflation Rate
      frequency: daily

    # Employment
    - id: UNRATE           # Unemployment Rate
      frequency: monthly
    - id: PAYEMS           # Total Nonfarm Payrolls
      frequency: monthly
    - id: ICSA             # Initial Jobless Claims
      frequency: weekly

    # Interest rates and yield curve
    - id: FEDFUNDS         # Federal Funds Rate
      frequency: monthly
    - id: DGS2             # 2-Year Treasury
      frequency: daily
    - id: DGS10            # 10-Year Treasury
      frequency: daily
    - id: T10Y2Y           # 10Y-2Y Spread (yield curve)
      frequency: daily
    - id: T10Y3M           # 10Y-3M Spread
      frequency: daily

    # Financial conditions
    - id: BAMLH0A0HYM2     # High Yield Spread (risk appetite)
      frequency: daily
    - id: VIXCLS           # VIX
      frequency: daily

    # Money and credit
    - id: M2SL             # M2 Money Supply
      frequency: monthly

    # USD strength
    - id: DTWEXBGS         # Trade-Weighted USD Index (Broad)
      frequency: daily
```

The series list is config — add or remove without code changes.

**Collection logic:**

1. For each series, request observations from last collection date to today
2. FRED returns JSON with observation date and value
3. Normalise to `macro_series` schema
4. Return list of records (orchestrator handles upsert on PK)

#### Economic Calendar Collector

**Source:** To be determined — evaluate in order of preference:
1. Investing.com RSS/scrape (free, comprehensive, may break)
2. ForexFactory (free, well-structured, scrape-dependent)
3. TradingEconomics API (free tier limited but stable)
4. FXStreet calendar

**Schedule:** Every 6 hours (events get updated as releases happen)
**Target table:** `econ_events`

**Collection logic:**

1. Fetch calendar for current week + next week
2. Parse events: name, country, time, impact level, consensus, previous, actual
3. Normalise to `econ_events` schema
4. Upsert — existing events get `actual` field updated when releases happen

**Filtering (configured in YAML):**

```yaml
econ_calendar:
  schedule: "0 */6 * * *"
  countries: ["US", "GB", "EU", "JP", "CN", "AU", "CA", "CH"]
  min_impact: "medium"   # skip low-impact releases
  lookback_days: 7
  lookahead_days: 14
```

### 4.3 Analysis Layer

#### Processor Interface

```python
class Processor:
    """Interface contract for LLM-powered analysis processors."""

    processor_id: str      # "macro_regime", "event_impact", etc.

    def process(self, db_session, config: dict) -> dict:
        """
        Query raw data from DB, construct prompt, call LLM,
        parse response into structured opinion.

        Returns dict matching structured_opinions schema.

        Must log: full prompt, full response, model, tokens, cost, duration.
        """
        ...

    def get_prompt_version(self) -> str:
        """Return version string for current prompt template."""
        ...
```

#### Macro Regime Classifier (Phase 1)

**Input:** Latest values from `macro_series` for all tracked indicators
**Output:** One `structured_opinion` + one `regime_classification`
**Schedule:** Runs after FRED collector completes
**Model:** Pinned via OpenRouter config

**What it does:**

Constructs a prompt containing the current state of all macro indicators, their recent trends (direction and magnitude of change), and key cross-indicator relationships (yield curve shape, credit spreads vs VIX, USD strength vs risk assets). Asks the LLM to classify the current regime and provide a structured opinion.

**Prompt template structure (versioned, stored as a file):**

```
You are a macro analyst. Given the following current economic data,
classify the current macro regime and provide a structured assessment.

## Current Data
{formatted_indicator_table}

## Recent Changes (vs previous reading)
{formatted_changes_table}

## Key Relationships
- Yield curve (10Y-2Y): {value} ({interpretation})
- Credit spreads: {value} ({direction})
- VIX: {value} ({level_context})
- USD index: {value} ({direction})

Respond in the following JSON format:
{
  "regime": "trending | ranging | volatile | quiet",
  "sub_regime": "risk_on | risk_off | tightening | easing | null",
  "direction": "bullish | bearish | neutral | mixed",
  "confidence": "high | moderate | low",
  "timeframe": "short_term | medium_term",
  "summary": "2-3 sentence plain language summary",
  "key_factors": ["factor 1", "factor 2", "factor 3"],
  "reasoning": "Detailed reasoning paragraph",
  "momentum_implications": "What this means for trend-following strategies",
  "caution_flags": ["any warning signs or regime change indicators"]
}
```

The `momentum_implications` field is specifically for your expansion system — it should flag whether the macro environment favours momentum continuation or suggests chop risk.

#### Event Impact Assessor (Phase 1)

**Input:** Upcoming events from `econ_events` within next 48 hours
**Output:** One `structured_opinion` per high-impact upcoming event
**Schedule:** Runs after econ calendar collector completes
**Model:** Pinned via OpenRouter config

**What it does:**

For each upcoming high-impact event, provides context on what the consensus expects, what a deviation would mean, and which of your watched assets are most likely to be affected.

**Prompt template includes:**

- Event name, consensus, previous value, historical surprise frequency
- Your watchlist (from config) so the LLM can map impact to your specific instruments
- Request for volatility expectation and directional bias if consensus is met vs missed

#### Daily Briefing Generator (Phase 1)

**Input:** Latest regime classification + upcoming events + any recent structured opinions
**Output:** One `daily_briefing` record
**Schedule:** Daily at 07:00 UTC (after FRED and econ calendar have run)

**What it does:**

Synthesises all current analysis into a single morning briefing. Structured sections:

```json
{
  "macro_summary": "Current regime and key developments",
  "upcoming_events": "Next 48 hours of catalysts with impact assessment",
  "regime_assessment": "Is this a momentum-friendly or chop-risk environment",
  "watchlist_notes": "Per-instrument context for your trading watchlist",
  "key_levels_context": "Macro factors that might drive moves to watch for",
  "action_items": "Anything requiring attention or preparation"
}
```

The dashboard renders these sections as concise bullets where possible. If a
section only contains long prose, the view conservatively splits it into a small
number of sentence-level bullets for readability. Briefing output remains
informational context only; it is not rendered as trade instructions or entry
signals.

### Dashboard Presentation Layer

The dashboard is a thin presentation layer over stored macro, calendar,
briefing, price, and health data. It intentionally keeps display helpers
separate from stored records:

- The main catalyst ribbon shows HIGH impact events only, sorted
  chronologically, with weekday plus London/NY time.
- The expandable full calendar retains HIGH and MEDIUM event detail grouped by
  day.
- Expanded asset cards can show matched HIGH and MEDIUM catalysts based on a
  simple exposure map, such as EUR+USD for EURUSD or GBP/UK/BoE for UK100.
- Asset-event matching is non-authoritative and display-only. It does not
  mutate `econ_events`, daily briefings, structured opinions, or strategy logic.
- Source freshness is shown with subtle section dots and hover details rather
  than prominent warning banners.
- Logs poll every few seconds while the page is open, and the header refreshes
  after a cycle finishes so cost/token usage stays current.
- Initial dashboard loaders execute concurrently rather than serializing
  independent database and internal-service reads. Section failures retain
  local fallbacks.
- One consolidated health result supplies both detailed component state and the
  compact header status. The orchestrator bounds its expensive quality snapshot
  to a 30-second default TTL, while the Quality page remains a live diagnostic.
- The macro indicator strip is populated by one batched query using lateral
  index probes for current/previous values and a bounded trend aggregate.

### 4.4 Orchestrator

The orchestrator is a simple Python script that:

1. Reads config
2. Checks which collectors are due to run (based on schedule + last run time from `collection_log`)
3. Runs due collectors, captures output, writes to DB, logs to `collection_log`
4. Runs analysis processors that depend on updated data, logs to `processing_log`
5. Handles errors gracefully — a failed collector doesn't block others

**Not a framework.** This is a single Python file with a `run()` function. It uses APScheduler or a simple cron-like check loop. The scheduler runs as a Docker container that stays alive.

For on-demand runs, the same orchestrator exposes a simple function that can be called by the CLI tool or (in phase 2) by the FastAPI backend:

```python
def run_collector(source_id: str) -> CollectionResult:
    """Run a specific collector on demand."""
    ...

def run_processor(processor_id: str) -> ProcessingResult:
    """Run a specific analysis processor on demand."""
    ...

def run_full_cycle() -> CycleResult:
    """Run all due collectors, then all dependent processors."""
    ...
```

---

## 5. Configuration Structure

Single YAML file with environment variable substitution for secrets:

```yaml
# config.yaml

database:
  host: postgres          # Docker service name
  port: 5432
  name: trading_data
  user: ${DB_USER}
  password: ${DB_PASSWORD}

llm:
  provider: openrouter
  api_key: ${OPENROUTER_API_KEY}
  default_model: "anthropic/claude-sonnet-4-20250514"  # example — pin your choice
  models:
    macro_regime: "anthropic/claude-sonnet-4-20250514"
    event_impact: "anthropic/claude-sonnet-4-20250514"
    briefing: "anthropic/claude-sonnet-4-20250514"
  temperature: 0.2         # low for consistency
  max_retries: 3

logging:
  level: DEBUG
  format: structured_json
  output:
    - stdout
    - file: /var/log/trading-data/app.log
  rotate:
    max_size_mb: 100
    keep_files: 30

collectors:
  fred:
    enabled: true
    api_key: ${FRED_API_KEY}
    schedule: "0 6 * * *"
    series:
      # ... series list as shown above

  econ_calendar:
    enabled: true
    schedule: "0 */6 * * *"
    source: investingcom     # can swap to forexfactory, tradingeconomics
    countries: ["US", "GB", "EU", "JP", "CN", "AU", "CA", "CH"]
    min_impact: medium
    lookback_days: 7
    lookahead_days: 14

  # Future collectors — disabled by default
  edgar:
    enabled: false
    schedule: "0 8 * * *"
    # ...

  news:
    enabled: false
    # ...

processors:
  macro_regime:
    enabled: true
    depends_on: [fred]
    prompt_template: prompts/macro_regime_v1.txt
    schedule: after_dependency   # runs when FRED collector completes

  event_impact:
    enabled: true
    depends_on: [econ_calendar]
    prompt_template: prompts/event_impact_v1.txt
    schedule: after_dependency

  briefing:
    enabled: true
    depends_on: [macro_regime, event_impact]
    prompt_template: prompts/briefing_v1.txt
    schedule: "0 7 * * *"

watchlist:
  trading:
    - symbol: EURUSD
      type: forex
    - symbol: GBPUSD
      type: forex
    - symbol: USDJPY
      type: forex
    - symbol: SPX500
      type: index
    - symbol: NAS100
      type: index
    - symbol: XAUUSD
      type: metal
    - symbol: XAGUSD
      type: metal
    # add up to ~10

  investing:
    watchlists:
      - name: index_funds
        symbols: [SPY, QQQ, VTI, VXUS]
      - name: sectors
        symbols: []         # populate as you research
    # investing watchlist can be larger — these are for context, not real-time
```

### 5.1 Environment Variables

Stored in `.env` file (Docker Compose loads automatically, git-ignored):

```
DB_USER=trading
DB_PASSWORD=<generate a strong password>
FRED_API_KEY=<from FRED website>
OPENROUTER_API_KEY=<from OpenRouter>
```

---

## 6. Logging Architecture

### 6.1 Philosophy

Every operation produces a structured JSON log entry. Too much detail is better than too little. Logs are the first place you look when something breaks and the primary tool for evaluating system performance over time.

### 6.2 Log Structure

Every log entry contains:

```json
{
  "timestamp": "2026-04-12T06:00:01.234Z",
  "level": "INFO",
  "component": "collector.fred",
  "action": "fetch_series",
  "details": {
    "series_id": "CPIAUCSL",
    "records_fetched": 1,
    "api_response_ms": 342,
    "http_status": 200
  },
  "correlation_id": "run-2026-04-12-0600-abc123"
}
```

The `correlation_id` ties together all log entries from a single orchestrator run, so you can trace an entire cycle from collection through analysis.

### 6.3 What Gets Logged

**Collection layer:**
- Start/end of each collector run with duration
- Every HTTP request: URL, status code, response time, response size
- Number of records fetched and written
- Any records skipped (duplicates, validation failures) with reason
- Full error tracebacks on failure

**Analysis layer:**
- Full prompt text sent to LLM
- Full raw response from LLM
- Parsed structured output
- Model used, tokens in/out, estimated cost
- Duration of API call
- Prompt version used
- Any parsing failures (LLM returned invalid JSON, etc.)

**Database operations:**
- Records inserted/updated per table
- Any constraint violations or upsert conflicts
- Query durations for anything over 100ms

### 6.4 Log Storage

Logs go to stdout (visible in Docker logs) and to a rotating file. The `collection_log` and `processing_log` database tables store structured summaries for querying through the dashboard. The file logs contain the full verbose detail for debugging.

---

## 7. Docker Compose Layout

```yaml
# docker-compose.yml

services:
  postgres:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_USER: ${DB_USER}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: trading_data
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init:/docker-entrypoint-initdb.d    # schema creation scripts
    ports:
      - "5432:5432"          # expose for direct DB access during development
    restart: unless-stopped

  orchestrator:
    build: ./orchestrator
    env_file: .env
    volumes:
      - ./config:/app/config
      - ./prompts:/app/prompts
      - ./logs:/var/log/trading-data
    depends_on:
      - postgres
    restart: unless-stopped

  # Phase 2 additions:
  # api:
  #   build: ./api
  #   env_file: .env
  #   ports:
  #     - "8000:8000"
  #   depends_on:
  #     - postgres
  #   restart: unless-stopped

  # dashboard:
  #   build: ./dashboard
  #   ports:
  #     - "3000:3000"
  #   depends_on:
  #     - api
  #   restart: unless-stopped

volumes:
  pgdata:
```

### 7.1 Directory Structure

```
trading-data-platform/
├── docker-compose.yml
├── .env                          # secrets (git-ignored)
├── .env.example                  # template without real values
├── config/
│   └── config.yaml
├── prompts/
│   ├── macro_regime_v1.txt
│   ├── event_impact_v1.txt
│   └── briefing_v1.txt
├── db/
│   └── init/
│       ├── 001_extensions.sql    # CREATE EXTENSION timescaledb;
│       ├── 002_raw_tables.sql
│       ├── 003_derived_tables.sql
│       └── 004_system_tables.sql
├── orchestrator/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                   # entry point — scheduler loop
│   ├── orchestrator.py           # run_collector, run_processor, run_full_cycle
│   ├── db.py                     # database connection and query helpers
│   ├── logging_config.py         # structured logging setup
│   ├── http_client.py            # shared HTTP with retries, logging
│   ├── llm_client.py             # OpenRouter wrapper with logging
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── fred.py
│   │   ├── econ_calendar.py
│   │   └── ...                   # future collectors
│   ├── processors/
│   │   ├── __init__.py
│   │   ├── macro_regime.py
│   │   ├── event_impact.py
│   │   ├── briefing.py
│   │   └── ...                   # future processors
│   └── cli.py                    # CLI tool for on-demand runs and briefing
├── api/                          # Phase 2
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                   # FastAPI app
│   ├── routes/
│   └── ...
├── dashboard/                    # Phase 2
│   ├── Dockerfile
│   ├── ...
└── README.md
```

---

## 8. Phase 2: Dashboard and API

### 8.1 FastAPI Backend

Lightweight API that reads from the database and exposes endpoints for the dashboard. Does not contain business logic — all analysis happens in the orchestrator.

**Core endpoints:**

```
GET  /api/briefing/latest           Latest daily briefing
GET  /api/briefing/{date}           Briefing for a specific date
GET  /api/regime/current            Current macro regime classification
GET  /api/regime/history            Regime history over time
GET  /api/opinions/latest           Recent structured opinions
GET  /api/opinions/{type}           Opinions filtered by type
GET  /api/events/upcoming           Economic calendar (next 14 days)
GET  /api/events/recent             Recent releases with actual vs consensus
GET  /api/macro/{series_id}         Time series data for a specific indicator
GET  /api/macro/dashboard           Batched key-indicator values + trends
GET  /api/watchlist                 Current watchlist from config
GET  /api/system/health             Local state + bounded orchestrator quality snapshot
GET  /api/system/logs               Recent collection and processing logs
POST /api/collect/{source_id}       Trigger on-demand collection
POST /api/process/{processor_id}    Trigger on-demand analysis
POST /api/cycle                     Trigger full collection + analysis cycle
```

### 8.2 Dashboard Frontend

**Approach:** Lightweight, server-rendered with minimal client-side JavaScript. The stack:

- **HTMX** for dynamic updates without a full SPA framework — sends HTML fragments over the wire, minimal JS footprint, fast rendering
- **Jinja2 templates** served by FastAPI
- **Vanilla CSS** with a clean, modern design system — rounded corners, subtle shadows, muted colour palette, good typography
- **A small amount of JS** only where needed: charting (lightweight library like uPlot or Chart.js), and the on-demand refresh buttons

This gives you a modern, clean UI that loads fast and uses negligible RAM compared to a React SPA. No node_modules, no build step, no webpack. The dashboard is server-rendered HTML with HTMX handling partial page updates.

**Dashboard layout (single page, sections):**

```
┌─────────────────────────────────────────────────────────────┐
│  TRADING DATA PLATFORM                    Last updated: ... │
│  [Refresh All]  [System Health: ●]                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─── MACRO REGIME ──────────────────────────────────────┐  │
│  │ Current: TRENDING (Risk-On) | Confidence: High        │  │
│  │ Momentum-friendly: Yes                                │  │
│  │ Summary: ...                                          │  │
│  │ Caution flags: ...                                    │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─── UPCOMING EVENTS ───────────────────────────────────┐  │
│  │ Today                                                 │  │
│  │  14:30 UTC  US CPI (YoY)     Exp: 2.8%  Prev: 2.9%  │  │
│  │  ● HIGH IMPACT — likely to move USD pairs, gold       │  │
│  │                                                       │  │
│  │ Tomorrow                                              │  │
│  │  ...                                                  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─── KEY INDICATORS ────────────────────────────────────┐  │
│  │ Yield curve (10Y-2Y): +0.42  ▲                       │  │
│  │ VIX: 14.2  ▼                                         │  │
│  │ DXY: 103.8  ▼                                        │  │
│  │ HY Spread: 3.1%  ─                                   │  │
│  │ [expandable charts]                                   │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─── DAILY BRIEFING ────────────────────────────────────┐  │
│  │ [Full briefing text, collapsible sections]            │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌─── SYSTEM LOG ────────────────────────────────────────┐  │
│  │ Last 24h collection/processing runs with status       │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 9. Future Expansion Points

These are the interfaces where future systems plug in. The architecture accommodates them without requiring redesign.

### 9.1 Additional Collectors (plug into collection layer)

| Collector | Source | Table | Notes |
|---|---|---|---|
| EDGAR filings | SEC EDGAR API | filings | 10-K, 10-Q, 8-K, 13F, Form 4 |
| News | RSS feeds, NewsAPI, or Benzinga | news_items | LLM tags relevance to watchlist |
| CFTC COT | CFTC bulk CSV downloads | cot_reports | Weekly positioning data |
| Central bank comms | Fed/ECB/BOE/BOJ websites | cb_communications | Minutes, speeches, statements |
| Broker market data | IB API or Polygon.io | market_data | OHLCV for watchlist instruments |

Each is a new Python file in `collectors/`, a new section in config, and possibly a new DB table. Nothing else changes.

### 9.2 Additional Processors (plug into analysis layer)

| Processor | Input | Output | Notes |
|---|---|---|---|
| Earnings analyser | filings (10-Q, 10-K) | structured_opinion | Extract key metrics, compare to consensus |
| News classifier | news_items | structured_opinion | Relevance scoring, catalyst detection |
| COT positioning | cot_reports | structured_opinion | Crowding indicators, positioning shifts |
| Fed language tracker | cb_communications | structured_opinion | Hawkish/dovish scoring, language diff |
| Cross-asset regime | market_data + macro_series | regime_classification | Multi-asset trend/chop classification |

Each is a new Python file in `processors/`, a new prompt template in `prompts/`, and a new section in config.

### 9.3 Additional Output Channels (plug into output layer)

| Output | Purpose | Interface |
|---|---|---|
| Telegram/Discord bot | Push alerts for high-impact events, regime changes | Reads from structured_opinions, sends on trigger conditions |
| MT5 bridge | Feed context to MetaTrader for position sizing | Reads from regime_classifications, exposes via local API or file |
| Position sizing API | Automated strategies query current regime and vol estimate | Additional FastAPI endpoint |
| Research notebook | Jupyter integration for deeper analysis | Direct DB connection, shared SQLAlchemy models |

### 9.4 Investing Research Extensions

The investing use case shares the same data backbone but needs additional processing:

- **Sector/industry screening:** EDGAR data + LLM analysis to identify interesting sectors based on macro regime
- **Earnings calendar tracking:** Upcoming earnings for watchlist stocks with historical surprise rates
- **Institutional positioning:** 13F analysis to track what large funds are buying/selling
- **Thesis tracking:** A structured way to log your investment theses and the data points that would confirm or invalidate them (this is a future dashboard feature)

---

## 10. Phased Build Sequence

### Phase 1: Data Pipeline (build first, validate before moving on)

**Goal:** End-to-end data collection, analysis, and structured opinion generation. Verify the system produces useful output before investing in UI.

**Build order:**

1. Docker Compose with PostgreSQL + TimescaleDB
2. Database schema (init scripts)
3. Shared utilities: DB connection, HTTP client, logging, LLM client
4. FRED collector
5. Orchestrator (run single collector, write to DB, log)
6. Macro regime processor (first LLM integration)
7. Economic calendar collector
8. Event impact processor
9. Daily briefing generator
10. CLI tool: `python cli.py briefing` prints today's briefing to terminal

**Validation criteria before moving to Phase 2:**
- Both collectors run reliably on schedule for 2+ weeks
- LLM analysis produces consistent, useful structured opinions
- Logging captures enough detail to diagnose any issue
- You've iterated on prompt templates based on output quality
- You have a feel for which data is actually useful to your trading

### Phase 2: Dashboard

**Goal:** Web-based access to all data and analysis.

**Build order:**

1. FastAPI backend with core read endpoints
2. Dashboard HTML/CSS/HTMX — macro regime + upcoming events sections
3. Key indicators section with simple charts
4. Daily briefing display
5. System health and log viewer
6. On-demand refresh (POST endpoints + HTMX triggers)

### Future Phases (sequence based on value to your trading)

- **Phase 3:** News collector + classifier (highest value add after macro)
- **Phase 4:** EDGAR collector + earnings analyser (investing use case)
- **Phase 5:** Central bank comms + language tracker
- **Phase 6:** Broker market data + cross-asset regime classification
- **Phase 7:** COT reports + positioning analysis
- **Phase 8:** Alerting (Telegram/Discord)
- **Phase 9:** MT5 bridge for position sizing
- **Phase 10:** Investing research features (thesis tracking, sector screening)

This ordering prioritises what adds the most context to your current trading style. Adjust based on what you actually find useful after Phase 1.

---

## 11. Key Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Free data source breaks (API change, scraping blocked) | High | Medium | Config-driven source selection; collector swap doesn't affect downstream |
| LLM produces inconsistent/unhelpful opinions | Medium | High | Prompt versioning; structured output validation; temperature pinning; compare versions against historical data |
| Scope creep — adding sources before validating existing ones | High | Medium | Strict phased approach; validation criteria before advancing |
| Docker/infra complexity slows development | Low | Medium | Keep compose simple; only 2 services in Phase 1 |
| Over-reliance on system output for trading decisions | Medium | High | System produces context, not signals; log what it told you vs what you did |
| OpenRouter model deprecation or pricing changes | Medium | Low | Config-driven model selection; switch models without code changes |
| Database grows large over time | Low | Low | TimescaleDB compression; retention policies in config for old data |

---

## 12. Decision Log

Decisions made during architecture planning and their rationale.

| Decision | Choice | Rationale | Alternatives Considered |
|---|---|---|---|
| Database | PostgreSQL + TimescaleDB | Best time-series support on relational DB; free; excellent community; your data is fundamentally time-series | KDB+ (expensive), InfluxDB (less flexible for relational queries), SQLite (no concurrent access), DuckDB (analytical not operational) |
| Orchestration | Simple Python scheduler in Docker | Minimal complexity; single operator; no need for DAG visualisation or distributed execution | Airflow (heavy), Prefect (good but extra dependency), cron (no error handling or dependency tracking) |
| LLM access | OpenRouter with pinned models | Flexibility to test models; single API key; cost tracking built in | Direct API to one provider (less flexibility), local models (Phase 1 needs reliable quality) |
| Frontend | HTMX + Jinja2 + vanilla CSS | Minimal JS footprint; fast; no build tooling; server-rendered is simpler for solo dev | React (bloated for this), Streamlit (can't style properly, gets slow), Grafana (always looks like Grafana) |
| Normalisation | Inline in collectors | At current scale, separate normalisation layer is unnecessary abstraction; extract later if multiple sources feed same table | Separate normalisation service (over-engineered for solo use) |
| Social sentiment | Excluded | Low signal-to-noise ratio for discretionary macro trading; adds complexity without clear value | Twitter/Reddit APIs (expensive, noisy) |
| Alt data | Excluded | Poor cost/utility at retail scale; affordable versions too delayed to provide edge | Various vendors (practitioner consensus is negative ROI) |
| MT5 integration | Deferred, interface prepared | Automation is future scope; clean output interface means MT5 bridge is a simple addition later | Build now (premature, would slow Phase 1) |
