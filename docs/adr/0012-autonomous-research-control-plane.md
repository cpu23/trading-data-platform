# 0012: Autonomous research control plane

- Status: accepted
- Date: 2026-08-23

## Context

The platform already has four durable primitives that the research control plane must preserve:

1. PostgreSQL owns coordination, immutable market events, a transactional outbox, analysis-job leases, budget reservations, thesis versions, forecasts, outcomes and UI invalidations.
2. `analysis_jobs` provides active-identity uniqueness, `FOR UPDATE SKIP LOCKED` claims, owner-checked completion, retryable and terminal failures, and expired-lease recovery.
3. The thesis desk already provides content-addressed evidence, deterministic scoring, independent challenge/falsification, accepted-reference guards and append-only forecast outcomes.
4. `/operations` already uses server-rendered partials, one centralized EventSource and a single hidden-tab-aware heartbeat fallback.

Direct source events currently trigger bounded publication jobs and, for material event types, a coalesced full thesis-autonomy cycle. They do not preserve atomic research questions, planner decisions, skill versions, work-order effects or source/skill outcome attribution. Forecast resolution exists inside the broad autonomy cycle rather than as inspectable targeted work.

The highest existing migration is `057_thesis_metrics_nullable.sql`. The highest existing ADR is `0011-benchmark-gated-model-promotion.md`.

## Decision

### Durable ledger and coordination

Migration 058 adds:

- `research_questions`: one active row per deterministic question fingerprint, explicit unknown priority components, validated lifecycle, accepted cutoff, blockers, due/expiry bounds and bounded resolution fields;
- `research_skill_versions`: immutable typed specifications identified by `(skill_key, version)` and a content fingerprint;
- `research_plans` and `research_plan_decisions`: immutable policy/budget snapshots and stable selected/deferred/blocked explanations;
- `research_work_orders`: a typed extension anchored one-to-one to an existing `analysis_jobs` row; it owns no lease;
- `research_dependency_nodes` and `research_dependency_edges`: bounded typed dependency state and incremental dirty propagation;
- `research_source_capabilities`: typed, inspectable semantic source coverage and current availability;
- `research_effects`: append-only before/after effects, cost, runtime, evidence use and justified no-op reasons;
- `research_source_gaps`: bounded aggregation of unresolved material capabilities;
- scorecard views for work productivity, skills, sources and forecast calibration.

Partial unique indexes prevent equivalent active questions and work orders. PostgreSQL transactions reserve a question, create its work order and enqueue the existing analysis job atomically. Planner transactions take one advisory lock, lock eligible questions with `FOR UPDATE SKIP LOCKED`, and enforce per-plan cost/runtime bounds before dispatch. Existing analysis-job lease ownership remains authoritative.

### Pure domain policy

`orchestrator/research_control_plane/domain.py` owns bounded typed values, deterministic question fingerprints and lifecycle guards.

`planner.py` is pure: no SQL, network or model calls. Priority policy `v1` computes:

```text
benefit = materiality * uncertainty * discrimination * urgency * freshness * resolvability
penalty = expected_cost_usd
        + runtime_weight * expected_runtime_seconds
        + review_weight * expected_review_minutes
        + epsilon
priority = benefit / penalty
```

Every component is finite and bounded. Missing required components produce named blockers and no score. Valid numeric zero remains zero. Ordering ends with the immutable question ID. Greedy selection enforces both monetary and runtime budgets and may return a successful empty agenda.

Materiality policy `v1` compares bounded, canonical before/after state. No state change is recorded as a justified no-op; threshold crossings, status/falsification changes, core evidence changes, forecast/catalyst changes and high-materiality unresolved questions are material.

### Questions and incremental propagation

`repository.py` generates or refreshes atomic questions from promoted-candidate missing evidence, challenge required data, stale evidence, unconfirmed catalysts, matured forecasts, relevant market/source events and repeated source gaps. Cutoff participates in identity when it defines the answer boundary; refreshable dirty-state questions keep one active identity and advance only to a newer accepted cutoff.

`events.py` maps persisted market-event entities and markets to bounded dependency nodes, marks only matching theses/forecasts/catalysts dirty, refreshes questions and enqueues one debounced planner job. Broad scheduled thesis discovery remains separate.

Research lifecycle events reuse `market_events` plus its transactional outbox under a dedicated `research_control_plane` topic. That handler never routes back into dirty propagation or emits another topology event. UI invalidation uses the existing allowlisted `ui_events` ledger with section key `system_topology`. Equivalent planner jobs and active questions coalesce through deterministic identities.

### Skills and work orders

Five checked-in immutable specifications are registered at startup:

- `earnings.guidance_delta@1`;
- `filing.peer_readthrough@1`;
- `expectations.positioning_divergence@1`;
- `thesis.targeted_challenge@1`;
- `forecast.resolve@1`.

`skills.py` validates typed input/output, allowed source families and point-in-time cutoffs. Skills query bounded persisted evidence and reuse filing deltas, causal relationships, measured expectations/positioning, thesis challenge/falsification, deterministic thesis scoring and append-only forecast outcomes. Missing evidence closes to `unresolved`; it never becomes a model-generated fact. Every result distinguishes reused and newly attached evidence, structured effects, cost, runtime and human review.

A `research_skill` analysis job executes one work order. Worker start/retry/finalization mirrors the work-order status while the existing job lease remains authoritative. Handler persistence, effect recording, question resolution and analysis-job success share one transaction, so a process death cannot expose a partial accepted effect. Accepted-cutoff compare-and-set guards reject stale completions.

The single configured model remains the only production model. Skill specifications expose model policy and evaluation attribution, but planner selection never calls a model and no automatic scoring-policy or model-routing mutation is permitted.

### API and live topology

Shared Pydantic contracts define bounded status, question, work-order and topology responses.

Authenticated routes are:

- `GET /api/research/control-plane/status`;
- `GET /api/research/questions`;
- `GET /api/research/work-orders`;
- `POST /api/research/control-plane/run`;
- `GET /api/system/topology`.

The manual run only enqueues the same coalesced durable planner job used by the scheduler and event router. Global/per-plan budget policy remains authoritative. Validation occurs before database work; failures return bounded safe states without exception or payload leakage.

`topology.py` has a pure static graph builder and a bounded batched persisted-state loader. Each query family fails independently. Existing nodes and edges remain visible while failed state families become `unknown`. Activity appears only from recent role heartbeats, collection runs, jobs, outbox rows, market events, research effects or UI delivery state; inferred activity is labelled as persisted-state inference.

`/operations` renders one canonical `/partials/operations/system-topology` partial after health and before detailed tables. Semantic HTML and dependency-free inline SVG provide grouped layers, status text, keyboard-focusable nodes, a detail panel, text summary, legend, timestamps, mobile containment and reduced-motion behavior. It registers with the existing centralized SSE/fallback contract and creates no EventSource or timer.

### Ownership boundaries

- Contracts/config/migration: `contracts/contracts/{runtime_config.py,models.py}`, `config/config.yaml`, `db/migrations/058_autonomous_research_control_plane.sql`.
- Pure policy and persistence: `orchestrator/research_control_plane/`.
- Shared integration seams owned centrally: `analysis_job_handlers.py`, `job_worker.py`, `scheduler.py`, `events/{contracts.py,routing.py}`, `thesis_autonomy.py`, `ui_events.py`.
- API/topology routes: `api/routes/json/{research.py,system.py}` and `api/routes/views/operations.py`.
- Operations presentation: `api/templates/operations.html`, `api/templates/partials/system_topology.html`, `api/static/{app.js,style.css}`.

## Consequences

- PostgreSQL remains the only coordination system; no broker, workflow engine, cache or frontend framework is introduced.
- Work-order lifecycle is inspectable without duplicating lease ownership.
- Routine maintenance becomes targeted and coalesced; broad discovery remains scheduled for novel thesis generation.
- Historical skill specifications, planning snapshots, effects and outcomes remain auditable after policy or skill upgrades.
- Additional schema and SQL require real PostgreSQL concurrency, recovery and migration tests; mocks cannot prove uniqueness, lock or cutoff behavior.
- The topology reports persisted truth, not process assumptions. A healthy-looking edge cannot be inferred merely from graph existence.
- The platform remains decision support only: no order, execution, allocation or position-mutation capability is added.
