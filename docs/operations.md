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
# http://127.0.0.1:8001 — demo / demo
```

Demo mode disables external collectors, paid APIs, and the live price stream. It uses fictional local fixtures.

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
- FRED concurrency: 4.

Budget accounting includes failed attempts. If the budget state cannot be read, processing fails closed. Raw model responses and secrets are not persisted in failure telemetry.

## Health contracts

Health separates process survival from dependency and data quality:

- `liveness: ok` means the process is running.
- `readiness: ready` means required service dependencies are usable.
- `readiness: unready` returns HTTP 503 when the database or required orchestrator contract is unavailable.
- `data_health: degraded` reports stale or unhealthy data without pretending the process is dead.

The dashboard may therefore truthfully show **Degraded** while API readiness remains ready. `/api/system/health` is authenticated. The orchestrator health port is internal to Compose.

## Authentication, CSRF, and SSE

All API routes require Basic authentication except the exact SSE stream route. The authenticated token endpoint issues a short-lived, signed, path-bound, HttpOnly, SameSite-Strict cookie; the stream validates that cookie before starting its generator. Credentials and tokens never appear in the EventSource URL.

Browser mutations require a signed, expiring CSRF token and same-origin validation. JSON-only machine requests are exempt only when they carry no browser signals. API-to-orchestrator mutations forward the configured internal Basic credentials.

## News operations and cost caution

Reuters is scheduled every two hours and requires no credential. Kobeissi uses TwitterAPI.io, remains on-demand by default, and should not be scheduled until its call budget and polling frequency are explicitly approved.

```bash
docker compose exec orchestrator python cli.py news reuters
docker compose exec orchestrator python cli.py news kobeissi
docker compose exec orchestrator python cli.py news all
```

Publication is atomic: source snapshots and the unified feed are written before cursor advancement. A publication failure leaves the previous feed valid and the cursor unchanged. The dashboard shows a bounded summary; `/news` provides source and symbol/tag filters without a search database.

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

`failure_drills.py` is unit-only by default. `--docker` opts into the full smoke test. Neither default path calls paid or external data sources.
