# Current Architecture and Operations

This document describes the deployed platform as of 8 August 2026. Older phase
documents remain historical design records and are not current operator
instructions.

## Runtime Services

The production Compose project contains:

- `postgres`: PostgreSQL with TimescaleDB.
- `migrate`: checksum-verified, one-shot schema gate.
- `orchestrator`: internal HTTP control API only.
- `scheduler`: durable logical-run scheduling and enqueue.
- `worker`: leased operation and analysis execution.
- `outbox`: transactional event publication.
- `quotes`: quote-stream ownership.
- `api`: session-authenticated JSON/SSE API and server-rendered HTMX interface.

The credential-free demo adds `demo-live`, a bounded deterministic publisher
that inserts fictional price observations and watchlist invalidations. It does
not call external providers or models.

Normal deployment executes image-copied code, configuration, prompts,
migrations, and database bootstrap SQL. `docker-compose.dev.yml` is the
explicit bind-mount override. Private operator state is stored in the
`operatorstate` volume. The API exposes build identity, `/live`, and
dependency-aware `/ready`.

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
`manifest.json`, all private. `activated.json` is the atomically replaced
current-version pointer. Readers validate the marker, manifest, checksums, and
parseability rather than treating partial legacy files as completed setup.

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

When the normalized input fingerprint is unchanged, market intelligence emits
a no-change result without paid inference.

## AI Client and Accounting

The client targets any OpenAI-compatible endpoint. It supports:

- one active endpoint profile
- global and per-processor model overrides
- per-role market-intelligence profiles
- reasoning-effort capability fallback
- sampling-parameter capability fallback
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

## Intelligence Pipeline

Market intelligence uses four calls:

1. Analyst: strongest evidence-bounded economic interpretation.
2. Skeptic: challenges causality, confidence, and missing evidence.
3. Auditor: checks support, freshness, contradictions, and policy.
4. Editor: synthesizes only validated role claims.

Contracts enforced in code include:

- every configured symbol appears exactly once
- role and editor schemas reject extra keys
- evidence IDs must be supplied and asset-eligible
- CFTC contracts apply only to configured mapped assets
- positioning claims describe one participant category at a time
- global narratives cite only global claims
- asset narratives cite only claims for that asset
- invalid source references drop the optional narrative
- editor evidence is derived from cited validated claims
- sparse assets become neutral, low confidence, and explicitly unavailable
- advisory and technical-analysis language fails policy validation

The currently selected production profile is documented in
`docs/intelligence_model_benchmark.md`.

## Research Intelligence

The research-intelligence subsystem is separate from the four-role daily market
briefing above. It normalizes existing macro, release-card, market-state,
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

The configured weekday schedule enqueues a deduplicated `research_discovery`
analysis job. Model attempts, prompt versions, input fingerprints, usage, cost,
validation failures, and one bounded repair attempt remain inspectable. Adapter,
macro-stage, and per-candidate failures are isolated. Dashboard reads never
invoke the model.

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
- collapsed long briefing and role details
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

`GET /api/system/health` performs local API checks and one internal
orchestrator `/health` request. The orchestrator response includes its quality
result. The expensive quality suite is protected by a process-local,
configuration-aware snapshot with a 30-second default TTL; set
`HEALTH_QUALITY_CACHE_SECONDS` to change that bound. An expired snapshot is
refreshed under a lock so concurrent probes do not duplicate the sweep.

The operator-facing `/quality` page deliberately calls the uncached
orchestrator `/quality` endpoint. It is the explicit live diagnostic path;
ordinary dashboard, settings, readiness, and Compose health probes consume the
bounded snapshot.

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

## Durable Live Path

Collectors normalize domain changes into the append-only `market_events`
ledger. The transactionally coupled `event_outbox` is leased by the outbox
worker, which schedules idempotent `analysis_jobs`. Workers publish immutable
`section_snapshots`; a changed current snapshot appends a small `ui_events`
invalidation. `/stream` replays retained invalidations and then streams new
wakeups. Browsers fetch the named HTMX partial; SSE never transports report,
price, evidence, or source payloads.

The full cycle remains authoritative reconciliation. It repairs abandoned
leases, refreshes freshness classifications, backfills reaction windows and
market confirmations, expires claims, republishes missing snapshots, and
removes expired UI events. A failed publication leaves the previous valid
snapshot visible with stale/failure metadata.

## Model Evaluation and Promotion

All production processors inherit one exact slug from `llm.models.default`.
The offline `benchmark-models` command compares the two pinned candidate slugs
against versioned core, adversarial, long-context, and regression fixtures.
Artifacts include exact request bodies, raw responses, deterministic metrics,
actual usage, a randomized `blind-review.html`, a separate identity key,
weighted scores, and hard disqualifiers. The initial decision has no
recommendation. A reviewer scores all eight criteria and supplies rationale for
every candidate/case, downloads `blind-review-scores.json`, then finalizes it:

```bash
python cli.py benchmark-score \
  --artifact ../artifacts/model-benchmarks/<run> \
  --review /path/to/blind-review-scores.json
```

Only complete, validated blind review unlocks a recommendation. ADR 0011 still
requires operator approval of that artifact before `llm.models.default`
changes.

## Operator Checks

```bash
# Service state
docker compose ps
curl http://127.0.0.1:8000/api/meta/build

# Full cycle
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py collect --all

# Individual source or processor
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py collect ecb
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py process market_intelligence

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
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research-run
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research-status
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research-inspect <case-uuid>
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research-update <case-uuid> --force
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research-rebuild
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research-retry <job-uuid>
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research benchmark list
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research inspect-replay <replay-run-uuid>
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py research metrics --scope comparison

# Collector, queue, and connectivity state
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py status
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py health

# Logs
docker compose logs -f api orchestrator
```

The dashboard quality page is authoritative for operator-facing freshness,
gap, duplicate, and anomaly state. The logs page hides benchmark experiments by
default while preserving them for explicit audit queries.

## Backup and Upgrade

Back up both named volumes before a material upgrade:

- database volume (`pgdata`)
- private state volume (`platform_state`)

Apply migrations before starting analytical workloads. Existing installations
are expected to retain historical opinions and previous snapshots across
migrations. Never replace the private state volume with repository `state/`
files; that directory is only a legacy migration input.

