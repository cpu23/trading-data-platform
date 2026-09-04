# Trading Data Platform

[![CI](https://github.com/cpu23/trading-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/cpu23/trading-data-platform/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![TimescaleDB](https://img.shields.io/badge/TimescaleDB-FDB515?logo=postgresql&logoColor=111827)](https://www.timescale.com/)
[![Docker Compose](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)

A local-first market intelligence platform that collects macroeconomic,
economic-calendar, price, news, and regulatory-filing data; runs
dependency-aware analytical processors; and presents traceable daily market
and company context through a FastAPI and HTMX dashboard.

This public-safe repository demonstrates the platform architecture, collectors,
processors, API, database schema, operational views, and dashboard. It excludes
credentials, generated logs, private databases, proprietary research, and
trading decisions.

## Why This Project Exists

Market context often lives across unrelated websites, spreadsheets, API
responses, and manually written notes. This platform turns those inputs into a
repeatable data workflow:

1. Collect and normalise source data and company reports.
2. Store raw observations and report evidence separately from derived analysis.
3. Run processors only when their dependencies have succeeded.
4. Record lineage, status, duration, model usage, and cost.
5. Present market, investment, and operational context in authenticated views.

The platform is decision support, not a signal or execution engine. Analytical
outputs provide context for human review and do not produce trade calls,
entries, or position-sizing instructions.

The deployed runtime is deliberately small: `postgres`, `web`, and `worker`.
The worker owns scheduling, the canonical durable queue, outbox publication,
quotes, and autonomous research; the web process calls orchestration modules
directly rather than through an internal HTTP service.

## Architecture

```mermaid
flowchart LR
    subgraph Sources["External Data Sources"]
        FRED["FRED macro data"]
        Official["OECD, ECB, BoE, EIA<br/>and CFTC official data"]
        CentralBanks["Fed, ECB, BoE and BoJ<br/>communications"]
        Calendar["Economic calendar"]
        OANDA["OANDA price snapshots"]
        News["Reuters and Kobeissi"]
        Filings["SEC, Companies House,<br/>EDINET, OpenDART"]
        LLM["OpenAI-compatible LLM"]
    end

    subgraph Orchestration["Collection and Processing"]
        Collectors["Normalising collectors"]
        Cycle["Dependency-aware cycle runner"]
        FilingIntake["Scheduled filing intake"]
        Investment["Investment fact extraction<br/>and deterministic engine"]
        Intelligence["Regime, event, and<br/>briefing processors"]
        ResearchEngine["Evidence adapters, dynamic cases,<br/>competing theses and falsification"]
        Quality["Data-quality checks<br/>30-second health snapshot"]
        Events["Source event routing<br/>and dependency propagation"]
        Planner["Atomic questions and<br/>deterministic VOI planner"]
        Skills["Durable work orders and<br/>versioned research skills"]
        Effects["Material effects, justified no-ops,<br/>forecast outcomes and feedback"]
    end

    subgraph Storage["PostgreSQL and TimescaleDB"]
        Raw["Raw time-series and events"]
        Reports["Report documents and analyses"]
        Derived["Derived market intelligence"]
        Cases["Versioned cases and theses,<br/>forecasts, playbooks and evidence"]
        Operations["Run history, logs, costs"]
        ControlPlane["Questions, plans, budgets,<br/>work orders, effects and scorecards"]
        EventDelivery["Dependency graph, outbox,<br/>and canonical jobs queue"]
    end

    subgraph Delivery["Delivery Layer"]
        API["FastAPI JSON API"]
        Dashboard["Dashboard `/` — lean surface:<br/>compact strip, since-last-view,<br/>lazy watchlist grid, briefing"]
        Markets["Markets `/markets` — lazy shell:<br/>cross-asset, catalysts, macro releases,<br/>regime, indicators, calendar"]
        News["News `/news` — change feed,<br/>story monitor, source controls"]
        Investments["Investment research view"]
        Research["Research case and thesis desks"]
        Evaluation["Point-in-time replay<br/>and quality evaluation"]
        Health["Health, live quality, and logs"]
        Heartbeat["Browser `marketRefresh` heartbeat:<br/>one polling timer"]
        OpsTopology["Operations `/operations` —<br/>truthful live system topology"]
    end

    FRED --> Collectors
    Calendar --> Collectors
    OANDA --> Collectors
    News --> Collectors
    Collectors --> Raw
    Collectors --> Cycle

    Cycle --> Intelligence
    LLM --> Intelligence --> Derived
    Raw --> Quality --> API

    Filings --> FilingIntake --> Reports
    Reports --> Investment
    News --> Investment
    LLM --> Investment --> Reports
    Raw --> ResearchEngine
    Reports --> ResearchEngine
    LLM --> ResearchEngine --> Cases
    Cycle --> Operations
    FilingIntake --> Operations

    Raw --> API
    Reports --> API
    Derived --> API
    Cases --> API
    Operations --> API
    Raw --> Events
    Reports --> Events
    Cases --> Events
    Events --> Planner --> Skills --> Effects
    Planner --> ControlPlane
    Skills --> ControlPlane
    Effects --> ControlPlane
    Effects --> Cases
    Effects --> Planner
    Events --> EventDelivery
    ControlPlane --> EventDelivery
    ControlPlane --> API
    EventDelivery --> API
    API --> Dashboard
    API --> Markets
    API --> News
    API --> Investments
    API --> Research
    API --> Evaluation
    API --> Health
    API --> OpsTopology
    Heartbeat --> Dashboard
    Heartbeat --> Markets
    Heartbeat --> News
    Heartbeat --> OpsTopology
```

Every triggered cycle receives a correlation ID that connects collector runs,
processor runs, operational logs, and cycle status. This makes failures and
derived outputs inspectable without mixing raw source data with analysis.

## Engineering Highlights

- **Configuration-driven collectors:** revision-aware FRED and OECD macro
  series, ECB and Bank of England data, EIA energy balances, CFTC and FINRA
  positioning, central-bank communications, economic-calendar events, OANDA
  prices, historical option surfaces, issuer filings/news/transcripts, and
  point-in-time company expectations, catalysts, institutional ownership, and
  short interest are defined in YAML rather than hard-coded.
- **Dependency-aware processing:** processors run only after their required
  collectors or upstream processors succeed.
- **Traceable LLM usage:** processing logs capture model, prompt version, token
  usage, cost, duration, status, and errors.
- **Resilient collection:** retries, upserts, structured logging, source payload
  caching, and stale-cache fallback reduce avoidable failures.
- **Time-series storage:** PostgreSQL with TimescaleDB stores macro observations
  and market snapshots as hypertables.
- **Operational visibility:** health, freshness, cycle status, logs, and daily
  LLM spend are available through both the API and dashboard.
- **Durable delivery:** append-only domain events, transactional outbox,
  one leased `jobs` queue, immutable section snapshots, one centralized
  `marketRefresh` polling heartbeat, and bounded HTMX refreshes decouple
  ingestion from presentation.
- **Separated delivery layer:** JSON endpoints and server-rendered HTMX views
  share the same stored data without coupling collection to presentation.
- **Bounded read latency:** the dashboard `/` renders a compact context with
  lazy surfaces, `/markets` is an empty shell until its partials load, macro
  indicators use one batched query, and `/quality` remains an explicit
  diagnostic.
- **Investment research:** deterministic filing deltas, versioned themes and
  theses, evidence history, company dossiers, optional read-only portfolio
  context, and automated SEC/Companies House intake. See
  [docs/investment-research.md](docs/investment-research.md).
- **Research intelligence:** normalized source adapters, atomic claims, dynamic
  case discovery, deterministic lifecycle, bounded causal graphs,
  multidimensional value-capture review, counterevidence, cold-data requests,
  evidence-linked major-market drivers, and point-in-time replay with
  version-controlled benchmark episodes and model/prompt variant comparison.
  A separate bounded thesis tournament generates competing, citation-audited
  candidates; deduplicates them by deterministic identity; continuously
  challenges active theses; publishes scenario, catalyst, playbook, and
  opportunity views; and records point-in-time forecast outcomes for
  calibration. See
  [docs/research-intelligence.md](docs/research-intelligence.md) and
  [docs/investment-research.md](docs/investment-research.md).
- **News feed** — CLI commands poll Reuters news sitemaps and TwitterAPI.io for
  Kobeissi posts, then publish a normalized, deduplicated feed for the read-only
  FastAPI news endpoints. See
  [docs/news-sources.md](docs/news-sources.md).
- **Durable operations** — accepted jobs, heartbeats, advisory locks, restart
  reconciliation, scheduler state, and manual retry remain inspectable across
  process restarts. See [docs/operations.md](docs/operations.md).
- **Measured acceptance** — offline HTTP and browser baselines are recorded in
  [docs/performance-baseline.md](docs/performance-baseline.md).
- **Offline model promotion:** committed core, adversarial, long-context, and
  regression fixtures compare pinned provider slugs with raw usage artifacts,
  blind review, weighted scores, and hard disqualifiers.

## Dashboard

The authenticated web UI is split into focused workspaces in a fixed
navigation order: **Dashboard** (`/`), **Markets** (`/markets`), **News**
(`/news`), **Investments** (`/investment`), **Research** (`/research`), and
**Settings** (`/settings`).

### Dashboard (`/`)

The dashboard is a lean trader-facing morning context view rather than a
signal engine. It renders only:

- Header with the data freshness control
- Compact top strip: Current session, Current regime, Next catalyst
- Since your last view change summary
- One lazy-loaded watchlist grid
- One asset evidence drawer
- One merged briefing (What changed / Current interpretation / What would
  invalidate this, with delta, atom counts, and evidence disclosures)

Market-detail surfaces — cross-asset context, upcoming catalysts,
macro-release cards, regime history, macro indicators, and the economic
calendar — live on the Markets workspace rather than the dashboard.

### Markets (`/markets`)

A lazy shell that loads six canonical partials on demand, in order:
cross-asset context, upcoming catalysts, macro release monitor, global macro
regime, key macro indicators, and the economic calendar. The page itself
renders no dataset; each section swaps in its partial, which then owns its
refresh contract.


### News (`/news`)

Owns the continuous material change feed (with load-earlier pagination), the
source controls, the canonical story monitor, and story lane/state filters.

### Refresh model

One browser heartbeat event (`marketRefresh`) is the only periodic timer in
the client. Polling partials refresh on that event. Lazy placeholders swap in
their partial once on `load`; the swapped-in partial then owns its refresh
contract, so no placeholder creates a second timer. The heartbeat pauses while
the tab is hidden and dispatches one immediate refresh when visibility returns.
There is no SSE route or streaming fallback.

### Since your last view

Research market-driver and material research-case changes appear in the same
deterministic Since your last view path as every other change type: one
loader, one marker endpoint, no model inference, and no second aggregator.

### Operations

Run, analyze, and explicit force-full cycle controls with durable status live
on Settings, not on the dashboard header. The dashboard and settings pages
each consume one consolidated system-health response instead of rerunning the
quality suite, and macro indicator summaries are fetched in one batched
database query.

The authenticated `/operations` page shows a bounded topology assembled from
persisted source, canonical queue, worker, control-plane, database, outbox, and
web state. Nodes are keyboard accessible and missing evidence is shown as
unknown or unavailable, never healthy.
See
[Autonomous Research Control Plane](docs/autonomous-research-control-plane.md)
for question, planning, skill, budget, recovery and feedback semantics.

### Read-path baseline

The local read-path measurements in
[docs/performance-baseline.md](docs/performance-baseline.md) include historical
dashboard baselines and the measured autonomous research control-plane surface.
On the 24 August 2026 deterministic demo, `/operations` measured 20.452 ms warm
p50 and 22.098 ms warm p95; `/api/system/topology` measured 6.083 ms warm p95;
and the topology partial measured 13.392 ms warm p95. The populated
100-question maximum page measured 5.103 ms warm p95. These are local
acceptance observations, not production SLAs. The document records payload
bounds, query plans, environment, artifacts and reproduction procedure.

![System logs](docs/assets/system-logs-full-page.png)

## API Surface

FastAPI exposes JSON endpoints for:

- Current and historical macro regimes
- Macro dashboard data and individual series
- Upcoming and recent economic events
- Latest and dated daily briefings
- Watchlist context and structured opinions
- System health, logs, and cycle status
- Manually triggered collectors, processors, and full cycles
- Read-only Reuters and Kobeissi feed and source-state views
- Investment documents, report URLs, analyses, dashboard aggregates, filing
  source status, and durable filing-collection triggers
- Bounded dynamic research cases, case history, current major-market drivers,
  operational/model-cost status, and durable run/retry controls
- Durable research-control-plane status, bounded question/work-order lists,
  coalesced authenticated planning, and the live system-topology contract

Reuters and Kobeissi collection can be invoked through the orchestrator CLI or
the authenticated durable API trigger. Reuters is scheduled every two hours;
Kobeissi remains on-demand by default because it consumes a paid API.

When the web service is running, interactive OpenAPI documentation is available
at `/docs`.

## Data Model

The database keeps responsibilities explicit:

| Layer | Tables | Purpose |
| --- | --- | --- |
| Raw | `macro_series`, `econ_events`, `market_data`, `source_payload_cache`, `investment_documents` | Normalised source observations, report evidence, and cached upstream payloads |
| Derived | `regime_classifications`, `structured_opinions`, `daily_briefings`, `investment_analyses`, `research_cases`, `research_causal_edges`, `research_market_drivers` | Versioned market, company, causal, and research-case analytical outputs |
| Operations | `collection_log`, `processing_log`, `cycle_runs`, `jobs`, `generation_attempts` | One durable work lifecycle, lineage, validation failures, duration, model usage, and cost |
| Research control plane | `research_questions`, `research_plans`, `research_plan_decisions`, `budget_reservations`, `research_skill_versions`, `research_work_orders`, `research_dependency_nodes`, `research_dependency_edges`, `research_source_capabilities`, `research_source_gaps`, `research_effects`, `research_outcome_attributions` | Atomic question identity, deterministic planning, exact skill/job lineage, incremental dirty propagation, material/no-op effects, productivity, forecast feedback, and source/skill scorecards |
| Human review | `investment_thesis_proposals` | Immutable autonomous research proposals and explicit approval, rejection, or revision state |

## Quick Start

For a populated, credential-free live demo that works from a brand-new named
volume with no setup intervention:

```bash
docker compose -f docker-compose.demo.yml up --build
# Open http://127.0.0.1:8000
```

The credential-free demo sets `DISABLE_AUTH=true` in explicit demo mode. It
seeds deterministic fictional analysis and operational history, then publishes
bounded fictional prices through the unified worker. The browser exercises the
same polling-only HTMX partials as production and makes no external or paid API
calls.

### Prerequisites

- Docker and Docker Compose v2
- An OpenRouter API key for the analytical processors
- A free [FRED API key](https://fred.stlouisfed.org/docs/api/api_key.html) when
  US macroeconomic coverage is selected
- An OANDA personal access token when the watchlist price collector is enabled
- A descriptive SEC user agent; Companies House, EDINET, and OpenDART keys are
  optional and enable only their corresponding filing sources

OpenRouter is globally required. Credentialed collectors fail closed when
enabled without their matching key, while disabled collectors may remain
blank. The demo Compose file remains credential-free by supplying non-secret
demo placeholders and disabling external collection.

### Start The Platform

```bash
cp .env.example .env
# Populate OpenRouter and any selected collector credentials, then independently
# generate SETUP_TOKEN and the session/CSRF signing keys.
docker compose up -d
```

Docker Compose starts exactly:

- PostgreSQL with TimescaleDB
- The public FastAPI JSON API and HTMX dashboard (`web`)
- One durable worker owning scheduling, jobs, outbox publication, quotes, and
  autonomous research

Application processes run as UID 10001 with no-new-privileges, bounded memory
and PID limits, immutable upstream image digests, and shared named volumes for
logs and published News. Only `web` publishes a host port.

The dashboard is exposed at `http://127.0.0.1:8000` by default.

Normal deployment runs code, configuration, prompts, and the authoritative
schema copied into immutable images. PostgreSQL is reachable only on the
Compose network. For source/configuration bind mounts and loopback database
access during development, opt in explicitly:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

### Authentication And Bootstrap

Normal deployment is authenticated. `DISABLE_AUTH` is rejected unless
`DEPLOYMENT_MODE` is explicitly `demo` or `test`; loopback binding is not an
authentication control. Before first activation, replace `SETUP_TOKEN` and the
three purpose-specific signing-key placeholders in `.env`, then open `/setup`.
The activation request must present the bootstrap token and commits a complete
versioned state atomically.

Demo and test deployments may set `DISABLE_AUTH=true`; production rejects it.
Production bootstrap always uses `/setup` and `SETUP_TOKEN`, then session
authentication through `/login`. HTTP Basic is unsupported.

Browser mutations require a session, a signed CSRF token, and an `Origin` that
matches `EXTERNAL_ORIGIN`. `TRUSTED_HOSTS` constrains accepted Host headers.
Remote production browser origins must use HTTPS with `COOKIE_SECURE=1`; plain
HTTP is accepted only for an explicit loopback origin.
`SESSION_SIGNING_KEY_PREVIOUS` provides a bounded session-rotation grace period;
new sessions are always signed with `SESSION_SIGNING_KEY`. CSRF and session keys
must remain distinct.

Configuration commits are versioned, validated snapshots. The API adopts a
valid commit atomically; the worker detects the committed version, stops
gracefully, and is restarted by Compose. During convergence, readiness reports
a version mismatch rather than claiming the old and new configuration are
equivalent. A rejected reload retains the last valid snapshot.

### First Authenticated Run

1. Open `/setup`, enter the configured `SETUP_TOKEN`, choose an administrator
   password of at least 12 characters, select coverage, and add the model slug
   and provider credentials required by the selected sources.
2. Activate the platform. If the committed configuration changes a
   restart-sensitive section, readiness may be non-2xx while the worker exits
   at a safe boundary and restarts against the new configuration version. Wait
   for `docker compose ps` to report the required services healthy.
3. Open `/login` and sign in with the administrator password. Browser sessions
   are authenticated and all state-changing requests use the signed CSRF token
   issued by the application.
4. Visit **Settings → Data & operations**. Review the active model, daily
   budget, role health, next schedule, and source freshness before selecting
   **Run due cycle**. The page follows the durable correlation ID through
   collection and processing rather than treating HTTP `202 Accepted` as
   completion.
5. Inspect **Dashboard**, **Markets**, **News**, **Investments**, and
   **Research** for outputs; use **Operations**, **Logs**, and
   **Quality checks** for a partial or failed run. A terminal `partial`
   result means accepted work completed durably but at least one collector or
   processor failed; it is not reported as success.

Use **Force full cycle** only for deliberate recovery or validation: it ignores
normal due-time and unchanged-input skips and may consume substantially more
provider budget. See [docs/operations.md](docs/operations.md) for exact cycle,
queue, retry, and health semantics.

### Run And Inspect Collectors

```bash
docker compose exec worker /app/.venv/bin/python cli.py collect --all
docker compose exec worker /app/.venv/bin/python cli.py status
docker compose exec worker /app/.venv/bin/python cli.py health
docker compose logs worker
```

Additional collector commands:

```bash
docker compose exec worker /app/.venv/bin/python cli.py collect fred
docker compose exec worker /app/.venv/bin/python cli.py collect oanda
docker compose exec worker /app/.venv/bin/python cli.py db-check
```

On-demand news collection also runs through the orchestrator CLI:

```bash
docker compose exec worker /app/.venv/bin/python cli.py news reuters
docker compose exec worker /app/.venv/bin/python cli.py news kobeissi
docker compose exec worker /app/.venv/bin/python cli.py news all
```

Kobeissi collection requires `TWITTERAPI_KEY`; Reuters and the read-only news
API do not. Leaving `TWITTERAPI_KEY` empty keeps optional Kobeissi collection
unconfigured until it is invoked with a credential.

### Run And Inspect Research Intelligence

Research discovery is a budgeted durable job. Read commands do not invoke the
model:

```bash
docker compose exec worker /app/.venv/bin/python cli.py research-run
docker compose exec worker /app/.venv/bin/python cli.py research-status
docker compose exec worker /app/.venv/bin/python cli.py research-inspect <case-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research benchmark list
docker compose exec worker /app/.venv/bin/python cli.py research inspect-replay <replay-run-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research metrics --scope comparison
```

Open `http://127.0.0.1:8000/research` for dynamic cases and
`http://127.0.0.1:8000/research/evaluation` for point-in-time replay,
scorecards, model/prompt variants, failures, cost, and immutable human-review
history. See [Research Intelligence](docs/research-intelligence.md) for replay
commands, API bounds, lifecycle semantics, and source-adapter extension rules.

## Local Verification

The repository uses one locked root `uv` environment. Test commands run
through the 4 GiB address-space guard; set `TEST_MEMORY_LIMIT_BYTES` to a
positive byte count when a smaller host requires a lower ceiling.

```bash
uv sync --frozen
uv run python -m compileall -q -x '/\.venv/' api orchestrator contracts scripts tests
uv run python scripts/run_bounded_tests.py discover -s api/tests
uv run python scripts/run_bounded_tests.py discover -s orchestrator/tests
uv run python scripts/run_bounded_tests.py discover -s tests
uv run ruff check api orchestrator contracts scripts tests
uv run mypy api/api_db.py api/config.py orchestrator/db.py orchestrator/config_loader.py contracts
docker compose config --quiet
docker compose -f docker-compose.demo.yml config --quiet
scripts/smoke_test.sh
```

The GitHub Actions workflow runs compilation, API/orchestrator/root tests,
PostgreSQL integration tests, deterministic fixtures, Compose validation,
Ruff, mypy, dependency audits, the credential-free demo smoke, and a Trivy
image gate requiring zero High or Critical findings on the application image.

## Project Structure

```text
.
├── api/                    # FastAPI JSON routes, HTMX views, templates, assets
├── config/                 # Collectors, processors, models, schedules, dashboard
├── db/
│   ├── schema.sql          # Authoritative fresh-database schema
│   └── Dockerfile          # Production and deterministic demo database images
├── docs/                   # Architecture, operations, research, performance
├── orchestrator/
│   ├── collectors/         # FRED, economic-calendar, and OANDA collectors
│   ├── processors/         # Regime, event-impact, and briefing processors
│   ├── investment_filings.py  # Regulatory discovery and ingestion
│   ├── investment_service.py  # Document extraction and analysis lifecycle
│   ├── investment_engine.py   # Deterministic scoring and valuation
│   ├── research_intelligence/ # Evidence adapters and bounded research engines
│   ├── tests/              # Focused service and processor unit tests
│   ├── cli.py              # Operator commands
│   └── orchestrator.py     # Dependency-aware cycle execution
├── prompts/                # Versioned public-safe analytical prompt templates
└── docker-compose.yml      # Exact postgres, web, and worker services
```

## Design Decisions

**Why separate raw and derived data?**

Source observations remain inspectable even when analytical logic changes.
Derived outputs can be regenerated and reviewed without losing provenance.

**Why a small custom orchestrator?**

The workload is local-first and operated by one user. A compact dependency
runner keeps execution understandable without introducing a distributed
workflow platform before it is needed.

**Why server-rendered HTMX views?**

The dashboard prioritises operational clarity and low maintenance over a large
frontend application. JSON routes remain available for other clients.

**Why keep humans responsible for decisions?**

LLM outputs are useful for synthesis and context, but they can be incomplete or
wrong. The system records evidence and operational metadata while leaving
interpretation and decisions with the user.

## Roadmap

- Measure production collector and processor latency under an approved upstream
  call budget.
- Add an operator-approved Kobeissi polling budget before enabling its schedule.
- Evaluate independent chart axes or normalization for mixed-scale macro series.
- Define host-level log retention and a tested backup/restore cadence.

## Public Boundary

This repository is intentionally public-safe. It does not include:

- Credentials, `.env` files, or API tokens
- Local databases, generated logs, or private briefings
- Proprietary trading research or strategy logic
- Trade calls, execution instructions, or trading decisions

The screenshots are curated examples of the presentation layer.

## Suggested Repository Topics

`trading`, `market-data`, `data-engineering`, `timescaledb`, `fastapi`,
`htmx`, `docker`, `llm-observability`
