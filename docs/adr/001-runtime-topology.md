# ADR 001: Split container runtime topology

- **Status:** Accepted
- **Date:** 2026-07-16

## Context

The platform has distinct schema-migration, HTTP, scheduling, operation-worker,
analysis-worker, outbox, and quote-stream lifecycles. A single shell or HTTP
lifespan that starts more than one role makes one process an accidental
supervisor, duplicates singleton work under replicas, and can report a
partially working container as healthy.

## Decision

Build one application image and run one foreground role per Compose service:

1. `migrate` waits for PostgreSQL, applies the checksum-verified migration chain, and exits.
2. `orchestrator` runs only the internal HTTP control API.
3. `scheduler` acquires durable logical-run identity and enqueues operation jobs; it never executes scheduled work inline.
4. `worker` claims leased operation and analysis jobs, heartbeats ownership, retries bounded transient failures, and exposes terminal poison-job state.
5. `outbox` owns transactional-outbox publication.
6. `quotes` owns the long-running quote stream.
7. `api` runs the public dashboard and JSON API and is the only application service with a host port.

Docker/Compose owns process supervision. Process ownership is explicit and
split by role: each long-running container owns exactly one foreground
process and uses `restart: unless-stopped`; migration uses `restart: "no"`.
No application process starts, supervises, or restarts another role.
PostgreSQL leases and durable heartbeats, rather than process-local globals,
make status and crash recovery visible across replicas.

Production and demo use the same role separation. Demo sets
`DEPLOYMENT_MODE=demo` and `DEMO_MODE=true`, uses disabled provider credentials,
and reads deterministic local fixtures.

## Options considered

### Single shell

A single shell launching API and orchestrator with background `&` and `wait` was rejected. It creates ambiguous signal propagation and can hide one dead child behind another live process.

### s6

s6 would provide real supervision inside one container, but adds a supervisor and lifecycle configuration that are unnecessary when Compose can own one process per service. It was rejected for this platform.

### Split services (chosen)

Split services make independent failure, restart, health, logs, and resource ownership visible to Compose while retaining a shared image and dependency lock inputs.

## Health, restart, and migration ordering

The internal API exposes separate liveness and dependency-aware readiness.
Role health comes from bounded database-backed heartbeats; a missing or stale
required scheduler, worker, outbox, or quote role makes readiness fail instead
of being inferred from process-local threads. Data quality is reported
separately as healthy, degraded, or unknown. Compose healthchecks are bounded.

## Shared storage

Normal deployment copies code, configuration, prompts, migrations, and
database bootstrap SQL into immutable images. Named volumes hold only writable
PostgreSQL data, private operator state, logs, and published News. The API sees
News read-only. `docker-compose.dev.yml` is the explicit development opt-in for
source/configuration bind mounts and loopback PostgreSQL publication.

## Consequences

The API resolves the internal control service through the configured
`ORCHESTRATOR_URL`; no host can connect directly to that service. Compose
implementations must support `service_completed_successfully`. One shared
application image is a deliberate trade-off for identical runtime dependencies
and a single release identity.

## Rollback

Rollback is a Compose-only deployment change: pin the prior image and prior Compose files, stop the split stack, and start the previous topology. Database rollback is not automatic; migration compatibility must be assessed before application rollback. Never reintroduce the single shell background-process path as a production workaround.
