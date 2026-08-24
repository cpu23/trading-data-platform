# Operations guide

This guide describes the repaired split-service runtime. For topology rationale, see [ADR 001](adr/001-runtime-topology.md). For deterministic incident exercises, see [failure-drills.md](failure-drills.md).

## Runtime ownership and startup

The image is shared, but each Compose service owns one lifecycle:

1. `postgres` starts and becomes healthy.
2. `migrate` applies the checksum-verified migration chain and exits.
3. `orchestrator` serves only the internal HTTP control API.
4. `scheduler` records durable logical-run identity and enqueues work.
5. `worker` claims leased operation and analysis jobs.
6. `outbox` publishes transactional outbox rows.
7. `quotes` owns the quote stream.
8. `api` serves the public dashboard/API and is the only application service with a host port.

Application containers run as UID/GID 10001 with `no-new-privileges`, bounded
memory/PIDs, and immutable base digests. Normal Compose executes image-copied
code and configuration; named volumes hold only mutable database, state, News,
and log data. Use `docker-compose.dev.yml` explicitly for bind mounts.

Start production only after replacing every required `.env` placeholder:

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d
```

Development-only mounts and loopback PostgreSQL publication:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Use the credential-free deterministic stack for local acceptance:

```bash
docker compose -f docker-compose.demo.yml up --build
# http://127.0.0.1:8000 — demo / demo
```

A fresh demo volume has no setup state, so the browser receives the native
HTTP Basic prompt at the root (sign in with `demo` / `demo`); the demo never
shows the setup form and needs no `SETUP_TOKEN`.

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

### Authenticated browser workflow

1. Sign in, open **Settings**, and review **Data & operations** before starting
   work. The panel shows the active model and daily cap, restart/configuration
   state, required-role health, source freshness, and the next scheduled run.
2. Select **Run due cycle** for normal operation. The browser submits one
   authenticated, CSRF-protected `/api/triggers/cycle` request and retains the
   returned correlation ID.
3. Keep the page open while it follows
   `/api/system/cycle-status?correlation_id=<id>`. `accepted`, `queued`, and
   `running` are non-terminal states. `success`, `partial`, `failed`, and
   `validation_failed` are terminal outcomes; `partial` must be investigated
   rather than treated as success.
4. Expand the result or open **Operations** and **Logs** to identify the failed
   component. **Quality checks** is the live data-trust view; `/ready` is the
   dependency gate and `/live` is only process liveness.
5. Retryable operation failures are requeued automatically under the bounded
   worker policy. Start a new cycle only after a terminal result and only when
   the whole cycle should be re-evaluated. Use **Force full cycle** only when
   bypassing due-time and unchanged-input skips is intentional.

The progress display is derived from persisted `cycle_runs`, collection and
processing logs, not browser-local state. Closing the page does not cancel
accepted work. A worker crash does not erase it: the job remains claimable
after lease expiry, and the same correlation ID continues to expose the final
state. Conversely, `202 Accepted` and a disabled button prove only durable
admission, not successful execution.

After setup or a Settings save that reports `restart_required`, wait for
supervised roles to restart against the committed configuration version before
judging a run. `docker compose ps` must show required services healthy and
`/ready` must return 2xx; a version mismatch is an expected temporary
non-readiness condition, not permission to bypass the gate.

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

HTTP `202 Accepted` means the acceptance row and corresponding
`operation_jobs` row committed in one transaction. The operation worker claims
with `FOR UPDATE SKIP LOCKED`, records a lease owner and heartbeat, and applies
bounded full-jitter retry/backoff. Completion is idempotent. A killed worker
does not lose accepted work: another worker reclaims an expired lease.

Jobs that exhaust `max_attempts` enter an observable terminal failure state;
they are never silently dropped or retried forever. Active dedupe identity and
the scheduler's durable logical-run key prevent duplicate scheduled execution.
`cycle_runs` remains the user-facing operation history rather than being used
as an in-process task list.

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

Budget decisions fail closed on missing or malformed data. Before a paid call,
the worker transactionally reserves its estimate under a PostgreSQL lock;
settled spend plus active reservations plus the request may not exceed the
daily cap. Completion settles estimated to actual cost. Expired abandoned
reservations are released explicitly.

Manual overrides are durable but narrowly scoped to correlation ID, run kind,
component/requestor, and expiry. The worker validates and consumes an override
once at claim time, then mints the trusted in-process budget context. HTTP
clients cannot mint that trust token, and it is propagated to every paid call.
Raw model responses, credentials, and authorization material are not persisted
in failure telemetry.

## Health contracts

Health separates process survival, dependencies, and data trust:

- `/live` means only that the HTTP process can respond.
- `/ready` checks required database, configuration, internal-service, and role-heartbeat dependencies with bounded probes.
- Critical dependency failure returns non-2xx readiness.
- Data quality is explicitly `healthy`, `degraded`, or `unknown`.
- A missing required quality subsystem or an empty result when data is expected is `unknown`, never healthy.

`/api/system/health` is authenticated and consumes one internal orchestrator
health snapshot. Expensive quality checks may be cached for the bounded
`HEALTH_QUALITY_CACHE_SECONDS` interval; the operator `/quality` path remains an
explicit live diagnostic. Optional dependency failures can degrade status
without failing readiness, while required dependency failures cannot.

## Authentication, CSRF, and SSE

Normal deployment requires authentication; `DISABLE_AUTH` is a startup error
outside explicit demo/test mode. Browser login issues an HttpOnly,
SameSite-Strict signed session. `SESSION_SIGNING_KEY_PREVIOUS` is read-only
during rotation, while every new session uses the current key.

Authenticated browser `POST`, `PUT`, `PATCH`, and `DELETE` requests require a
valid signed CSRF token and an Origin matching `EXTERNAL_ORIGIN`. The only token
exemptions are the exact login and bootstrap-activation endpoints; broader
`/api/setup/...` prefixes are protected. `TRUSTED_HOSTS` constrains Host
headers. SSE uses a separate short-lived, path-bound token and
`SSE_SIGNING_KEY`.

First activation requires `SETUP_TOKEN` outside demo/test. Activation stages a
complete versioned state, validates checksums and parsability, then atomically
switches the current pointer. Failed or concurrent replacements cannot delete a
previous valid version. Generate `SETUP_TOKEN`, `SESSION_SIGNING_KEY`,
`CSRF_SIGNING_KEY`, and `SSE_SIGNING_KEY` independently with
`secrets.token_urlsafe(48)`; never reuse the dashboard password.

## Configuration commits and reloads

Setup and Settings publish a fully validated, versioned configuration snapshot
with one atomic `current` pointer switch. The API reads that committed snapshot
without mutating process environment variables. Invalid candidate snapshots are
rejected and the prior snapshot remains active.

Scheduler, operation/analysis worker, outbox, and quote-stream roles capture the
active configuration version at startup. After a valid commit they finish the
current safe boundary, write their final heartbeat state, and exit; Compose
restarts them against the new immutable snapshot. Until every required role
reports the active version, `/ready` returns non-2xx with the mismatch. A
rejected reload never triggers role exit. `restart_required` means the active
runtime roles must recycle; Compose normally performs that restart
automatically, while an unsupervised deployment must do so explicitly.

The worker role quiesces both operation and analysis claim loops before waiting,
so neither loop can claim fresh work while its sibling drains. Production and
demo Compose allow 30 minutes for an in-flight job to reach its transactional
finalization boundary. If the container is killed before that drain completes,
the durable lease expires and another worker reclaims the job; no heartbeat may
claim the old process stopped before both worker threads have exited.


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

## Autonomous research control-plane operations

Incremental maintenance runs every 15 minutes by default and after coalesced
relevant source events. It is independent of the broad thesis-autonomy cycle.
The planner may finish successfully with no work and no model call.

Use the authenticated `/operations` topology first, then the bounded JSON
contracts:

```bash
curl --user "$DASHBOARD_USER:$DASHBOARD_PASSWORD" \
  http://127.0.0.1:8000/api/research/control-plane/status
curl --user "$DASHBOARD_USER:$DASHBOARD_PASSWORD" \
  'http://127.0.0.1:8000/api/research/questions?limit=20'
curl --user "$DASHBOARD_USER:$DASHBOARD_PASSWORD" \
  'http://127.0.0.1:8000/api/research/work-orders?limit=20'
curl --user "$DASHBOARD_USER:$DASHBOARD_PASSWORD" \
  http://127.0.0.1:8000/api/system/topology
```

An operator-triggered `POST /api/research/control-plane/run` requires normal
authentication and CSRF, is subject to the API and global model-budget gates,
and only enqueues a coalesced durable planner job. Normal scheduled and
event-triggered operation does not need the endpoint.

Trace an incident from question UUID and accepted cutoff to plan decision,
budget reservation, work order, linked `analysis_jobs` lease, immutable skill
version and append-only effect. Restore the failed database, source or worker
and allow normal lease reconciliation to run. Do not delete queue, question,
effect or outbox rows. Retry only `failed_retryable` work; a stale accepted
cutoff must not be forced over newer research state.

Blocked questions preserve unknown inputs and unavailable or semantically
insufficient source capabilities. Fix the capability/collector rather than
substituting zero or fabricated evidence. A justified no-op is a successful
audited result, not an error.

The control plane cannot place trades, size positions, submit orders, connect
to execution APIs or mutate read-only portfolio state. Full lifecycle, skill,
budget, recovery, source-capability, scorecard and topology semantics are in
[Autonomous Research Control Plane](autonomous-research-control-plane.md).

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
