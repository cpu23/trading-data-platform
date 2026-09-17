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

## Schema, backup, and upgrade procedures

### Fresh initialization

`db/schema.sql` is the sole application schema definition. On a fresh installation
(an empty `pgdata` volume), the PostgreSQL container initializes the database schema
automatically via `/docker-entrypoint-initdb.d/001_schema.sql`. `scripts/deploy_runtime.sh`
verifies required tables and critical column compatibility upon startup before launching application services.

### Existing-volume upgrades and compatibility

The runtime intentionally contains no automated migration framework, no migration
service, and no automatic replay of `db/schema.sql` over existing database volumes.
`scripts/deploy_runtime.sh` safely validates schema compatibility against the live
database and refuses to deploy application containers if required tables or critical
columns (e.g. `jobs.run_kind`, `jobs.requested_component`, `jobs.result_ref`,
`role_heartbeats.instance_id`, `role_heartbeats.detail`) are missing, preventing
runtime errors or data corruption from unsupported legacy schemas.

For upgrades on existing database volumes, follow this explicit non-destructive procedure:

1. **Create and verify backups of database and private operator state:**
   ```bash
   mkdir -p backups
   backup_db="backups/trading-data-$(date -u +%Y%m%dT%H%M%SZ).dump"
   backup_state="backups/operatorstate-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"

   # Database backup
   docker compose exec -T postgres sh -c \
     'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_db"
   pg_restore --list "$backup_db" >/dev/null

   # Private operator state (configuration/credentials) - archived directly without printing secrets
   docker compose exec -T web sh -c \
     'tar -czf - -C /app/state .' > "$backup_state"
   chmod 600 "$backup_state"
   ```

2. **Perform an explicit compatibility review:**
   Review any new tables, columns, or indexes introduced in `db/schema.sql` against
   the current schema. Ensure that any manual schema adjustments are strictly
   non-destructive and additive (e.g. `CREATE TABLE IF NOT EXISTS`).

3. **Apply reviewed non-destructive changes (if required):**
   Quiesce all current and legacy writers before applying schema changes to prevent
   concurrent writes or partial state:
   ```bash
   docker compose stop web worker
   ```
   Execute explicitly reviewed SQL statements directly via `psql` with `ON_ERROR_STOP=1`
   so execution halts immediately upon encountering any error:
   ```bash
   docker compose exec -T postgres sh -c \
     'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < path/to/reviewed_upgrade.sql
   ```

4. **Execute runtime rollout:**
   Run the deployment script, which builds current images, verifies schema compatibility,
   and recreates application containers while preserving named volumes:
   ```bash
   ./scripts/deploy_runtime.sh
   ```

5. **Rollback if needed:**
   - **Aborted deployment / failed validation:** `scripts/deploy_runtime.sh` performs
     read-only schema validation before touching application containers or data. An aborted
     deployment leaves the live database untouched; do not execute destructive `--clean`
     database restores on an unmodified database.
   - **Reverting an upgrade after schema modifications:** To roll back an application
     release where database modifications were executed, roll back to the matching prior
     application version/commit and restore the verified database backup only into an
     explicit fresh or approved target (never rerun the new deployment script on the
     restored legacy schema):
     ```bash
     docker compose stop web worker
     # Checkout matching prior application version
     git checkout <prior-release-tag>
     # Restore verified backup into postgres target
     docker compose exec -T postgres sh -c \
       'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < "$backup_db"
     docker compose up -d
     ```

Restore only into a fresh or explicitly approved target. Do not use loose
root-level operator files as compatibility input; only complete versioned setup
snapshots are supported.

Production bootstrap uses `/setup` plus `SETUP_TOKEN`. Activation creates the
administrator password and atomically publishes a complete versioned snapshot.
Subsequent access uses `/login` and a signed `market_session` cookie. HTTP Basic
is unsupported. `DISABLE_AUTH=true` is accepted only in explicit `demo` or
`test` mode.

Browser mutations require the session, a valid double-submit CSRF token, and an
Origin matching `EXTERNAL_ORIGIN`. `TRUSTED_HOSTS` constrains Host headers.
Generate `SETUP_TOKEN`, `SESSION_SIGNING_KEY`, and `CSRF_SIGNING_KEY`
independently. `SESSION_SIGNING_KEY_PREVIOUS` provides bounded rotation grace.

`contracts.runtime_config.RuntimeConfigLoader` is the one configuration API
shared by web and worker. Each process loads one immutable snapshot from
repository defaults, environment/deployment settings, the activated operator
profile, and private secrets. Setup and Settings validate a candidate, then
save it through one atomic `current` symlink switch. A saved snapshot is not
loaded for the next run: restart both services explicitly:

```bash
docker compose restart web worker
```

The worker detects committed drift, drains at a safe boundary, writes its final
heartbeat, and exits for Compose to restart. The `/ready` endpoint reports database
connectivity (returning 503 if the database is unreachable) and active configuration
version. Worker heartbeat and role liveness are monitored via `/api/system/health`
and `roles check worker`. Credentials changed outside the commit path fail closed;
paid work is not admitted with drifted credentials.

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
3. Follow `/api/system/runs/<correlation_id>` for the run detail contract.
   `/api/system/cycle-status?correlation_id=<id>` is the cycle-status alias and
   returns the same shape: `cycle_runs` metadata, linked `job_id`/`job_state`,
   progress, and stages assembled from `collection_log` and `processing_log`.
   Non-terminal run statuses are `accepted` and `running`; terminal statuses
   include `completed`, `failed`, and `abandoned`. `result_status` reports
   outcomes such as `success`, `partial`, `failed`, `validation_failed`,
   `skipped`, or `budget_blocked`.
4. Investigate `partial` or failed runs through **Operations**, **Logs**, and
   **Quality checks**.

Closing the browser does not cancel work. `cycle_runs`, `collection_log`,
`processing_log`, and `jobs` are authoritative; browser-local state is not.

## Durable queue and recovery

The single `jobs` table has two independent execution lanes. Operation jobs have
`run_kind` (`cycle`, `collector`, `processor`, `news`, or `filings`), pair with
one `cycle_runs` row, and run in the operation lane. Analysis jobs have a
`job_type` (including snapshot publication, investment analysis, and research
jobs), do not claim the operation run lifecycle, and run in the analysis lane.
Both lanes share the same transitions:

```text
queued → leased → running → succeeded
                         ↘ failed_retryable → leased (after backoff)
                         ↘ failed_terminal
queued/leased/running/failed_retryable → suppressed_*
```

Lease expiry is reconciled back to `failed_retryable`, or to
`failed_terminal` at the attempt ceiling; operation retries reset their
`cycle_runs` row to `accepted` in the same transaction. The operation and
analysis workers claim only their own lane, so one lane's backlog does not
consume the other's throughput. Accepted operation runs commit their
`cycle_runs` row and job atomically. Workers claim with `FOR UPDATE SKIP LOCKED`;
active identity and scheduler logical-run keys suppress duplicate work.

`202 Accepted` proves admission, not completion. Jobs that exhaust
`max_attempts` remain visible as `failed_terminal`; they are never silently
dropped or retried forever.

Empty operation, analysis, and outbox polls back off from their configured base
interval to at most `max(base, 5 seconds)`. A successful claim resets the backoff;
queued work is drained without sleeping between claims. With the default base
interval, an idle worker can take up to five seconds to notice a new job.
Lease recovery and retry reconciliation run independently every 30 seconds,
including while jobs are flowing; failed maintenance retries on the next poll.

```bash
curl http://127.0.0.1:8000/api/jobs/status
docker compose exec worker /app/.venv/bin/python cli.py status
docker compose exec worker /app/.venv/bin/python -m roles check worker
```

## Health

- `/live`: the web process responds.
- `/ready`: PostgreSQL database connection is available (returns 503 if unreachable)
  and reports current configuration version.
- `/api/system/health`: bounded database, worker, freshness, and quality state.
- `/quality`: detailed operator diagnostic.
- `/api/system/topology`: exactly `postgres`, `web`, and `worker`.

Data quality is `healthy`, `degraded`, or `unknown`. Missing evidence is never
reported healthy. Quote, scheduler, jobs, and outbox detail belongs inside the
single worker node; those are not extra services or readiness identities.

Production and demo Compose health checks run every 30 seconds after startup
(six probes per minute across three services). The worker probe is the lightweight
`python -m worker_health`: it checks local process identity and a status record
no older than 12 seconds. The worker publishes that record only after configuration
validation and a successful durable database heartbeat. Missing, stale, malformed,
or mismatched-process records fail closed. Use `python -m roles check worker`
for the heavier database-backed diagnostic, not a recurring container probe.

## Browser refresh

The browser owns one visibility-aware 90-second `marketRefresh` timer. HTMX
partials refresh stored data from that event. The timer pauses while hidden and
fires once when visibility returns. There is no SSE endpoint, UI invalidation
table, or streaming fallback.

The compact dashboard/header health loader excludes quality sweeps; use
`/api/system/health` or `/quality` for detailed diagnostics. Logs retain their
three-second refresh only while the document is visible.

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

`investment_filings.run_on_startup` and `investment_filings.auto_analyze` default
to `false`: restarting the worker does not launch a filings run, and collection
does not automatically invoke model analysis. Enable these deliberately after
reviewing provider credentials and budget. Explicit manual operations remain
available. Demo mode disables all external schedules regardless of these flags.

## Research and mandatory review

Research discovery and autonomous thesis scheduling are opt-in:
`research_intelligence.schedule_enabled` and `thesis_autonomy.schedule_enabled`
default to `false`. When enabled, the scheduler enqueues discovery and the nightly
02:00 UTC thesis cycle on `jobs`. Manual commands remain available. Two independent
researcher roles produce competing candidates; evidence, opposition, citation,
score, and budget gates run before a candidate enters
`investment_thesis_proposals` as `pending_review`.

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
uv run mypy api/api_db.py orchestrator/db.py contracts
docker compose config --quiet
scripts/smoke_test.sh
```

For incidents, preserve the correlation ID, job state/attempt count, worker
heartbeat, configuration version, terminal error class, and relevant bounded
logs before retrying. Never treat a restarted container or HTTP `202` as proof
that accepted work completed.
