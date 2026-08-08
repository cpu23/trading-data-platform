# Live Platform Implementation Map

Companion plan for `market-intelligence-live-platform-implementation-spec.md`
(2026-08-05). Produced by Phase 0 repository reconnaissance. This document maps
each spec phase to exact modules, records conflicts found between the spec and
the repository, and states the resolution chosen.

## Discovered conflicts and resolutions

1. **Model config namespace.** The spec's illustrative `models:` top-level key
   does not exist; the repository stores model settings under `llm.*`
   (`llm.default_model`, `llm.models.<processor>`). Resolution: the new single
   source of truth is `llm.models.default`. Legacy `llm.default_model` and
   per-processor `llm.models.<id>` overrides remain readable for one release
   with deprecation warnings; the settings form writes only
   `llm.models.default`.

2. **Publication is not pointer-based today.** `cycle_runs.publication_status`
   exists in schema but runtime never maintains it; processors write visible
   rows directly via `publication._persist_processor_result`. Resolution:
   `section_snapshots` is additive. Phase 3 adds the snapshot publication API;
   cycles publish through it alongside their existing writes (§8.1 parity),
   legacy direct writes remain until the Phase 11 rollback window closes.

3. **News has no dedicated table.** Reuters/Kobeissi items live in JSON
   snapshots under the news volume plus `investment_research_observations`
   rows. Resolution: the event ledger (`market_events`) becomes the normalized
   news record; `story_cluster_members` reference members by `event_id` and by
   source string id, not by a news table FK.

4. **OANDA stream is memory-only.** `price_stream.QuoteStream` updates an
   in-memory dict; `market_data` snapshots only happen via manual collector
   runs. Resolution: Phase 2/5 add a bounded snapshot writer fed by the quote
   stream (fast path), keeping the manual collector untouched.

5. **Synchronous SQLAlchemy/psycopg2 everywhere.** Both apps use sync
   `get_session` context managers. Resolution: the outbox worker and job
   runner are orchestrator-side daemon threads (matching the scheduler and
   quote stream pattern); the API `/stream` SSE endpoint stays async and
   polls orchestrator state via the existing shared `httpx.AsyncClient`
   boundary. No asyncpg.

6. **No live database in unit tests.** Repo convention is unittest + mocks;
   CI has no Postgres service outside the demo smoke. Resolution: spec §25.2
   database integration tests are opt-in, gated on `TDP_TEST_DATABASE=1`
   with a reachable `postgresql://`, mirroring the existing opt-in process
7. **Spec command form `python -m orchestrator.cli`.** `orchestrator/` is a
   flat module directory run as `python cli.py` inside the container, with
   bare-module imports (`from llm_client import ...`). Adding a package
   `__init__.py` would shadow `orchestrator.py` and break every flat import.
   Resolution: the documented invocation is `cd orchestrator && uv run python
   cli.py benchmark-models ...`; all commands work identically.

8. **Model unification scope.** Spec §2.3 names macro regime, event impact,
   briefing, and market intelligence as selectors to deprecate, and states
   every processor inherits the one active model. Resolution: all five LLM
   processors (including `investment_analysis`) resolve through
   `llm.models.default`; per-processor override keys stay readable for one
   release with a deprecation warning, and are removed from the settings UI.

9. **`.env.example` drift.** It documents `LLM_*` variable names while
   compose/config consume `OPENROUTER_API_KEY`/`OPENROUTER_MODEL`.
   Resolution: `.env.example` is rewritten to the real variable names; no
   credentials added.

## Phase → module map

### Phase 1 — Model unification and benchmark skeleton
- `orchestrator/llm_client.py` — `resolve_model` precedence
  (`explicit > llm.models.default > legacy overrides with warning`); extend
  `call_llm` return with `requested_model`, `provider`, `reasoning_tokens`,
  `cached_tokens`, `retry_count`, `generation_id`, `schema` metadata.
- `orchestrator/model_benchmark.py` (new) — offline/replay harness: fixture
  loader, pinned per-model request profiles, metrics, artifact writer.
- `orchestrator/cli.py` — `benchmark-models` command.
- `orchestrator/tests/fixtures/model_eval/` — fixture schema + core seed
  cases; `orchestrator/tests/test_model_resolution.py`,
  `test_model_benchmark.py`.
- `config/config.yaml` — pin `llm.models.default:
  deepseek/deepseek-v4-flash-0731`; legacy keys kept, commented deprecated.
- `api/routes/json/settings.py` + `api/templates/settings.html` +
  `api/static/app.js` — one model field; body normalization legacy→default.
- `api/routes/json/settings.py` + orchestrator `main.py` — model preflight
  (`POST /api/settings/test-model` → orchestrator `POST /model/preflight`).
- `.env.example`, `docker-compose.demo.yml` — naming fixes, no credentials.

### Phase 2 — Event ledger, outbox, source freshness
- `db/migrations/027_market_events_outbox_freshness.sql`.
- `orchestrator/events/contracts.py`, `canonicalize.py`, `repository.py`,
  `publisher.py`, `freshness.py` — strict event envelopes, stable identities,
  transactional raw/event/outbox writes, and explainable freshness state.
- `orchestrator/collector_execution.py`, `price_stream.py` — event publication
  behind the `event_pipeline` configuration while preserving the legacy path
  when disabled.
- `orchestrator/events/worker.py` — bounded leased outbox processing with
  retry/backoff and no notification-only dependency.
- `orchestrator/main.py` — worker lifecycle plus `/events/health` and bounded
  `/events/backlog` operations endpoints.
- `config/config.yaml`, `config/features.yaml` — source contracts and rollout
  controls.

### Phase 3 — Jobs and section snapshots
- `db/migrations/028_analysis_jobs_section_snapshots.sql`.
- `orchestrator/analysis_jobs.py`, `job_worker.py` — durable leasing, retries
  with jitter, suppression states, and fingerprint no-op.
- `orchestrator/section_snapshots.py`, `analysis_job_handlers.py` —
  validate-then-publish snapshots, bounded source-health/watchlist publication,
  immutable history, and UI invalidation.
- `orchestrator/reconciliation.py` — isolated, bounded repair for jobs,
  snapshots, freshness, reaction windows, and expired UI events.
- `api/routes/json/system.py` — bounded snapshot and aggregate job-state APIs.

### Phase 4 — SSE live shell
- `db/migrations/029_ui_events.sql`.
- `orchestrator/ui_events.py` — caller-owned invalidation append, bounded replay,
  expiry, and allowlisted section/event identity.
- `api/routes/stream.py` — authenticated `GET /stream`, heartbeat,
  `Last-Event-ID` replay, coalescing, stream caps, and `resync_required`.
- Dashboard and operations partial routes plus `api/static/app.js` — native
  `EventSource` section refresh with a 45-second HTMX polling fallback.

### Phase 5 — Deterministic market state, staged macro events, materiality
- `db/migrations/030_market_state_features.sql`,
  `031_event_reaction_windows.sql`, `032_macro_release_cards.sql`, and
  `033_event_materiality.sql`.
- `orchestrator/market_state.py` — bounded price-history features, Timescale
  aggregates, deterministic labels, and idempotent feature snapshots.
- `orchestrator/reaction_windows.py`, `macro_releases.py` — mapped cross-asset
  windows with later-cycle backfill, immutable revision-aware cards, and a
  mutable staged pointer.
- `orchestrator/materiality.py` — stored multiplicative component scoring,
  suppression reasons, configurable thresholds, and debounce provenance.
- `orchestrator/events/routing.py`, `analysis_job_handlers.py` — fast-path T+0
  publication and leased T+1/T+5/T+15/T+30/T+60/session-close updates.
- `api/routes/json/events.py`, dashboard routes/templates — bounded public card
  data and SSE-refreshed macro-release monitor without model inference.
- `config/config.yaml` — market-state bounds, reaction policy,
  `macro_event_mappings`, source confidence, time sensitivity, and routing
  thresholds.

### Phase 6 — Story clustering and news
- `db/migrations/034_story_clusters.sql` (clusters, members, immutable versions)
  and `035_story_market_confirmations.sql`.
- `orchestrator/stories.py` — deterministic clustering (normalized token
  overlap, shared entity/market overlap, bounded time proximity), six lanes,
  confidence/novelty/importance formulas, immutable version audit.
- `orchestrator/story_confirmation.py` — bounded 5m/30m/session observations
  with descriptive flags; reconciliation backfill.
- `orchestrator/events/publisher.py` — durable-feed items publish normalized
  `headline_published` events plus allowlisted raw payload cache rows.
- `api/routes/views/news.py`, `api/routes/json/news.py`, templates — canonical
  story monitor, `GET /api/news/clusters`, `/partials/dashboard/news` with SSE
  `news_clusters` refresh and 90s polling fallback.

### Phase 7 — Analysis atoms and briefing refactor
- `db/migrations/037_analysis_atoms.sql` (+ evidence).
- `orchestrator/atoms.py` (new) — pipeline, evidence validation, expiry,
  supersession; budget reservation in `orchestrator/budgets.py`.
- `orchestrator/processors/*.py` — publish atoms / assemble from atoms.
- Claim-history UI partials.

### Phase 8 — Dashboard cockpit
- `api/routes/views/dashboard.py` restructure + new partials; top strip,
  change feed, dense watchlist grid, asset drawer, cross-asset panels,
  since-last-view marker (state dir).

### Phase 9 — Investment workspace v2
- `db/migrations/038_investment_workspace.sql` (themes, theses, versions,
  evidence, catalysts, risks, watch items, company profiles, portfolio
  context).
- `orchestrator/investment_workspace.py` (new), filing-delta extraction in
  `investment_service.py`, investment UI expansion. Auto-analysis stays off.

### Phase 10 — Benchmark completion and promotion ADR
- Fixture suites completed (`core` 12 cases, `adversarial`, `long_context`,
  `regression`), scoring policy, blind-review artifact, `docs/adr/` record.

### Phase 11 — Docs, demo live mode, cleanup
- `docs/current-architecture-and-operations.md`, `operations.md`,
  `performance-baseline.md` updates; demo compose exercises the fast plane
  with deterministic synthetic events; verification suite passes.

## Invariants carried into every phase

Spec §4.2 invariants 1–12 hold. Additional repo constraints preserved:
session auth + CSRF, `DISABLE_AUTH` demo behavior, UID 10001 / resource
limits / pinned images, checksum-verified migrations (no rewrites of old
migration files), unittest conventions, no paid inference in tests/demo/
backfills, public-safe content rules.
