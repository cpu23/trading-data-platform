# Trading Data Platform Reliability, Performance, and UI Remediation Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make the platform operationally truthful, reproducible, restart-safe, substantially faster, and visibly coherent while preserving its local-first architecture and calm research-OS design.

**Architecture:** Keep the existing FastAPI API, FastAPI orchestrator, PostgreSQL/TimescaleDB, HTMX, Chart.js, and scheduler. Repair database/bootstrap truth first, then run-status and health truth, then durable execution controls, cycle performance, news integration, UI defects, and CI/container hardening. Use PostgreSQL job records and advisory locks rather than adding Redis, Celery, Kafka, or another infrastructure dependency.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL/TimescaleDB, APScheduler, httpx, structlog, HTMX, vanilla JavaScript, Chart.js, Docker Compose, `uv`, `unittest`, GitHub Actions.

---

## Scope and acceptance criteria

This plan covers every material issue found in the July 2026 assessment.

### Required functional outcomes

- `/quality` renders with HTTP 200.
- API health cannot report `healthy` when embedded data quality is degraded.
- Live-price stream appears in health and reflects real readiness.
- A collector cannot report success when all source operations or all database writes fail.
- Invalid collector/processor IDs are rejected synchronously before HTTP 202.
- Accepted jobs survive API/orchestrator restarts as persisted records and abandoned work is reconciled.
- Manual, scheduled, and full-cycle triggers share PostgreSQL advisory locks.
- Migrations are packaged in the orchestrator image, historical checksums are verified, and a missing migration directory is fatal.
- A clean database can be built and migrated reproducibly from the repository.
- Demo mode starts without real API credentials.
- FRED uses persistent connections, avoids repeated metadata calls, and uses bounded concurrency.
- Unchanged inputs do not trigger duplicate LLM analysis.
- LLM output limits, hard budget checks, attempt counts, and stage deadlines are enforced.
- News collection updates source state truthfully, rebuilds the feed transactionally, and is visible in the UI.
- The macro comparison chart renders; date axes are readable.
- The compact catalyst section is actually limited and the watchlist layout has no orphan card.
- API and orchestrator tests, clean-stack smoke tests, migration checks, and cross-service contracts run in CI.

### Performance targets

Use persisted run timing, not subjective browser timing.

- Current baseline: full cycle 51.1–77.0s; latest 76.99s.
- FRED warm run target: <=10s under normal upstream latency.
- No-change refresh target: <=15s and zero LLM calls.
- Changed full cycle target: <=45s median, acknowledging provider latency.
- Dashboard TTFB target: <=300ms locally.
- Logs page initial HTML target: <=100KB by lazy-loading details.

### Explicit non-goals

- Do not replace PostgreSQL/TimescaleDB.
- Do not introduce a broker or distributed workflow product.
- Do not rewrite HTMX/vanilla JS into React.
- Do not redesign the visual language; preserve near-black, bone, amber, compact typography, and static interactions.
- Do not silently delete the existing Financial Times schema or data.

---

## Phase 0 — Protect the current work before remediation

### Task 1: Create a verified checkpoint for the existing dirty worktree

**Objective:** Separate the existing FT-removal/news refactor from remediation commits so no work is lost or misattributed.

**Files:**
- Review: all files from `git status --short`
- Preserve: `.hermes/plans/2026-07-13_155833-platform-reliability-performance-ui-remediation.md`

**Step 1: Capture the current state**

Run:

```bash
git status --short
git diff --stat
git diff --check
git branch --show-current
git log --oneline -5
```

Expected: dirty tree containing FT removal and Reuters/Kobeissi additions; `git diff --check` passes.

**Step 2: Run the current baseline gates**

```bash
cd orchestrator && uv run python -m unittest discover -s tests -v
cd ../api && uv run python -m unittest discover -s tests -v
cd .. && python3 -m compileall -q -x '/\.venv/' api orchestrator
docker compose config --quiet
```

Expected: 44 orchestrator tests, 8 API tests, compile and Compose validation pass.

**Step 3: Review deleted FT files before checkpointing**

```bash
git diff --name-status
git diff -- db/migrations/008_financial_times.sql
git diff -- orchestrator/sources/financial_times.py
git diff -- docs/financial-times-source.md
```

Expected: the implementer understands whether deletion is intentional and does not fold database destruction into a generic cleanup.

**Step 4: Create a safety branch and checkpoint**

```bash
git switch -c fix/platform-reliability-performance-ui
git add -A
git commit -m "refactor: replace financial-times ingestion with news feeds"
```

If the FT removal is not intended, restore those files instead of committing their deletion. Do not continue with a mixed dirty baseline.

**Step 5: Verify clean scope**

```bash
git status --short
```

Expected: empty output except `.hermes/` if intentionally ignored.

---

## Phase 1 — Make configuration and demo bootstrap deterministic

### Task 2: Add conditional environment substitution

**Objective:** Disabled or demo-only sources must not require production credentials.

**Files:**
- Modify: `orchestrator/config_loader.py`
- Modify: `api/config.py`
- Test: `orchestrator/tests/test_runtime_features.py`
- Test: `api/tests/test_routes.py`

**Step 1: Write failing config tests**

Add tests proving:

```python
def test_demo_mode_does_not_require_twitter_key():
    with patch.dict(os.environ, {
        "DEMO_MODE": "true",
        "DB_USER": "demo",
        "DB_PASSWORD": "demo",
        "FRED_API_KEY": "demo-disabled",
        "OPENROUTER_API_KEY": "demo-disabled",
        "OPENROUTER_MODEL": "demo/model",
        "OANDA_API_KEY": "demo-disabled",
    }, clear=True):
        config = load_config(TEST_CONFIG_PATH)
    assert config["demo"]["enabled"] is True


def test_missing_required_enabled_source_key_fails():
    # Production + enabled Kobeissi still fails closed.
    ...
```

**Step 2: Verify RED**

```bash
cd orchestrator
uv run python -m unittest tests.test_runtime_features -v
```

Expected: demo test fails with missing `TWITTERAPI_KEY`.

**Step 3: Implement one shared substitution contract**

Create a helper in each service or, preferably, move the shared implementation into a small copied module such as `config/env_substitution.py` available to both images. Support `${NAME}` and `${NAME:-default}`. Demo mode must be determined before strict source credential validation.

Complete replacement semantics:

```python
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")

def _replace(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    value = os.environ.get(name)
    if value is not None:
        return value
    if default is not None:
        return default
    raise ValueError(f"Environment variable '{name}' referenced in config but not set")
```

Use `${TWITTERAPI_KEY:-}` for optional/on-demand credentials, while source execution itself rejects an empty key.

**Step 4: Verify GREEN**

Run both targeted tests and full suites.

**Step 5: Commit**

```bash
git add orchestrator/config_loader.py api/config.py config/config.yaml orchestrator/tests/test_runtime_features.py api/tests/test_routes.py
git commit -m "fix: make optional source credentials conditional"
```

### Task 3: Repair `.env.example` and demo Compose

**Objective:** Ensure documented quick starts contain every required setting and no dead configuration.

**Files:**
- Modify: `.env.example`
- Modify: `docker-compose.demo.yml`
- Modify: `docker-compose.yml`
- Modify: `README.md`

**Step 1: Add explicit environment values**

Add:

```dotenv
TWITTERAPI_KEY=
```

Either wire `DB_NAME` through Compose and config or remove it from `.env.example`. Preferred:

```yaml
POSTGRES_DB: ${DB_NAME:-trading_data}
```

and:

```yaml
database:
  name: ${DB_NAME:-trading_data}
```

**Step 2: Give demo mode a harmless empty/default key**

```yaml
TWITTERAPI_KEY: ""
```

**Step 3: Verify config loading**

```bash
cp .env.example /tmp/trading-data-platform.env
docker compose --env-file /tmp/trading-data-platform.env config --quiet
docker compose -f docker-compose.demo.yml config --quiet
```

**Step 4: Commit**

```bash
git add .env.example docker-compose.yml docker-compose.demo.yml config/config.yaml README.md
git commit -m "fix: make production and demo configuration reproducible"
```

---

## Phase 2 — Make database evolution reproducible

### Task 4: Package migrations in the orchestrator image

**Objective:** Make `python cli.py migrate` functional inside Docker.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker-compose.demo.yml`
- Modify: `orchestrator/migrate.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Write a failing migration-directory test**

```python
def test_migrations_directory_must_exist(tmp_path):
    with patch("migrate.MIGRATIONS_DIR", str(tmp_path / "missing")):
        with self.assertRaises(FileNotFoundError):
            run_migrations(MOCK_CONFIG)
```

**Step 2: Verify RED**

Expected: current code returns `[]` instead of raising.

**Step 3: Mount migrations read-only**

Add to orchestrator in both Compose files:

```yaml
- ./db/migrations:/app/db/migrations:ro
```

Change the default to:

```python
MIGRATIONS_DIR = os.environ.get("MIGRATIONS_DIR", "/app/db/migrations")
```

Missing directory must raise `FileNotFoundError`, never log “No pending migrations.”

**Step 4: Verify inside the built container**

```bash
docker compose build orchestrator
docker compose run --rm orchestrator python -c "from migrate import MIGRATIONS_DIR; import os; assert os.path.isdir(MIGRATIONS_DIR)"
```

**Step 5: Commit**

```bash
git add docker-compose.yml docker-compose.demo.yml orchestrator/migrate.py orchestrator/tests/test_runtime_features.py
git commit -m "fix: package runtime database migrations"
```

### Task 5: Verify historical migration checksums

**Objective:** Detect modified or missing already-applied migrations.

**Files:**
- Modify: `orchestrator/migrate.py`
- Test: create `orchestrator/tests/test_migrations.py`

**Step 1: Write failing tests**

Cover:

- Applied version missing from disk -> hard failure.
- Applied checksum differs -> hard failure.
- Null historical checksum -> backfill only when explicitly allowed.
- Pending migration -> apply once.

**Step 2: Implement migration inventory**

Return records, not just versions:

```python
def get_applied_migrations(config) -> dict[str, str | None]:
    with get_session(config) as session:
        rows = session.execute(text("SELECT version, checksum FROM schema_migrations"))
        return {row[0]: row[1] for row in rows}
```

Before applying pending files, compare all applied versions to disk and `compute_checksum()`.

**Step 3: Verify**

```bash
cd orchestrator
uv run python -m unittest tests.test_migrations -v
```

**Step 4: Commit**

```bash
git add orchestrator/migrate.py orchestrator/tests/test_migrations.py
git commit -m "fix: enforce migration history checksums"
```

### Task 6: Reconcile migrations 008 and 009 without deleting live data

**Objective:** Restore a repository migration history that matches the live database and add an explicit forward migration for FT retirement.

**Files:**
- Restore: `db/migrations/008_financial_times.sql`
- Create: `db/migrations/009_<historical_name>.sql` if recoverable from Git/session history
- Create: `db/migrations/010_retire_financial_times.sql`
- Modify: `docs/news-sources.md`

**Step 1: Recover exact historical files**

Use Git history and prior session artifacts. Do not invent SQL merely to satisfy version numbers.

```bash
git show HEAD^:db/migrations/008_financial_times.sql > /tmp/008.sql
git log --all --name-status -- db/migrations
```

Recover 009 from Git objects/history if it existed. Compare stored live checksums before restoring.

**Step 2: Define FT retirement policy**

Default safe policy: preserve FT tables and mark the feature retired. Do not drop tables in 010 unless the user explicitly approves permanent deletion after backup.

A safe 010 may add archival comments/metadata rather than destructive SQL:

```sql
COMMENT ON TABLE ft_articles IS 'Retired source data retained for historical lineage';
```

**Step 3: Test migration on a disposable clean database and a copy of the current schema**

Expected: clean migration chain succeeds; existing FT rows survive.

**Step 4: Commit**

```bash
git add db/migrations docs/news-sources.md
git commit -m "fix: reconcile financial-times migration history"
```

### Task 7: Add migration-before-start entrypoint

**Objective:** Refuse application startup against an incompatible schema.

**Files:**
- Create: `orchestrator/entrypoint.sh`
- Modify: `orchestrator/Dockerfile`
- Modify: `docker-compose.yml`
- Test: `scripts/smoke_test.sh`

**Step 1: Create entrypoint**

```bash
#!/usr/bin/env bash
set -euo pipefail
python cli.py migrate
exec uvicorn main:app --host 0.0.0.0 --port 8000
```

**Step 2: Wire and verify executable**

```dockerfile
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
```

**Step 3: Verify clean startup and second idempotent startup**

Expected: first applies pending migrations; second reports none pending; both become healthy.

**Step 4: Commit**

```bash
git add orchestrator/entrypoint.sh orchestrator/Dockerfile scripts/smoke_test.sh
git commit -m "feat: run verified migrations before orchestrator startup"
```

---

## Phase 3 — Make run status truthful

### Task 8: Return structured batch-write results

**Objective:** Stop suppressing database failures behind an integer count.

**Files:**
- Modify: `orchestrator/db.py`
- Test: create `orchestrator/tests/test_db_writes.py`

**Step 1: Write failing tests**

Cover all-success, partial failure, and all-failure.

Define:

```python
@dataclass(frozen=True)
class WriteResult:
    attempted: int
    written: int
    failed: int
    errors: tuple[str, ...]

    @property
    def status(self) -> str:
        if self.failed == 0:
            return "success"
        if self.written == 0:
            return "failed"
        return "partial"
```

**Step 2: Implement without changing orchestration yet**

Return `WriteResult` from `insert_records` and `upsert_records`.

**Step 3: Run focused tests**

Expected: all write result tests pass.

**Step 4: Commit**

```bash
git add orchestrator/db.py orchestrator/tests/test_db_writes.py
git commit -m "refactor: return structured database write outcomes"
```

### Task 9: Propagate collection and persistence failures into run status

**Objective:** Ensure run history reflects source and write failures.

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `orchestrator/collectors/fred.py`
- Test: `orchestrator/tests/test_collectors_edge_cases.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add failing acceptance tests**

- All FRED series fail -> collector status `failed`.
- Some series fail -> `partial`.
- Records fetched but every write fails -> `failed`.
- Some writes fail -> `partial`.
- Processor opinion write count zero -> `partial` or `failed` according to required-output policy.

**Step 2: Add collector outcome metadata**

Collectors should return records plus source errors, either through a `CollectionResult` object or a backwards-compatible `collector.last_errors` contract. Prefer an explicit dataclass.

**Step 3: Derive status in `run_collector`**

Status rules must be centralized and tested.

**Step 4: Verify full orchestrator suite**

**Step 5: Commit**

```bash
git add orchestrator/orchestrator.py orchestrator/collectors/fred.py orchestrator/tests
git commit -m "fix: report partial and failed collection outcomes truthfully"
```

### Task 10: Validate component IDs before accepting background work

**Objective:** Return 404/422 synchronously and prevent stuck `running` jobs.

**Files:**
- Modify: `orchestrator/main.py`
- Modify: `api/routes/json/triggers.py`
- Test: `orchestrator/tests/test_runtime_features.py`
- Test: `api/tests/test_routes.py`

**Step 1: Write failing endpoint tests**

```python
def test_unknown_collector_is_rejected_before_202():
    response = client.post("/run_collector/not-real")
    self.assertEqual(response.status_code, 404)
```

Repeat for processor.

**Step 2: Implement registry validation before job creation**

Use `get_all_collectors()`/`get_all_processors()` membership checks before creating the correlation ID record.

**Step 3: Verify no `cycle_runs` row is created for invalid IDs**

**Step 4: Commit**

```bash
git add orchestrator/main.py api/routes/json/triggers.py orchestrator/tests api/tests/test_routes.py
git commit -m "fix: reject unknown cycle components before enqueue"
```

---

## Phase 4 — Repair liveness, readiness, and data-health truth

### Task 11: Fix the Quality page 500

**Objective:** Restore `/quality` immediately with a regression test.

**Files:**
- Modify: `api/routes/views/quality.py`
- Test: `api/tests/test_routes.py`

**Step 1: Add failing test**

```python
@patch("routes.views.quality.httpx.Client")
def test_quality_page_renders(self, client_cls):
    client_cls.return_value.__enter__.return_value.get.return_value.is_success = True
    client_cls.return_value.__enter__.return_value.get.return_value.json.return_value = {
        "overall": "healthy", "checks": {}
    }
    response = client.get("/quality", headers=AUTH)
    self.assertEqual(response.status_code, 200)
```

**Step 2: Verify RED**

Expected: `Logger._log()` keyword error.

**Step 3: Use project structured logging**

Replace stdlib logger with `from logging_config import get_logger` and `get_logger("quality.view")`.

**Step 4: Verify GREEN and live HTTP 200**

**Step 5: Commit**

```bash
git add api/routes/views/quality.py api/tests/test_routes.py
git commit -m "fix: restore data-quality page rendering"
```

### Task 12: Define separate liveness, readiness, and data-health responses

**Objective:** Stop conflating “process responds” with “data is trustworthy.”

**Files:**
- Modify: `orchestrator/main.py`
- Modify: `api/routes/json/system.py`
- Test: `api/tests/test_routes.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add contract tests**

Expected contracts:

```json
{
  "liveness": "ok",
  "readiness": "ready|degraded|unready",
  "data_health": "healthy|degraded",
  "components": []
}
```

Critical database/orchestrator unavailable -> readiness non-200. Stale data -> process remains live but data health degraded.

**Step 2: Fix initialization and response-shape bugs**

Initialize `quality_warn_map` before use. Iterate orchestrator quality dictionaries with `.items()`. Never swallow contract errors silently; log them and expose an explicit degraded component.

**Step 3: Include live prices reliably**

Add the stream component after quality-map construction or make construction order independent.

**Step 4: Verify the live state**

Expected: current weekend-aware quality work may still report degraded until Task 13, but API must not report all-green.

**Step 5: Commit**

```bash
git add orchestrator/main.py api/routes/json/system.py api/tests/test_routes.py orchestrator/tests/test_runtime_features.py
git commit -m "fix: separate liveness readiness and data health"
```

### Task 13: Make FRED quality checks series- and calendar-aware

**Objective:** Remove weekend/frequency false alarms and meaningless cross-series statistics.

**Files:**
- Modify: `orchestrator/data_quality.py`
- Modify: `config/config.yaml`
- Test: `orchestrator/tests/test_data_quality.py`

**Step 1: Add failing tests**

Cover:

- Friday observation is fresh on Monday morning for a daily business-day series.
- Monthly series freshness uses a monthly threshold.
- Gap check excludes weekends.
- Gap check operates per series.
- Anomaly check never mixes values from two series.
- Future event timestamps are marked `future`, not assigned negative age.

**Step 2: Add frequency-aware configuration**

Use existing series frequency and configurable grace periods:

```yaml
data_quality:
  fred:
    grace_periods:
      daily_business: 2
      weekly: 10
      monthly: 45
      quarterly: 120
```

**Step 3: Implement per-series queries and business-day logic**

Keep implementation small. A weekday calendar is sufficient now; do not introduce a market-calendar package unless holidays become a demonstrated problem.

**Step 4: Verify quality endpoint becomes meaningful**

**Step 5: Commit**

```bash
git add orchestrator/data_quality.py config/config.yaml orchestrator/tests/test_data_quality.py
git commit -m "fix: make macro data-quality checks frequency aware"
```

---

## Phase 5 — Persist jobs and coordinate all triggers

### Task 14: Add durable job fields to `cycle_runs`

**Objective:** Represent accepted, running, completed, failed, and abandoned work transactionally.

**Files:**
- Create: `db/migrations/011_durable_jobs.sql`
- Modify: `db/init/005_cycle_runs.sql`
- Test: `orchestrator/tests/test_migrations.py`

**Step 1: Add schema migration**

Add fields such as:

```sql
ALTER TABLE cycle_runs
  ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS worker_id TEXT,
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cycle_runs_idempotency
  ON cycle_runs(idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

Extend allowed lifecycle statuses with `accepted` and `abandoned` through a safe constraint replacement.

**Step 2: Test clean and upgraded schemas**

**Step 3: Commit**

```bash
git add db/migrations/011_durable_jobs.sql db/init/005_cycle_runs.sql orchestrator/tests/test_migrations.py
git commit -m "feat: persist durable cycle job lifecycle"
```

### Task 15: Persist acceptance before returning HTTP 202

**Objective:** Guarantee every accepted request has a durable job record.

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `orchestrator/main.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Write failing endpoint test**

Assert `accepted` row exists before the response returns.

**Step 2: Add `accept_run()` and `start_run()`**

Keep lifecycle transitions explicit and conditional:

```sql
UPDATE cycle_runs
SET status='running', started_at=:started, worker_id=:worker, heartbeat_at=:started
WHERE correlation_id=:cid AND status='accepted'
```

**Step 3: Ensure all exception paths call `finish_run()`**

Background collector/processor exceptions must finalize the run.

**Step 4: Commit**

```bash
git add orchestrator/orchestrator.py orchestrator/main.py orchestrator/tests/test_runtime_features.py
git commit -m "fix: persist accepted jobs before responding"
```

### Task 16: Add PostgreSQL advisory locks for cycles and components

**Objective:** Prevent duplicates across scheduled, manual, and full-cycle triggers without another service.

**Files:**
- Create: `orchestrator/locks.py`
- Modify: `orchestrator/orchestrator.py`
- Modify: `orchestrator/scheduler.py`
- Modify: `orchestrator/main.py`
- Test: create `orchestrator/tests/test_locks.py`

**Step 1: Write lock tests**

Cover acquire, conflict, release, and release-on-exception.

**Step 2: Implement session-level advisory lock context**

Use a stable signed 64-bit key derived from `cycle`, `collector:<id>`, or `processor:<id>`.

```python
@contextmanager
def advisory_lock(name: str, config: dict):
    key = stable_lock_key(name)
    with get_session(config) as session:
        acquired = session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        ).scalar_one()
        if not acquired:
            raise RunConflict(name)
        try:
            yield
        finally:
            session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
```

Use a dedicated connection/session for the full lock lifetime.

**Step 3: Apply the same lock path everywhere**

Manual endpoint, scheduler, and full cycle must call the same wrapper.

**Step 4: Commit**

```bash
git add orchestrator/locks.py orchestrator/orchestrator.py orchestrator/scheduler.py orchestrator/main.py orchestrator/tests/test_locks.py
git commit -m "feat: coordinate runs with postgres advisory locks"
```

### Task 17: Reconcile abandoned jobs at startup

**Objective:** Mark stale accepted/running jobs abandoned after restart.

**Files:**
- Modify: `orchestrator/main.py`
- Modify: `orchestrator/orchestrator.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add failing reconciliation tests**

- Fresh running heartbeat remains running.
- Old running heartbeat becomes abandoned.
- Old accepted job becomes abandoned or requeued according to explicit policy.

**Step 2: Implement startup reconciliation**

For this local system, mark abandoned and expose a retry action; do not automatically replay LLM work without idempotency.

**Step 3: Commit**

```bash
git add orchestrator/main.py orchestrator/orchestrator.py orchestrator/tests/test_runtime_features.py
git commit -m "fix: reconcile abandoned jobs after restart"
```

### Task 18: Correct APScheduler weekday semantics

**Objective:** Make the documented Sunday calendar collection actually run Sunday.

**Files:**
- Modify: `config/config.yaml`
- Modify: `orchestrator/scheduler.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add failing next-run test**

Freeze Monday 2026-07-13 and assert the next Forex Factory run is Sunday 2026-07-19 20:00 UTC.

**Step 2: Replace ambiguous numeric weekday**

Preferred: configure named weekday through structured scheduler config, e.g. `day_of_week: sun`, instead of POSIX `0` passed to an APScheduler parser.

**Step 3: Verify live scheduler next due date**

**Step 4: Commit**

```bash
git add config/config.yaml orchestrator/scheduler.py orchestrator/tests/test_runtime_features.py
git commit -m "fix: schedule weekly calendar collection on sunday"
```

---

## Phase 6 — Fix HTTP retries, telemetry, and sensitive logging

### Task 19: Replace decorator-fixed retries with configurable retry loops

**Objective:** Honor `max_retries`, retry only transient failures, and respect `Retry-After`.

**Files:**
- Modify: `orchestrator/http_client.py`
- Test: create `orchestrator/tests/test_http_client.py`

**Step 1: Write failing tests**

- `max_retries=1` performs one attempt.
- 429 with `Retry-After` sleeps and retries.
- 500/502/503/504 retry.
- 400 does not retry.
- Attempt number and total duration are logged.

**Step 2: Implement explicit retry policy**

Avoid runtime-generated decorators. Use one loop with injectable sleep for tests.

**Step 3: Add separate connect/read timeouts**

Use `httpx.Timeout(connect=5, read=30, write=30, pool=5)` for sources; LLM read timeout remains separately configured.

**Step 4: Commit**

```bash
git add orchestrator/http_client.py orchestrator/tests/test_http_client.py
git commit -m "fix: honor configurable transient http retries"
```

### Task 20: Introduce reusable HTTP clients

**Objective:** Reuse TCP/TLS connections in collectors and API↔orchestrator calls.

**Files:**
- Modify: `orchestrator/http_client.py`
- Modify: `api/main.py`
- Modify: `api/routes/json/watchlist.py`
- Modify: `api/routes/views/quality.py`
- Test: `orchestrator/tests/test_http_client.py`
- Test: `api/tests/test_routes.py`

**Step 1: Add lifespan-managed clients**

- Orchestrator: shared sync client or collector-scoped client.
- API: `app.state.orchestrator_client = httpx.AsyncClient(...)` created in lifespan and closed at shutdown.

**Step 2: Reuse the API client in quote SSE**

Do not create a new `AsyncClient` every two seconds.

**Step 3: Verify connection reuse through tests and reduced logs**

**Step 4: Commit**

```bash
git add orchestrator/http_client.py api/main.py api/routes/json/watchlist.py api/routes/views/quality.py orchestrator/tests/test_http_client.py api/tests/test_routes.py
git commit -m "perf: reuse upstream http connections"
```

### Task 21: Redact secrets and reduce production logging

**Objective:** Ensure logs never contain API keys and stop quote-stream log flooding.

**Files:**
- Modify: `api/logging_config.py`
- Modify: `orchestrator/logging_config.py`
- Modify: `config/config.yaml`
- Test: create `orchestrator/tests/test_logging_redaction.py`

**Step 1: Add failing redaction tests**

Supply URLs containing `api_key`, `token`, `key`, and `authorization`; assert serialized logs contain `[REDACTED]` and never the value.

**Step 2: Add a processor/filter**

Redact URL query parameters and sensitive dictionary keys recursively.

**Step 3: Set dependency levels**

Production default `INFO`; `httpx`, `httpcore`, SQLAlchemy at `WARNING`. Keep an opt-in debug environment setting.

**Step 4: Separate service logs or use stdout only**

Preferred: stdout aggregation plus separate optional service files. Do not have two processes rotate the same file.

**Step 5: Sanitize existing local log and rotate exposed keys if logs left the machine**

This is an explicit operator step, not an automated source-code test.

**Step 6: Commit**

```bash
git add api/logging_config.py orchestrator/logging_config.py config/config.yaml orchestrator/tests/test_logging_redaction.py
git commit -m "fix: redact credentials and reduce dependency log noise"
```

---

## Phase 7 — Optimize FRED and database writes

### Task 22: Persist FRED metadata instead of fetching it every run

**Objective:** Remove 18 redundant metadata requests per cycle.

**Files:**
- Create: `db/migrations/012_macro_series_metadata.sql`
- Modify: `db/init/002_raw_tables.sql`
- Modify: `orchestrator/collectors/fred.py`
- Test: create `orchestrator/tests/test_fred.py`

**Step 1: Add metadata cache table**

```sql
CREATE TABLE IF NOT EXISTS macro_series_metadata (
  series_id TEXT PRIMARY KEY,
  title TEXT,
  units TEXT,
  seasonal_adjustment TEXT,
  frequency TEXT,
  fetched_at TIMESTAMPTZ NOT NULL
);
```

**Step 2: Write failing tests**

- Fresh stored metadata -> no metadata HTTP call.
- Missing/expired metadata -> one call and upsert.
- Restart/new collector instance still uses stored metadata.

**Step 3: Implement a 30-day TTL**

Metadata is effectively static. Make TTL configurable but default to 30 days.

**Step 4: Verify requests drop from 36 to 18 on warm run**

**Step 5: Commit**

```bash
git add db/migrations/012_macro_series_metadata.sql db/init/002_raw_tables.sql orchestrator/collectors/fred.py orchestrator/tests/test_fred.py
git commit -m "perf: persist fred series metadata"
```

### Task 23: Add bounded concurrent FRED observation fetching

**Objective:** Reduce FRED wall time while respecting rate limits and deterministic output.

**Files:**
- Modify: `orchestrator/collectors/fred.py`
- Modify: `config/config.yaml`
- Test: `orchestrator/tests/test_fred.py`

**Step 1: Add concurrency tests**

Use controlled fake requests to prove:

- Maximum concurrent calls never exceeds configured workers.
- Results retain configured series order.
- One failure produces partial outcome without cancelling successful series.

**Step 2: Add configuration**

```yaml
collectors:
  fred:
    max_concurrency: 4
```

**Step 3: Implement bounded worker pool**

Use `ThreadPoolExecutor(max_workers=4)` for the existing sync stack or convert only FRED to async if it makes the code simpler. Do not mix both approaches in one collector.

**Step 4: Add substage metrics**

Persist/log metadata time, observation-fetch time, parse time, and DB-write time.

**Step 5: Verify live performance**

Run one warm FRED collection. Expected <=10s under normal upstream conditions and 18 observation calls.

**Step 6: Commit**

```bash
git add orchestrator/collectors/fred.py config/config.yaml orchestrator/tests/test_fred.py
git commit -m "perf: fetch fred series with bounded concurrency"
```

### Task 24: Batch inserts and upserts

**Objective:** Replace savepoint-per-row writes with executemany while retaining partial diagnostics.

**Files:**
- Modify: `orchestrator/db.py`
- Test: `orchestrator/tests/test_db_writes.py`

**Step 1: Add batch behaviour tests**

- Success uses one executemany call.
- Batch failure falls back to isolated rows only to identify failures.
- Result counts remain truthful.

**Step 2: Implement batch-first execution**

```python
try:
    session.execute(stmt, prepared_records)
    return WriteResult(len(records), len(records), 0, ())
except Exception:
    session.rollback()
    return _write_rows_individually(...)
```

Use a new transaction for fallback after rollback.

**Step 3: Benchmark 5,000-row test fixture**

Record old/new timing in the commit or plan handoff.

**Step 4: Commit**

```bash
git add orchestrator/db.py orchestrator/tests/test_db_writes.py
git commit -m "perf: batch database writes with diagnostic fallback"
```

### Task 25: Parallelize independent top-level collectors

**Objective:** Run independent collectors concurrently while preserving processor dependency order.

**Files:**
- Modify: `orchestrator/orchestrator.py`
- Modify: `config/config.yaml`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add deterministic concurrency test**

Three fake collectors with controlled timing must overlap, while progress records remain correct and results are ordered by configured registry order.

**Step 2: Add a small cycle worker limit**

```yaml
orchestration:
  collector_workers: 3
```

**Step 3: Implement collector-layer concurrency**

Do not parallelize dependent macro regime and briefing.

**Step 4: Commit**

```bash
git add orchestrator/orchestrator.py config/config.yaml orchestrator/tests/test_runtime_features.py
git commit -m "perf: run independent collectors concurrently"
```

---

## Phase 8 — Bound LLM latency, cost, and unnecessary recomputation

### Task 26: Enforce processor-specific LLM limits

**Objective:** Reduce tail latency and unbounded response generation.

**Files:**
- Modify: `config/config.yaml`
- Modify: `orchestrator/llm_client.py`
- Modify: `orchestrator/processors/macro_regime.py`
- Modify: `orchestrator/processors/briefing.py`
- Test: `orchestrator/tests/test_briefing.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add request-payload tests**

Assert each processor sends its configured model, `max_tokens`, temperature, and structured response format when supported.

**Step 2: Add configuration**

```yaml
llm:
  stage_timeout_seconds: 90
  models:
    macro_regime: ${OPENROUTER_MODEL}
    briefing: ${OPENROUTER_MODEL}
  max_output_tokens:
    macro_regime: 1800
    briefing: 2600
```

Tune from historical valid outputs; do not truncate below required schema size.

**Step 3: Record attempts separately**

Processing log input summary should include attempt count, first-attempt duration, validation retry duration, and validation warnings.

**Step 4: Commit**

```bash
git add config/config.yaml orchestrator/llm_client.py orchestrator/processors orchestrator/tests
git commit -m "perf: bound llm output and stage latency"
```

### Task 27: Enforce hard pre-call budgets

**Objective:** Prevent the system from exceeding configured daily spend rather than merely displaying it.

**Files:**
- Create: `orchestrator/budgets.py`
- Modify: `orchestrator/llm_client.py`
- Modify: `api/budgets.py`
- Test: create `orchestrator/tests/test_budgets.py`

**Step 1: Write failing tests**

- Below cap -> call permitted.
- At cap -> automatic call rejected with typed `BudgetExceeded`.
- Explicit force request -> permitted only when propagated from an authenticated manual action.

**Step 2: Implement one shared policy contract**

API display and orchestrator enforcement must calculate the same UTC-day spend from `processing_log`.

**Step 3: Surface budget-blocked status in cycle progress**

Do not report generic failure.

**Step 4: Commit**

```bash
git add orchestrator/budgets.py orchestrator/llm_client.py api/budgets.py orchestrator/tests/test_budgets.py
git commit -m "feat: enforce daily llm spending limits"
```

### Task 28: Add processor input fingerprints and no-change skipping

**Objective:** Avoid repeated LLM work when relevant inputs and processor version are unchanged.

**Files:**
- Create: `db/migrations/013_processor_input_fingerprints.sql`
- Modify: `orchestrator/orchestrator.py`
- Modify: `orchestrator/processors/base.py`
- Modify: `orchestrator/processors/macro_regime.py`
- Modify: `orchestrator/processors/briefing.py`
- Test: create `orchestrator/tests/test_processor_fingerprints.py`

**Step 1: Define deterministic fingerprint inputs**

Include:

- Latest relevant record IDs/timestamps.
- Upstream opinion/output ID.
- Prompt version.
- Model name.
- Processor code/schema version constant.

Do not hash entire table dumps.

**Step 2: Add failing tests**

- Identical inputs -> second run skipped and no LLM call.
- New FRED observation -> macro reruns.
- New macro opinion -> briefing reruns.
- Prompt/model version change -> reruns.
- Force flag -> reruns.

**Step 3: Persist fingerprint on successful processor log**

Add `input_fingerprint`, `skip_reason`, and `forced` fields.

**Step 4: Update statuses**

Use a clear `skipped` stage status accepted by progress/UI and overall cycle calculation.

**Step 5: Commit**

```bash
git add db/migrations/013_processor_input_fingerprints.sql orchestrator/orchestrator.py orchestrator/processors orchestrator/tests/test_processor_fingerprints.py
git commit -m "perf: skip unchanged analytical processors"
```

### Task 29: Add explicit cycle modes

**Objective:** Separate routine refresh from analysis recomputation and emergency force-full execution.

**Files:**
- Modify: `orchestrator/main.py`
- Modify: `orchestrator/orchestrator.py`
- Modify: `api/routes/json/triggers.py`
- Modify: `api/templates/partials/header.html`
- Modify: `api/static/app.js`
- Test: `api/tests/test_routes.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Define modes**

- `refresh`: due/stale collectors, changed processors only.
- `analyze`: no forced collection, recompute changed processors.
- `force_full`: current behaviour with budget confirmation.

Default UI button should be `refresh`.

**Step 2: Add endpoint and UI tests**

Assert mode validation and propagation.

**Step 3: Preserve one primary button**

Use a compact adjacent menu or secondary control; do not turn the header into button furniture.

**Step 4: Commit**

```bash
git add orchestrator/main.py orchestrator/orchestrator.py api/routes/json/triggers.py api/templates/partials/header.html api/static/app.js api/tests orchestrator/tests
git commit -m "feat: add refresh analyze and force-full cycle modes"
```

---

## Phase 9 — Complete news operational behaviour

### Task 30: Make source failure state truthful

**Objective:** Prevent stale `ok` status and successful CLI exits after upstream failures.

**Files:**
- Modify: `orchestrator/sources/kobeissi.py`
- Modify: `orchestrator/sources/reuters.py`
- Modify: `orchestrator/cli.py`
- Test: `orchestrator/tests/test_news.py`

**Step 1: Add failing tests**

- HTTP failure writes state `{status: "error", error: ...}`.
- Empty legitimate result remains `ok`.
- CLI returns nonzero on source failure.
- API sources endpoint reflects failure.

**Step 2: Return typed source outcomes**

Use a `NewsCollectionResult` rather than `[]` for both “no new items” and “failed.”

**Step 3: Commit**

```bash
git add orchestrator/sources/kobeissi.py orchestrator/sources/reuters.py orchestrator/cli.py orchestrator/tests/test_news.py
git commit -m "fix: distinguish news source failure from no new items"
```

### Task 31: Make feed updates single-writer and transactional

**Objective:** Prevent lost updates and ensure collection automatically publishes a valid feed.

**Files:**
- Modify: `orchestrator/sources/news_storage.py`
- Modify: `orchestrator/sources/news_feed.py`
- Modify: `orchestrator/cli.py`
- Test: `orchestrator/tests/test_news.py`

**Step 1: Add concurrent writer test**

Two processes/threads updating the same source snapshot must not lose items.

**Step 2: Add a file lock or database single-writer lock**

Use `fcntl.flock` on Linux or reuse PostgreSQL advisory locks. Prefer PostgreSQL locks for consistency with Task 16.

**Step 3: Build feed after successful source collection**

Write source snapshot, rebuild feed to a temporary file, validate, fsync, atomic rename.

**Step 4: Commit**

```bash
git add orchestrator/sources/news_storage.py orchestrator/sources/news_feed.py orchestrator/cli.py orchestrator/tests/test_news.py
git commit -m "fix: publish news feed atomically after collection"
```

### Task 32: Integrate news with scheduler and run lineage

**Objective:** Make news collection visible, scheduled, and inspectable like other sources.

**Files:**
- Modify: `config/config.yaml`
- Modify: `orchestrator/scheduler.py`
- Modify: `orchestrator/main.py`
- Modify: `orchestrator/orchestrator.py`
- Modify: `api/routes/json/triggers.py`
- Test: `orchestrator/tests/test_news.py`
- Test: `api/tests/test_routes.py`

**Step 1: Define conservative schedules**

Keep sources individually configurable. Do not poll paid APIs aggressively. Example defaults require user approval before enabling recurring Kobeissi calls.

**Step 2: Add source-specific trigger endpoints**

Documented API claims must match actual routes.

**Step 3: Record run duration, new item count, state, and error under correlation ID**

**Step 4: Commit**

```bash
git add config/config.yaml orchestrator/scheduler.py orchestrator/main.py orchestrator/orchestrator.py api/routes/json/triggers.py orchestrator/tests/test_news.py api/tests/test_routes.py
git commit -m "feat: schedule and trace news collection"
```

---

## Phase 10 — Repair and improve the dashboard without losing capability

### Task 33: Fix the macro comparison chart

**Objective:** Render the existing six-series chart reliably with readable dates.

**Files:**
- Modify: `api/templates/base.html`
- Modify: `api/static/app.js`
- Modify: `api/static/style.css`
- Test: `api/tests/test_routes.py`

**Step 1: Add static asset contract test**

Assert the chosen date adapter is locally vendored and loaded before `app.js`, or avoid a time scale by preformatting category labels. Prefer vendored adapter if tooltips and time spacing matter.

**Step 2: Remove failure-prone hidden initialization**

Give `.regime-compare` a stable minimum height and only hide an explicit loading state, not the canvas container itself.

**Step 3: Add visible empty/error states**

Catch Chart construction errors and render a message rather than leaving a zero-size hidden canvas.

**Step 4: Verify in browser**

Expand History and evidence. Expected: chart visible, legend visible, no console errors, human dates.

**Step 5: Commit**

```bash
git add api/templates/base.html api/static/app.js api/static/style.css api/tests/test_routes.py
git commit -m "fix: render macro comparison chart reliably"
```

### Task 34: Improve individual indicator charts

**Objective:** Make chart modals analytically useful and accessible.

**Files:**
- Modify: `api/static/app.js`
- Modify: `api/templates/base.html`
- Modify: `api/static/style.css`

**Step 1: Format date labels**

Use `MMM d` or `MMM yyyy` based on range; never raw ISO strings.

**Step 2: Add metadata**

Display unit, latest value, change, range, and relevant reference line such as zero for yield spreads.

**Step 3: Fix modal focus behaviour**

On open: save trigger, focus close button/dialog. Trap Tab inside. On close: restore trigger focus and body scrolling.

**Step 4: Browser acceptance**

Verify mouse, Enter/Space, Escape, Tab, close button, and outside click.

**Step 5: Commit**

```bash
git add api/static/app.js api/templates/base.html api/static/style.css
git commit -m "feat: improve indicator chart context and accessibility"
```

### Task 35: Make the catalyst section genuinely compact

**Objective:** Render the computed top catalysts rather than every high-impact event.

**Files:**
- Modify: `api/templates/partials/events_section.html`
- Modify: `api/routes/views/dashboard.py`
- Test: `api/tests/test_routes.py`

**Step 1: Add template-context test**

Provide ten high-impact events and assert compact output contains six while expanded calendar contains all ten.

**Step 2: Render `catalysts` directly**

Group same-time release families where useful, but retain individual actual/expected/previous values.

**Step 3: Browser verify collapsed and expanded states**

**Step 4: Commit**

```bash
git add api/templates/partials/events_section.html api/routes/views/dashboard.py api/tests/test_routes.py
git commit -m "fix: limit compact catalyst view"
```

### Task 36: Rebalance the watchlist layout and selected state

**Objective:** Remove the orphan card and make expansion state obvious.

**Files:**
- Modify: `api/static/style.css`
- Modify: `api/templates/partials/cards_section.html`
- Modify: `api/static/app.js`

**Step 1: Preserve all existing data and interactions**

Inventory: symbol, bias, live price, timestamp, summary, keyboard activation, expansion panel, evidence.

**Step 2: Use a balanced responsive grid**

Use `repeat(auto-fit, minmax(...))` with a sensible maximum or explicit wide-screen 5/4 layout. Avoid one orphan card at the current desktop viewport.

**Step 3: Add selected styling**

Selected card gets stronger hairline/amber or foreground treatment; non-selected cards may dim slightly. No glow or animation.

**Step 4: Explain unavailable prices**

Replace ambiguous em dash with `unavailable` plus tooltip/reason when known.

**Step 5: Browser verify desktop and narrow layouts**

**Step 6: Commit**

```bash
git add api/static/style.css api/templates/partials/cards_section.html api/static/app.js
git commit -m "fix: rebalance watchlist and selected state"
```

### Task 37: Surface operational navigation and truthful exceptions

**Objective:** Put logs, quality, and degraded state within immediate reach.

**Files:**
- Modify: `api/templates/partials/header.html`
- Modify: `api/templates/partials/system_health.html`
- Modify: `api/static/style.css`
- Test: `api/tests/test_routes.py`

**Step 1: Add quiet header links**

Dashboard, Logs, Quality, News. Keep typography compact and hierarchy subordinate to the primary cycle control.

**Step 2: Make degraded state actionable**

Show concise reason and link to Quality/Logs. Do not show “Healthy” when data health is degraded.

**Step 3: Browser verify**

All pages must have persistent return navigation without scrolling to the footer.

**Step 4: Commit**

```bash
git add api/templates/partials/header.html api/templates/partials/system_health.html api/static/style.css api/tests/test_routes.py
git commit -m "feat: expose operational navigation and health exceptions"
```

### Task 38: Add a compact news view

**Objective:** Make Reuters/Kobeissi data useful without turning the dashboard into a news terminal.

**Files:**
- Create: `api/templates/news.html`
- Create: `api/routes/views/news.py`
- Modify: `api/routes/views/__init__.py`
- Modify: `api/templates/dashboard.html`
- Create: `api/templates/partials/news_section.html`
- Modify: `api/static/style.css`
- Test: `api/tests/test_routes.py`

**Step 1: Add route tests**

- `/news` renders feed and source state.
- Missing feed shows a useful empty state.
- Source error is visible.
- Dashboard compact section limits items.

**Step 2: Implement compact dashboard summary**

Show 5–8 newest items, source, age, matched symbols/tags, and a News link. No auto-scrolling ticker.

**Step 3: Implement full news page filters**

Source and symbol/tag only; YAGNI—no search database until needed.

**Step 4: Browser verify**

**Step 5: Commit**

```bash
git add api/templates api/routes/views api/static/style.css api/tests/test_routes.py
git commit -m "feat: surface unified news feed in dashboard"
```

### Task 39: Lazy-load log details and improve duration scanning

**Objective:** Reduce logs HTML size and make cycle bottlenecks obvious.

**Files:**
- Modify: `api/routes/views/logs.py`
- Modify: `api/routes/json/system.py`
- Modify: `api/templates/partials/log_rows.html`
- Modify: `api/static/app.js`
- Modify: `api/static/style.css`
- Test: `api/tests/test_routes.py`

**Step 1: Stop embedding prompt/raw-response details initially**

Initial `_fetch_logs()` uses `include_detail=False`. Add authenticated detail endpoint by log ID.

**Step 2: Lazy fetch on row expansion**

Keep hidden detail rows out of initial DOM.

**Step 3: Format durations**

Use `40.9s`, `1.4s`, `638ms`; visually flag top-duration stages without gradients.

**Step 4: Add cycle-stage waterfall or proportional duration bar to selected recent run**

Static hairline bars only. Preserve raw table and filters.

**Step 5: Verify initial logs HTML <=100KB**

```bash
curl -su "$DASHBOARD_USER:$DASHBOARD_PASSWORD" http://127.0.0.1:8001/logs -o /tmp/logs.html
wc -c /tmp/logs.html
```

**Step 6: Commit**

```bash
git add api/routes api/templates/partials/log_rows.html api/static/app.js api/static/style.css api/tests/test_routes.py
git commit -m "perf: lazy-load log details and expose stage timing"
```

---

## Phase 11 — CI, containers, and production acceptance

### Task 40: Run API tests and smoke tests in CI

**Objective:** Catch the failures the current mocked/import-only CI missed.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `scripts/smoke_test.sh`

**Step 1: Add API test step**

Set `TWITTERAPI_KEY: ""` and run:

```bash
uv run python -m unittest discover -s tests -v
```

**Step 2: Add clean demo smoke job**

Run `scripts/smoke_test.sh`, including `/quality`, `/api/news/sources`, and `/api/news/feed` empty-or-valid contract.

**Step 3: Add cleanup and diagnostic logs on failure**

Always run `docker compose ... logs` before teardown when smoke fails.

**Step 4: Commit**

```bash
git add .github/workflows/ci.yml scripts/smoke_test.sh
git commit -m "ci: run api and full-stack smoke tests"
```

### Task 41: Add clean migration and cross-service contract tests

**Objective:** Verify a brand-new database and live service contracts.

**Files:**
- Create: `scripts/test_clean_migrations.sh`
- Create: `scripts/test_service_contracts.py`
- Modify: `.github/workflows/ci.yml`

**Step 1: Build a disposable project name and volume**

Never touch the developer’s live volume.

**Step 2: Assert**

- Migrations directory present.
- Clean DB reaches latest migration.
- Second migration run is idempotent.
- API health reflects orchestrator quality contract.
- Quality page 200.
- Invalid trigger IDs rejected.
- Restart reconciliation marks stale work abandoned.

**Step 3: Add CI job**

**Step 4: Commit**

```bash
git add scripts/test_clean_migrations.sh scripts/test_service_contracts.py .github/workflows/ci.yml
git commit -m "ci: verify migrations and cross-service contracts"
```

### Task 42: Pin and harden container images

**Objective:** Improve reproducibility and reduce container blast radius without breaking local development.

**Files:**
- Modify: `api/Dockerfile`
- Modify: `orchestrator/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.demo.yml`

**Step 1: Pin base and tool images**

Use explicit versions/digests for Python, uv, and TimescaleDB.

**Step 2: Add non-root users**

Ensure mounted data/log paths have correct permissions.

**Step 3: Add healthchecks**

- Orchestrator readiness.
- API authenticated or dedicated unauthenticated liveness endpoint.
- API depends on orchestrator healthy, not merely started.

**Step 4: Add conservative limits**

Define PID and memory limits appropriate for local hardware. Do not set read-only root filesystem until all write paths are mapped explicitly.

**Step 5: Verify Compose and smoke tests**

**Step 6: Commit**

```bash
git add api/Dockerfile orchestrator/Dockerfile docker-compose.yml docker-compose.demo.yml
git commit -m "chore: pin and harden service containers"
```

### Task 43: Tighten CORS and internal service authentication

**Objective:** Remove unsafe wildcard credential policy and protect orchestrator mutations.

**Files:**
- Modify: `api/main.py`
- Modify: `orchestrator/main.py`
- Modify: `api/routes/json/triggers.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Test: `api/tests/test_routes.py`
- Test: `orchestrator/tests/test_runtime_features.py`

**Step 1: Add auth-negative tests**

- Unauthenticated API mutation rejected.
- Missing/incorrect internal service token rejected.
- Trusted local origin accepted; arbitrary origin not granted credentialed CORS.

**Step 2: Configure trusted origins**

Default to `http://127.0.0.1:8001` and `http://localhost:8001`; no wildcard with credentials.

**Step 3: Add internal bearer token**

API sends it to orchestrator; orchestrator validates on mutating endpoints. Keep health/readiness available only inside the Compose network as appropriate.

**Step 4: Commit**

```bash
git add api/main.py orchestrator/main.py api/routes/json/triggers.py .env.example docker-compose.yml api/tests orchestrator/tests
git commit -m "fix: restrict cors and authenticate internal mutations"
```

### Task 44: Add linting, typing, and dependency/image checks

**Objective:** Catch obvious defects before runtime without creating a months-long typing project.

**Files:**
- Modify: `api/pyproject.toml`
- Modify: `orchestrator/pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Step 1: Add Ruff with narrow baseline rules**

Start with syntax/import/undefined-name rules. Do not reformat the entire repository in this task.

**Step 2: Add dependency audit**

Use `uv audit` or a pinned supported equivalent.

**Step 3: Add container scan**

Build images and run Trivy with a defined severity threshold.

**Step 4: Commit**

```bash
git add api/pyproject.toml orchestrator/pyproject.toml .github/workflows/ci.yml
git commit -m "ci: add lint dependency and container checks"
```

---

## Phase 12 — Final integration and measured acceptance

### Task 45: Run the full local acceptance matrix

**Objective:** Prove the complete platform works before merge.

**Files:**
- Modify only if failures require targeted fixes.

**Step 1: Static and unit gates**

```bash
python3 -m compileall -q -x '/\.venv/' api orchestrator
cd orchestrator && uv run python -m unittest discover -s tests -v
cd ../api && uv run python -m unittest discover -s tests -v
cd ..
docker compose config --quiet
docker compose -f docker-compose.demo.yml config --quiet
git diff --check
```

Expected: all pass.

**Step 2: Clean-stack gates**

```bash
scripts/test_clean_migrations.sh
scripts/smoke_test.sh
```

Expected: all service, Quality, News, auth, and migration probes pass.

**Step 3: Live production-mode gates**

- Dashboard 200.
- Logs 200.
- Quality 200.
- News feed 200 or documented empty state.
- Health truth matches orchestrator data quality.
- No secret values appear in new logs.
- Scheduler shows Sunday Forex Factory run.

**Step 4: Performance benchmark**

Run and record:

1. Warm FRED collection.
2. No-change refresh.
3. Changed/forced full cycle.
4. Dashboard/logs response size and TTFB.

Compare with baseline and attach results to the final handoff.

**Step 5: Browser visual acceptance**

Capture screenshots at desktop and narrow viewport. Verify:

- Comparison chart visible.
- Indicator dates readable.
- Balanced watchlist.
- Selected card obvious.
- Compact catalysts limited.
- Health degraded/healthy states truthful.
- News section present.
- Header navigation practical.
- No console errors.

Use Playwright screenshot plus `vision_analyze` as required by project convention.

**Step 6: Commit final integration fixes**

```bash
git add <only-files-changed-for-final-fixes>
git commit -m "fix: complete platform acceptance remediation"
```

### Task 46: Update operator documentation and handoff

**Objective:** Ensure the repository documents how the repaired system is operated.

**Files:**
- Modify: `README.md`
- Modify: `docs/news-sources.md`
- Create: `docs/operations.md`
- Create: `docs/performance-baseline.md`

**Step 1: Document**

- Cycle modes.
- Scheduler semantics.
- Hard budgets.
- Migration/startup process.
- Job abandonment/retry behaviour.
- News source scheduling and cost caution.
- Health contract meanings.
- Log redaction and retention.
- Backup/restore before destructive migrations.

**Step 2: Record measured before/after performance**

Use actual numbers only.

**Step 3: Commit**

```bash
git add README.md docs/operations.md docs/performance-baseline.md docs/news-sources.md
git commit -m "docs: add repaired platform operations and performance guide"
```

### Task 47: Final code and scope review

**Objective:** Ensure the implementation matches this plan without accidental feature loss.

**Files:**
- Review: complete branch diff

**Step 1: Run spec-compliance review**

Verify every required functional outcome and performance target. Explicitly list any deferred item with evidence and rationale.

**Step 2: Run code-quality review**

Focus on:

- No duplicate configuration loaders or health semantics.
- No broad silent exception swallowing.
- No new broker/infrastructure dependency.
- No removed dashboard capability.
- No secret-bearing fixtures or logs.
- Migration history intact.

**Step 3: Inspect branch scope**

```bash
git status --short
git log --oneline --decorate master..HEAD
git diff --stat master...HEAD
git diff --check master...HEAD
```

**Step 4: Push only after all gates pass**

```bash
git push -u origin fix/platform-reliability-performance-ui
```

Do not merge automatically. Present the branch, benchmark results, screenshots, migration notes, and any remaining risks for user review.

---

## Risks and tradeoffs

1. **Dirty baseline risk:** The current FT-removal/news refactor must be intentionally checkpointed or restored before remediation. Mixing it into later commits will make review and rollback unsafe.
2. **Migration history risk:** The live database contains applied versions absent from the worktree. Recover exact historical SQL/checksums; do not fabricate history.
3. **Schema deletion risk:** Preserve FT data by default. Destructive removal requires backup and explicit approval.
4. **FRED rate-limit risk:** Bounded concurrency must be conservative and observable. Start at four workers.
5. **LLM model variability:** The <=45s changed-cycle target depends partly on provider latency. Hard deadlines and output caps control tails but cannot guarantee upstream speed.
6. **Fingerprint correctness:** An incomplete fingerprint can incorrectly skip analysis. Tests must cover every meaningful upstream input and prompt/model version.
7. **Lock lifetime:** PostgreSQL session advisory locks require a dedicated live connection for their full scope. Connection-pool handling must be tested carefully.
8. **Health semantics:** Data-health degradation should not make the process liveness endpoint fail. Keep liveness, readiness, and data quality distinct.
9. **News cost:** Do not enable frequent paid Kobeissi polling by default without explicit schedule/cost approval.
10. **UI regression risk:** Preserve indicator modal, keyboard interaction, card expansion, evidence, logs filtering, cycle progress, and live quotes while redesigning layout.

## Open questions to resolve during execution

- Is the current Financial Times removal intentional, and should historical FT tables remain indefinitely or be exported then dropped later?
- What recurring Kobeissi polling schedule and daily API-call budget is acceptable?
- Should a stale accepted job be automatically retried after restart or only marked abandoned for manual retry? This plan defaults to manual retry.
- Which OpenRouter model should be used independently for macro regime and briefing after latency benchmarking?
- Should `force_full` be available directly in the header menu or restricted to an operations page?

## Recommended implementation sequence

Execute phases in order. Do not begin cycle-performance or UI work until migration history, status truth, health truth, and a clean CI baseline are in place. After each task, run its targeted test, obtain spec-compliance review, obtain code-quality review, and commit before moving forward.
