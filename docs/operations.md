# Operations guide

This guide describes the current three-service runtime. See
[ADR 001](adr/001-runtime-topology.md) for the topology decision and
[failure-drills.md](failure-drills.md) for deterministic incident exercises.

## Runtime ownership

Compose defines exactly:

1. `postgres`: PostgreSQL/TimescaleDB. A fresh volume is initialized from
   `db/schema.sql`.
2. `web`: the public FastAPI JSON API and server-rendered HTMX interface.
3. `worker`: scheduler, canonical durable job executor, transactional outbox,
   quote stream, and deterministic demo publisher when enabled.

`web` and `worker` use one root lockfile and application image. They run as UID
10001 with `no-new-privileges`, bounded memory/PIDs, and immutable source in
normal deployment. Only `web` publishes a host port.

```bash
cp .env.example .env
# Replace every required production placeholder.
docker compose config --quiet
docker compose up -d
```

Development bind mounts and loopback PostgreSQL access are explicit:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Credential-free deterministic acceptance:

```bash
docker compose -f docker-compose.demo.yml up --build
# http://127.0.0.1:8000
```

Demo mode explicitly disables authentication and external/paid providers. The
unified worker publishes bounded fictional prices; the normal polling-only HTMX
surface consumes them.

## Schema and backup

`db/schema.sql` is the sole application schema. It initializes a fresh database
volume; the runtime contains no migration service or checksum history. A
schema-changing upgrade therefore requires a fresh volume or an explicitly
reviewed external database change.

Before either path, create and inspect a backup:

```bash
mkdir -p backups
backup="backups/trading-data-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup"
pg_restore --list "$backup" >/dev/null
```

Restore only into a fresh or explicitly approved target. Do not use loose
root-level operator files as compatibility input; only complete versioned setup
snapshots are supported.

## Authentication and configuration

Production bootstrap uses `/setup` plus `SETUP_TOKEN`. Activation creates the
administrator password and atomically publishes a complete versioned snapshot.
Subsequent access uses `/login` and a signed `market_session` cookie. HTTP Basic
is unsupported. `DISABLE_AUTH=true` is accepted only in explicit `demo` or
`test` mode.

Browser mutations require the session, a valid double-submit CSRF token, and an
Origin matching `EXTERNAL_ORIGIN`. `TRUSTED_HOSTS` constrains Host headers.
Generate `SETUP_TOKEN`, `SESSION_SIGNING_KEY`, and `CSRF_SIGNING_KEY`
independently. `SESSION_SIGNING_KEY_PREVIOUS` provides bounded rotation grace.

Setup and Settings publish a validated snapshot through one atomic `current`
symlink switch. The worker detects a restart-sensitive version change, drains
at a safe boundary, writes its final heartbeat, and exits for Compose to
restart. Until the current `worker` heartbeat reports the active version,
`/ready` returns non-2xx.

## Cycle operation

| Mode | Collection | Processing | Intended use |
| --- | --- | --- | --- |
| `refresh` | Runs enabled collectors when due | Runs changed, dependency-ready processors | Normal operation |
| `analyze` | No collection | Re-analyzes stored data within budget | New analysis without provider collection |
| `force_full` | Forces enabled collectors | Bypasses unchanged fingerprints | Explicit recovery or validation |

`force_full` is a trusted manual override. Concurrent identical cycles are
rejected rather than duplicated.

1. Open **Settings → Data & operations** and review the active model, daily cap,
   worker health, schedule, and source freshness.
2. Select **Run due cycle**. The CSRF-protected request returns a correlation ID
   only after durable acceptance.
3. Follow `/api/system/cycle-status?correlation_id=<id>`. `accepted`, `queued`,
   and `running` are non-terminal. `success`, `partial`, `failed`, and
   `validation_failed` are terminal.
4. Investigate `partial` or failed runs through **Operations**, **Logs**, and
   **Quality checks**.

Closing the browser does not cancel work. `cycle_runs`, collection logs,
processing logs, and `jobs` are authoritative; browser-local state is not.

## Durable queue and recovery

Every job kind uses the `jobs` table and the same state set, lease ownership,
attempt ceiling, retry timestamps, and terminal failure semantics. Accepted
operator runs commit their `cycle_runs` row and job atomically. Workers claim
with `FOR UPDATE SKIP LOCKED`; expired leases are reclaimable. Active identity
and scheduler logical-run keys suppress duplicate work.

`202 Accepted` proves admission, not completion. Jobs that exhaust
`max_attempts` remain visible as `failed_terminal`; they are never silently
dropped or retried forever.

```bash
curl http://127.0.0.1:8000/api/jobs/status
docker compose exec worker /app/.venv/bin/python cli.py status
docker compose exec worker /app/.venv/bin/python -m roles check worker
```

## Health

- `/live`: the web process responds.
- `/ready`: PostgreSQL, current configuration, and a fresh healthy `worker`
  heartbeat are available.
- `/api/system/health`: bounded database, worker, freshness, and quality state.
- `/quality`: detailed operator diagnostic.
- `/api/system/topology`: exactly `postgres`, `web`, and `worker`.

Data quality is `healthy`, `degraded`, or `unknown`. Missing evidence is never
reported healthy. Quote, scheduler, jobs, and outbox detail belongs inside the
single worker node; those are not extra services or readiness identities.

## Browser refresh

The browser owns one visibility-aware 90-second `marketRefresh` timer. HTMX
partials refresh stored data from that event. The timer pauses while hidden and
fires once when visibility returns. There is no SSE endpoint, UI invalidation
table, or streaming fallback.

## Budgets

`budgets.daily_llm_usd` is a hard daily cap. Before a paid call, the worker
transactionally reserves the estimate under a PostgreSQL lock. Settled spend,
active reservations, and the new estimate may not exceed the cap. Completion
settles estimated to actual cost; abandoned reservations expire explicitly.
Manual overrides are durable, correlation-scoped, expiring, and consumed once.

## News and filing operations

```bash
docker compose exec worker /app/.venv/bin/python cli.py news reuters
docker compose exec worker /app/.venv/bin/python cli.py news kobeissi
docker compose exec worker /app/.venv/bin/python cli.py news all
curl http://127.0.0.1:8000/api/investment/filings/status
```

Reuters is scheduled every two hours. Kobeissi remains on-demand pending an
explicit TwitterAPI.io budget decision. Publication is atomic: failure leaves
the prior feed and cursor intact.

Filing sources fail independently. SEC requires a descriptive user agent;
Companies House, EDINET, and OpenDART remain disabled without their source keys
and permanent issuer identifiers. Do not enable automatic model analysis merely
to test free collection.

## Research and mandatory review

The scheduler enqueues deterministic research discovery plus the nightly 02:00
UTC autonomous thesis cycle on `jobs`. Two independent researcher roles produce
competing candidates. Evidence, opposition, citation, score, and budget gates
run before a candidate enters `investment_thesis_proposals` as
`pending_review`.

Agent output never mutates canonical theses. An authenticated human must approve,
reject, or request revision. Approval materializes the canonical thesis and
records reviewer identity; proposal payload and evidence remain immutable.

```bash
docker compose exec worker /app/.venv/bin/python cli.py research-run
docker compose exec worker /app/.venv/bin/python cli.py research-status
docker compose exec worker /app/.venv/bin/python cli.py research-inspect <case-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research-retry <job-uuid>
docker compose exec worker /app/.venv/bin/python cli.py research benchmark list
```

Use `/research/theses` for the review queue and `/research/evaluation` for
point-in-time replay and scorecards.

## Verification and incident evidence

```bash
uv sync --frozen
uv run python scripts/run_bounded_tests.py discover -s api/tests
uv run python scripts/run_bounded_tests.py discover -s orchestrator/tests
uv run python scripts/run_bounded_tests.py discover -s tests
uv run ruff check api orchestrator contracts scripts tests
uv run mypy api/api_db.py api/config.py orchestrator/db.py orchestrator/config_loader.py contracts
docker compose config --quiet
scripts/smoke_test.sh
```

For incidents, preserve the correlation ID, job state/attempt count, worker
heartbeat, configuration version, terminal error class, and relevant bounded
logs before retrying. Never treat a restarted container or HTTP `202` as proof
that accepted work completed.
