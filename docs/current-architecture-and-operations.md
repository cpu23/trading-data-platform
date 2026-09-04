# Current Architecture and Operations

This document describes the current three-service deployment. Older phase
documents remain historical design records and are not operator instructions.

## Runtime Services

The production Compose project contains exactly:

- `postgres`: PostgreSQL with TimescaleDB. A fresh volume is initialized from
  the authoritative `db/schema.sql`.
- `web`: the session-authenticated FastAPI JSON API and server-rendered HTMX
  interface. API handlers call orchestration modules directly in-process.
- `worker`: one foreground runtime owning the scheduler, durable job executor,
  transactional outbox publisher, quote stream, and demo publisher when enabled.

`web` and `worker` use one root Python environment and application image.
Normal deployment executes image-copied code, configuration, prompts, and
schema. `docker-compose.dev.yml` is the explicit bind-mount override. Private
operator state is stored in the `operatorstate` volume. The web service exposes
build identity, `/live`, and dependency-aware `/ready`.

## State and Configuration Precedence

The platform merges:

1. Repository defaults in `config/config.yaml`.
2. Environment-backed credentials and deployment settings.
3. The activated private operator profile in `/app/state/operator.yaml`.
4. Private setup credentials in `/app/state/secrets.env`.

The operator profile changes coverage, watchlist, endpoint, model, reasoning,
and budget without rewriting repository defaults. Secret updates are
tri-state: a non-empty string sets, omission or an empty string leaves
unchanged, and JSON `null` deletes. Deleted values are absent from subsequent
immutable settings snapshots; loaders do not keep them alive in global
`os.environ`.

Setup commits versioned directories under `/app/state/versions`. Each version
contains `auth.json`, `operator.yaml`, `secrets.env`, and a checksummed
`manifest.json`, all private. One atomic `current` symlink selects the complete
snapshot; stable consumer links resolve through it. Readers validate the
manifest, checksums, and contents.

## Collection Contracts

Each collector is an isolated adapter that returns normalized records and
acquisition metadata. A source can fail without preventing unrelated sources
from updating.

Normalized macro records include:

- namespaced series identifier
- observation timestamp
- acquisition timestamp
- release/revision metadata when available
- source, frequency, region, units, and semantic feature

The semantic feature registry maps observations into growth, inflation,
policy, stress, energy, and positioning concepts. Asset context maps only the
relevant concepts and positioning contracts to each configured symbol.

### Forex Factory caching

The weekly export is immutable for a target week:

- First run for a new week: fetch live and store the payload.
- Subsequent runs: reuse the cached weekly payload.
- Upstream failure with an existing payload: use stale cache and report that
  acquisition mode explicitly.

The health API and dashboard expose `payload_source`, target week, fetched time,
and cache age.

### OANDA

OANDA is a continuous price stream, not a cycle collector. Its master
`enabled` setting and `stream_enabled` setting must both allow startup.
Collection cycles never fetch OANDA snapshots.

## Cycle and Publication Model

A full cycle:

1. Creates one `cycle_runs` record and correlation ID.
2. Runs enabled collectors under the runtime lock.
3. Records every collector result, including expected no-data/setup states.
4. Runs processors only when dependencies succeeded.
5. Stages validated outputs.
6. Publishes the complete snapshot atomically when all required stages succeed.

The previous published snapshot remains queryable and visible while a new cycle
is active. Failed or interrupted cycles do not partially replace dashboard
state.

Published intelligence includes:

- macro regime
- daily briefing
- one asset assessment per configured symbol
- rolling global narrative memory
- deterministic since-last-cycle delta

## AI Client and Accounting

The client targets one OpenAI-compatible endpoint. It supports:

- one active endpoint profile
- one default model inherited by every production processor
- bounded explicit benchmark/research overrides
- reasoning-effort and sampling-parameter capability fallback
- provider routing preferences
- configurable timeout and retry attempts

Every generation attempt records:

- stage and attempt number
- requested/returned model and provider
- prompt and raw response
- validation issues
- input/output tokens
- cost
- latency
- fallback parameters
- provider request identifier

Raw prompts and responses have a 90-day retention contract.

## Research Intelligence

The research-intelligence subsystem normalizes existing macro, release-card,
reaction, story, filing, investment-analysis, and observation records through
source adapters; no second generic raw-data store is introduced.

The configured hot spine covers US, euro-area, UK, and Japan growth,
inflation, labour, policy/rate, financial-condition, and market evidence where
an official series is available. FRED, OECD, ECB, Bank of England, EIA, and
CFTC collectors retain deterministic values in their source-owned tables.
Fed, ECB, Bank of England, and Bank of Japan feeds add source communications;
they enter research only through the existing normalized evidence adapters.
Absent regional dimensions remain unknown rather than being inferred.

Deterministic gates block bounded, source-diverse candidate groups before model
work. Seven strict stages extract atomic source claims where useful, discover a
coherent pattern, build an allowlisted and depth-bounded causal graph, assess
independent value-capture dimensions, conduct an adversarial review, emit cold
research-data requests, and create a concise evidence-linked deliverable.
Major-market drivers run through the same evidence boundary but retain a
separate target/direction/horizon contract.

Cases have immutable version snapshots and deterministic lifecycle transitions:
`candidate`, `forming`, `corroborated`, `research_ready`, `mature`,
`weakening`, and `archived`. Epistemic edge state (`observed`, `supported`,
`hypothesis`, or `rejected`) never controls or aliases that lifecycle. A
research-ready case can extend an existing manual theme or promote to one new
maintained theme; exact and semantic matching prevent duplicates.

The scheduler enqueues deterministic research discovery and a nightly 02:00 UTC
autonomous thesis cycle on the canonical `jobs` queue. Two independent
researcher roles generate competing candidates; deterministic evidence,
opposition, citation, budget, and scoring gates run before every surviving
candidate is staged in `investment_thesis_proposals`. Agent output never
mutates a canonical thesis. Approval, rejection, or revision always requires a
human review action. Model attempts, prompt versions, fingerprints, usage,
cost, and validation failures remain inspectable. Dashboard reads never invoke
the model.

Point-in-time replay reuses the same adapters and validators under a strict
availability cutoff and never mutates live research state. Four
version-controlled benchmark episodes exercise full claim extraction,
discovery, causal, value-capture, adversarial, deliverable, lifecycle, and
abstention paths. Benchmark answers remain evaluator-only. Replay rows persist
separate deterministic-input and resolved model/prompt variant identities;
longitudinal history cannot cross variants, and regression comparisons require
identical deterministic inputs. `/research/evaluation`, the bounded JSON API,
and the `research` CLI group expose deterministic score dimensions
(including testable-hypothesis discovery), stage failures, tokens, latency,
cost, immutable human-review history, and comparisons.

See [Research Intelligence](research-intelligence.md) for the adapter contract,
schema, relationship grammar, lifecycle, value-capture dimensions, prompt
stages, APIs, operational controls, and source-extension guide.

## Investment Research and Filing Intake

The Investments view is backed by a separate report lifecycle:

1. The weekday `08:00 UTC` scheduler or an authenticated operator creates a
   durable `filings` run.
2. Bounded workers discover source-native filing IDs from SEC, Companies House,
   EDINET, or OpenDART for configured permanent company identifiers.
3. New report content is extracted, hashed, deduplicated, and stored in
   `investment_documents`.
4. Operator-requested analysis claims the document, extracts strict
   evidence-linked facts through the provider-neutral AI client, and applies
   deterministic signal, comparison, valuation, and state rules.
5. The result, model, tokens, cost, and comparable-document link are stored in
   `investment_analyses` and `processing_log`.

The built-in universe contains top-100 US, UK, and EU snapshots. SEC requires a
descriptive user agent but no key. Companies House, EDINET, and OpenDART are
enabled only when their keys and permanent issuer identifiers are present. EU
ESEF coverage outside cross-listed SEC filers remains manual because national
OAMs are decentralized.

Automatic analysis after filing ingestion is disabled by default. This keeps
scheduled regulatory collection free of accidental model spend. Manual file and
public-URL intake remains available through `/investment`.

See [Investment Research and Filing Intake](investment-research.md) for source
coverage, HTTP contracts, storage, failure semantics, and measurements.

## Dashboard Design

The interface uses progressive disclosure:

- compact source and intelligence status strips
- a slim since-last-cycle section
- compact watchlist cards
- detailed focus/evidence panels only on demand
- collapsed long briefing details
- sparse asset history pages
- no permanent sidebar or competing dashboard grid

Primary navigation exposes Dashboard, News, Investments, and Settings, with
asset, Logs, Operations, and Data quality deep links. Desktop and mobile
layouts are supported.

## Read Path and Health Snapshot

Dashboard rendering is a concurrent fan-in over stored regime, briefing,
calendar, macro, price, cycle, budget, news, and health data. Each independent
loader retains a section-level fallback, so one unavailable dataset does not
serialize or suppress unrelated sections. The completed health response is
fetched once per page and reused for both the detailed component state and the
compact data-status chip.

The Investments page separately loads one aggregated report/analysis dashboard
and one filing-source status payload; it does not join the market-dashboard
fan-in or run model analysis during an ordinary read.

`GET /api/macro/dashboard` uses one batched PostgreSQL statement for every
configured dashboard series. Lateral index probes select the latest and
previous observations, while a bounded five-day aggregate derives the trend.
This replaces per-indicator query fanout without changing the response
contract.

`GET /api/system/health` checks PostgreSQL, the durable `worker` heartbeat,
collector/processor freshness, and bounded data-quality state. Readiness
requires the database and current worker heartbeat; missing quote or outbox
subcomponent detail does not invent extra service dependencies. The
operator-facing `/quality` page is the explicit detailed diagnostic path.

## Authentication and Request Security

Before activation, only setup/login-safe routes are available. Activation
creates the administrator record and session, publishes configuration
atomically, and locks setup routes.

After activation:

- HTML requests without a session redirect to `/login`.
- JSON requests without a session return `401` without a Basic Auth challenge.
- State-changing requests require a matching CSRF token.
- Cross-origin mutations are rejected.
- Trusted hosts are configured through `TRUSTED_HOSTS`.
- Cookies are HTTP-only, SameSite strict, and optionally secure.
- Session expiry is controlled by `SESSION_MAX_AGE_SECONDS`.

## Budgets

`budgets.daily_llm_usd` is a hard daily cap. A denied paid processor does not
run. Explicit one-run overrides require a reason and are audited. Data
collectors can still run when the paid inference budget is exhausted.

## Durable Event and Refresh Path

Collectors normalize domain changes into the append-only `market_events`
ledger. The transactionally coupled `event_outbox` is leased by the worker,
which schedules idempotent work on the canonical `jobs` queue. The same worker
claims every job kind and publishes immutable `section_snapshots`.

Browsers use one visibility-aware 90-second `marketRefresh` timer. HTMX
partials refresh from stored data on that event; there is no SSE endpoint,
invalidation table, or streaming fallback.

The full cycle remains authoritative reconciliation. It repairs abandoned
leases, refreshes freshness classifications, backfills reaction windows and
market confirmations, expires claims, and republishes missing snapshots. A
failed publication leaves the previous valid snapshot visible with
stale/failure metadata.

## Model Evaluation and Promotion

All production processors inherit one exact slug from `llm.models.default`.
Changing the default is a manual operator decision gated by ADR 0011: review
the candidate slug's spend, schema-repair, and budget metrics in
`/operations` before switching, and never weaken a schema or enable provider
fallback to make a candidate pass.

## Operator Checks

```bash
# Service state
docker compose ps
curl http://127.0.0.1:8000/api/meta/build

# Full cycle or individual component
docker compose exec worker /app/.venv/bin/python cli.py collect --all
docker compose exec worker /app/.venv/bin/python cli.py collect ecb
docker compose exec worker /app/.venv/bin/python cli.py process briefing

# Investment and research-intelligence read paths
curl http://127.0.0.1:8000/api/investment/dashboard
curl http://127.0.0.1:8000/api/investment/filings/status
curl http://127.0.0.1:8000/api/research/themes
curl http://127.0.0.1:8000/api/research/cases
curl http://127.0.0.1:8000/api/research/drivers?changed_only=true
curl http://127.0.0.1:8000/api/research/status
curl http://127.0.0.1:8000/api/research/benchmarks
curl http://127.0.0.1:8000/api/research/replays?limit=20
curl http://127.0.0.1:8000/api/research/metrics?limit=20

# Research durable controls
docker compose exec worker /app/.venv/bin/python cli.py research-run
docker compose exec worker /app/.venv/bin/python cli.py research-status
docker compose exec worker /app/.venv/bin/python cli.py research-inspect <case-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research-update <case-uuid> --force
docker compose exec worker /app/.venv/bin/python cli.py research-rebuild
docker compose exec worker /app/.venv/bin/python cli.py research-retry <job-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research benchmark list
docker compose exec worker /app/.venv/bin/python cli.py research inspect-replay <replay-run-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research metrics --scope comparison

# Queue and worker state
curl http://127.0.0.1:8000/api/jobs/status
docker compose exec worker /app/.venv/bin/python -m roles check worker

# Logs
docker compose logs -f web worker
```

The dashboard quality page is authoritative for operator-facing freshness,
gap, duplicate, and anomaly state. The logs page hides benchmark experiments by
default while preserving them for explicit audit queries.

## Backup and Upgrade

Back up both persistent volumes before a material upgrade:

- database volume (`pgdata`)
- private state volume (`operatorstate`)

`db/schema.sql` is the sole schema definition and initializes only a fresh
PostgreSQL volume. This clean-cutover runtime does not include an application
migration service or compatibility loader. Schema-changing upgrades therefore
require a fresh volume or an explicitly reviewed database change performed
outside the application. Never replace the private state volume with loose
root-level state files; only complete versioned snapshots are supported.

