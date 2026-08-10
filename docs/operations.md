# Operations guide

This guide describes the repaired split-service runtime. For topology rationale, see [ADR 001](adr/001-runtime-topology.md). For deterministic incident exercises, see [failure-drills.md](failure-drills.md).

## Runtime ownership and startup

The image is shared, but each Compose service owns one lifecycle:

1. `postgres` starts and becomes healthy.
2. `migrate` runs the checksum-verified migration chain once and exits successfully.
3. `orchestrator` starts only after migration success. It is internal-only and owns scheduling, durable jobs, collectors, processors, news collection, and the quote stream.
4. `api` starts after migrations and orchestrator health. It is the only application service with a host port.

The shared application image runs as UID/GID 10001 with `no-new-privileges`, bounded memory/PIDs, and immutable Python/uv base digests. Compose named volumes `newsdata` and `logsdata` provide the writable shared paths; do not replace them with root-owned bind mounts unless UID 10001 has explicit access.

Start production only after replacing every required `.env` placeholder:

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d
```

Use the credential-free deterministic stack for local acceptance:

```bash
docker compose -f docker-compose.demo.yml up --build
# http://127.0.0.1:8000 — demo / demo
```

Demo mode disables external collectors, provider quote streams, and paid
inference. `demo-live` instead publishes four bounded fictional prices and a
real watchlist invalidation every five seconds. The browser receives the event
through `/stream` and refreshes the normal HTMX partial; no demo-only UI path
exists.

## Migration procedure

The `migrate` service is a one-shot gate. A checksum mismatch or failed migration exits nonzero and prevents the API and orchestrator from starting.

```bash
docker compose run --rm migrate
scripts/test_clean_migrations.sh
```

The clean-migration test creates a disposable database, applies the complete chain, runs it again to prove idempotency, and always tears down its volume.

Never edit an applied migration. Add a new numbered migration. Before any destructive migration, take and verify a backup.

```bash
mkdir -p backups
backup="backups/trading-data-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup"
pg_restore --list "$backup" >/dev/null
```

Restore into a fresh or explicitly approved target; do not overwrite the only production database during a restore test.

## Cycle modes

The header exposes one primary Refresh action and a compact mode menu.

| Mode | Collection | Processing | Intended use |
|---|---|---|---|
| `refresh` | Runs enabled collectors only when due; uses historical success when not due | Runs processors only when dependencies changed; fingerprints can skip unchanged analysis | Normal operation |
| `analyze` | Performs no collection | Re-analyzes existing stored data, subject to dependency availability and budget | New analysis without upstream calls |
| `force_full` | Runs every enabled collector regardless of schedule | Forces processors and bypasses unchanged fingerprints | Explicit manual recovery or validation |

`force_full` is a trusted manual override, not a scheduler default. Concurrent identical cycles are rejected with a conflict rather than duplicated.

## Scheduler semantics

Schedules are UTC cron expressions from `config/config.yaml`.

- FRED: daily at `06:00`.
- OANDA snapshots: `06:00`, `12:00`, and `18:00`, Monday–Friday.
- Forex Factory: Sunday at `20:00`.
- Reuters: minute `15` every two hours.
- Kobeissi: configured for every six hours but `schedule_enabled: false` and `on_demand_only: true` by default.
- Briefing: after its macro-regime dependency, with the configured daily schedule retained for orchestration.
- Investment filings: weekdays at `08:00`, plus an optional startup run;
  bounded to one scheduled instance with four company workers by default.

The Operations page and authenticated health API expose scheduler state and next due times. In demo mode the scheduler starts but external jobs are disabled.

## Durable jobs, abandonment, and retry

A mutation is accepted into `cycle_runs` before background work begins. PostgreSQL advisory locks and durable idempotency ownership prevent duplicate execution. Running jobs heartbeat every 30 seconds.

At orchestrator startup:

- accepted jobs older than 15 minutes are marked `abandoned`;
- running jobs whose heartbeat is older than 5 minutes are marked `abandoned`;
- abandoned work is **not** retried automatically.

Only abandoned runs can be retried through the authenticated retry route. Retry creates a new accepted run linked to the prior request; it does not rewrite history.

## Hard budgets and deadlines

Current production defaults:

- daily LLM budget: `$2.00`;
- warning threshold: `80%`;
- LLM stage deadline: `90 seconds`;
- validation retries: one bounded correction attempt;
- output caps: 1,800 tokens for macro regime and 2,600 for briefing;
- collector workers: 3 by default, hard-capped at 8;
- FRED concurrency: 4;
- filing company workers: 4;
- filing lookback: 730 days;
- automatic model analysis after filing ingestion: disabled.

Budget accounting includes failed attempts. If the budget state cannot be read, processing fails closed. Raw model responses and secrets are not persisted in failure telemetry.

## Health contracts

Health separates process survival from dependency and data quality:

- `liveness: ok` means the process is running.
- `readiness: ready` means required service dependencies are usable.
- `readiness: unready` returns HTTP 503 when the database or required orchestrator contract is unavailable.
- `data_health: degraded` reports stale or unhealthy data without pretending the process is dead.

The dashboard may therefore truthfully show **Degraded** while API readiness
remains ready. `/api/system/health` is authenticated and makes one internal
orchestrator `/health` request. That response carries a configuration-aware
quality snapshot cached for 30 seconds by default. The cache TTL is bounded by
`HEALTH_QUALITY_CACHE_SECONDS`; changing activated configuration replaces the
configuration object and forces a new snapshot. Refreshes run under a lock so
concurrent dashboard, settings, API, and Compose probes do not duplicate the
quality sweep.

The orchestrator `/quality` endpoint is intentionally uncached and is used by
the operator Quality page for an explicit live diagnostic. The orchestrator
health port remains internal to Compose. A stale snapshot can therefore affect
summary health for at most the configured TTL, while the live Quality page is
available when immediate confirmation is required.

## Authentication, CSRF, and SSE

All API routes require Basic authentication except the exact SSE stream route. The authenticated token endpoint issues a short-lived, signed, path-bound, HttpOnly, SameSite-Strict cookie; the stream validates that cookie before starting its generator. Credentials and tokens never appear in the EventSource URL.

Browser mutations require a signed, expiring CSRF token and same-origin validation. JSON-only machine requests are exempt only when they carry no browser signals. API-to-orchestrator mutations forward the configured internal Basic credentials.

## News operations and cost caution

Reuters is scheduled every two hours and requires no credential. Kobeissi uses TwitterAPI.io, remains on-demand by default, and should not be scheduled until its call budget and polling frequency are explicitly approved.

```bash
docker compose exec orchestrator .venv/bin/python cli.py news reuters
docker compose exec orchestrator .venv/bin/python cli.py news kobeissi
docker compose exec orchestrator .venv/bin/python cli.py news all
```

Publication is atomic: source snapshots and the unified feed are written before cursor advancement. A publication failure leaves the previous feed valid and the cursor unchanged. The dashboard shows a bounded summary; `/news` provides source and symbol/tag filters without a search database.

## Investment filing operations and cost caution

The built-in filing universe scans 300 US, UK, and EU companies. SEC collection
requires a descriptive contact user agent. Companies House, EDINET, and
OpenDART stay disabled unless their source keys and permanent issuer identifiers
are configured. The source status endpoint makes those omissions explicit:

```bash
curl http://127.0.0.1:8000/api/investment/filings/status
curl http://127.0.0.1:8000/api/investment/dashboard
```

Use **Investments → Collect filings now** for an authenticated manual run. The
API creates a durable `filings` job with a correlation ID; monitor it through
cycle status, Operations, and structured orchestrator logs. Successful company
results survive failures from unrelated companies or filing sources.

Do not enable `auto_analyze` merely to test collection. Filing discovery and
ingestion are free apart from regulator traffic; analysis calls the configured
model and consumes the daily LLM budget. Ingested documents remain queued when
analysis is disabled, budget-blocked, or fails.

Regulator secrets belong in environment or private operator state:
`COMPANIES_HOUSE_API_KEY`, `EDINET_API_KEY`, and `OPENDART_API_KEY`. Never place
them in `config/config.yaml`. See
[Investment Research and Filing Intake](investment-research.md).

## Research-intelligence operations and cost caution

The configured weekday schedule enqueues one deduplicated
`research_discovery` durable analysis job. It reads bounded stored evidence;
it does not collect every possible cold dataset. Run and inspect it directly:

```bash
docker compose exec orchestrator .venv/bin/python cli.py research-run
docker compose exec orchestrator .venv/bin/python cli.py research-status
docker compose exec orchestrator .venv/bin/python cli.py research-inspect <case-uuid>
docker compose exec orchestrator .venv/bin/python cli.py research-update <case-uuid> --force
docker compose exec orchestrator .venv/bin/python cli.py research-rebuild
docker compose exec orchestrator .venv/bin/python cli.py research-retry <job-uuid>
```

Point-in-time evaluation is read-isolated from live research state:

```bash
docker compose exec orchestrator .venv/bin/python cli.py research replay --as-of 2026-06-30T23:59:00+00:00
docker compose exec orchestrator .venv/bin/python cli.py research benchmark list
docker compose exec orchestrator .venv/bin/python cli.py research benchmark run <episode-id> --comparison-group baseline
docker compose exec orchestrator .venv/bin/python cli.py research benchmark compare <left-run-uuid> <right-run-uuid>
docker compose exec orchestrator .venv/bin/python cli.py research inspect-replay <run-uuid>
docker compose exec orchestrator .venv/bin/python cli.py research metrics --scope comparison
docker compose exec orchestrator .venv/bin/python cli.py research cohorts --no-persist
```

Use `/research` for live cases and `/research/evaluation` for replay variants,
scorecards, stage failures, resource use, lifecycle cohorts, and human-review
state. Human annotations are separate from deterministic scores. API writers
should send `expected_version`; a stale review returns HTTP 409 and does not
alter the current projection or immutable annotation history.

`research-run`, `research-update`, and `research-rebuild` enqueue work; they do
not run model calls in the request process. Normal refreshes coalesce by input
identity. Forced rebuilds and retries get a new durable-job identity so a prior
completed job cannot suppress requested work. The worker owns leases,
heartbeats, attempts, and transactions. A failed adapter or candidate is
recorded and isolated. `research-status` reports bounded case/job/cost
aggregates; case detail/history expose persisted model, prompt, and input
provenance, while `generation_attempts` retains validation issues, latency,
tokens, and cost. The model gets one repair attempt after semantic validation
failure, then that stage fails closed.

The subsystem has `research_intelligence.model_budget_usd_per_run` in addition
to `budgets.daily_llm_usd`. An ordinary `force` request bypasses reusable
fingerprint output where explicitly supported; it does not bypass either
budget. Research discovery is independent of portfolio holdings.

Open `research_data_requests` identify evidence the platform does not yet have.
Satisfy one by adding or extending a source-owned collector plus a normalized
evidence adapter; do not paste fabricated evidence into case tables. See
[Research Intelligence](research-intelligence.md).

## Logging, redaction, and retention

Production emits structured JSON to stdout. Secret-shaped fields and query keys such as `token` are redacted. SSE credentials are cookie-based and therefore absent from request URLs and access logs.

The application does not silently delete historical logs. Retention and rotation belong to the Docker logging driver or host policy. Before changing retention, export any incident window that must be preserved. Never paste raw logs into issues without checking redaction.

## Verification and incident drills

```bash
python3 -m compileall -q -x '/\.venv/' api orchestrator scripts
cd api && uv run python -m unittest discover -s tests
cd ../orchestrator && uv run python -m unittest discover -s tests
cd ..
api/.venv/bin/python -m unittest discover -s tests
orchestrator/.venv/bin/ruff check api orchestrator scripts tests
(cd api && uv run --with pip-audit==2.9.0 pip-audit --local --progress-spinner off)
(cd orchestrator && uv run --with pip-audit==2.9.0 pip-audit --local --progress-spinner off)
api/.venv/bin/python scripts/failure_drills.py --unit-only
docker compose config --quiet
docker compose -f docker-compose.demo.yml config --quiet
scripts/test_clean_migrations.sh
scripts/smoke_test.sh
```

For a warm read-path check that avoids external or paid calls:

```bash
curl -sS -o /dev/null -w 'dashboard %{http_code} %{time_total}s\n' \
  http://127.0.0.1:8000/
curl -sS -o /dev/null -w 'settings %{http_code} %{time_total}s\n' \
  http://127.0.0.1:8000/settings
curl -sS -o /dev/null -w 'health %{http_code} %{time_total}s\n' \
  http://127.0.0.1:8000/api/system/health
```

The first health request after cache expiry can include one live quality sweep.
Record warm and refresh-path measurements separately.

`failure_drills.py` is unit-only by default. `--docker` opts into the full smoke test. Neither default path calls paid or external data sources.

## Queue and outbox troubleshooting

Use `/operations` first. It exposes outbox backlog, analysis jobs by state,
oldest queued work, retryable failures, and abandoned leases without requiring
log access. A growing outbox with no claimed rows indicates an outbox worker or
database-connectivity problem. Old `running` jobs indicate a dead worker;
reconciliation returns expired leases to a retryable state and preserves the
last valid section snapshot.

Confirm service and database health:

```bash
docker compose ps
docker compose exec orchestrator /app/orchestrator/.venv/bin/python cli.py health
curl -fsS http://127.0.0.1:8000/api/system/health
```

Do not delete queued rows to recover a backlog. Diagnose the worker, run the
normal reconciliation cycle, and retain the event/job lineage for audit.

## SSE and partial-refresh troubleshooting

`/stream` carries only invalidation identity: event ID, event name, section
key, scope, and version. Reconnect with the last received event ID to exercise
the retained replay window. A cursor older than retention causes a safe
section refresh rather than payload replay. If SSE is unavailable, enabled
sections retain bounded HTMX polling; initial HTML remains functional.

```bash
curl -N --user \"$DASHBOARD_USER:$DASHBOARD_PASSWORD\" \
  'http://127.0.0.1:8000/stream?last_event_id=0'
```

Check `ui_events` growth only after confirming a source observation or section
snapshot actually changed. Unchanged content intentionally creates no visible
version or wakeup.

## Model compatibility and spend troubleshooting

`/settings` owns the single default model and request profile. `/operations`
and the cost views expose spend by processor/model/provider, schema repairs,
cache use, duplicate/materiality avoidance, and budget suppression. A
`suppressed_budget` job is not a collector failure; deterministic collection
continues.

Before changing the default, run `benchmark-models` against all committed
suites, complete the generated `blind-review.html`, and run `benchmark-score`
with its downloaded review JSON. A recommendation is invalid while
`blind_review_complete` is false. Never weaken a schema, enable provider
fallback, or use a per-processor override to make a candidate pass. Follow ADR
0011 and retain the artifact with actual cost, latency, identity key, scores,
and rationale.

If any candidate rejects sampling controls, pass `--omit-temperature`. The
flag removes `temperature` from every candidate request in that benchmark; it
does not create a model-specific request profile.

## Stale-data troubleshooting

`/quality` and source-health partials are authoritative. `source_freshness_state`
distinguishes expected idle, cached fallback, stale, failed, recovered, and
never-run states from source schedule and observation time. A previously valid
snapshot may remain visible, but its stale/failure state must remain visible.
Check the source schedule, last attempt, last success, last observation,
reason code, and consecutive failures before retrying collection.
