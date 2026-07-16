# ADR 001: Split container runtime topology

- **Status:** Accepted
- **Date:** 2026-07-16

## Context

The platform has three different lifecycles: schema migration, the internal scheduler/orchestrator HTTP service, and the public dashboard/API. A single shell that starts more than one process makes the shell an accidental supervisor, obscures failures, and can leave a partially working container reported as healthy.

## Decision

Build one image containing both Python applications and run it as three Compose services:

1. `migrate` waits for healthy PostgreSQL, runs `python cli.py migrate` once, and exits. A zero exit permits dependants through `service_completed_successfully`; any non-zero exit blocks them.
2. `orchestrator` waits for PostgreSQL and migration success, then runs uvicorn in the foreground. It is internal-only and publishes no host port.
3. `api` waits for migration success and a healthy orchestrator, then runs uvicorn in the foreground. It alone publishes application port 8000.

Docker/Compose has process ownership. Each long-running container owns exactly one foreground process and uses `restart: unless-stopped`; migration uses `restart: "no"`. Service healthchecks are bounded and dependency-aware. PostgreSQL remains a separately supervised long-running service.

Production and demo use the same split and ordering. Demo sets `DEMO_MODE=true`, which disables every collector and stream before scheduler startup, uses dummy disabled credentials, and reads only deterministic local database fixtures.

## Options considered

### Single shell

A single shell launching API and orchestrator with background `&` and `wait` was rejected. It creates ambiguous signal propagation and can hide one dead child behind another live process.

### s6

s6 would provide real supervision inside one container, but adds a supervisor and lifecycle configuration that are unnecessary when Compose can own one process per service. It was rejected for this platform.

### Split services (chosen)

Split services make independent failure, restart, health, logs, and resource ownership visible to Compose while retaining a shared image and dependency lock inputs.

## Health, restart, and migration ordering

Orchestrator health reports liveness and returns 503 when its database dependency is unavailable. API health is authenticated and proxies the orchestrator health contract, so dependency loss makes API readiness unhealthy rather than falsely green. Bounded healthcheck timeouts prevent hung probes. Compose restarts crashed long-running processes independently; migrations are never crash-looped.

## Shared storage

Configuration, prompts, routes, templates, and migrations are read-only bind mounts. The orchestrator has writable news storage; the API receives the same news storage read-only. Log storage is shared and writable only by the two services that may emit file logs. Migration receives neither news nor log storage. Demo uses local named volumes for shared storage so it cannot mutate production fixture directories; `down --volumes` removes them.

## Consequences

The API resolves the orchestrator via `http://orchestrator:8000`, and no host can connect directly to the orchestrator. Compose implementations must support `service_completed_successfully`. Building one larger image is a deliberate trade-off for identical runtime dependencies and simpler release identity.

## Rollback

Rollback is a Compose-only deployment change: pin the prior image and prior Compose files, stop the split stack, and start the previous topology. Database rollback is not automatic; migration compatibility must be assessed before application rollback. Never reintroduce the single shell background-process path as a production workaround.
