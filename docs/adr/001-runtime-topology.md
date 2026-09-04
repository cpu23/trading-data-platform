# ADR 001: Three-service runtime topology

- **Status:** Accepted (supersedes the former split-role topology)
- **Date:** 2026-07-16
- **Amended:** 2026-09-04

## Context

The earlier deployment exposed scheduling, operation work, analysis work,
outbox publication, quote ingestion, schema migration, and API control as
separate containers. That topology duplicated configuration and package
boundaries, required internal HTTP for same-repository calls, and made operators
reason about multiple queues and heartbeats for one application workflow.

PostgreSQL already provides durable leases and logical-run identity. The work is
bounded and private to one installation, so separate process ownership does not
justify the additional runtime surface.

## Decision

Production and demo contain exactly three services:

1. `postgres` owns PostgreSQL/TimescaleDB and initializes a fresh volume from
   the authoritative `db/schema.sql`.
2. `web` owns the public FastAPI JSON API and server-rendered HTMX interface.
   API handlers call orchestration modules directly in-process; there is no
   internal control HTTP service or `ORCHESTRATOR_URL`.
3. `worker` owns scheduling, the canonical durable `jobs` queue, outbox
   publication, the quote stream, and the deterministic demo publisher.

`web` and `worker` use the same root dependency lock and application image.
Docker Compose supervises one foreground process per service. The worker uses
threads only for its cooperating internal loops and reports one durable
`worker` heartbeat containing bounded subcomponent detail.

The browser has one visibility-aware polling heartbeat. There is no SSE route,
UI invalidation queue, or streaming compatibility path.

The repository carries no runtime migration chain. A fresh database is created
from `db/schema.sql`; a schema-changing deployment must use a fresh volume or an
explicitly reviewed external database change.

## Consequences

- One release artifact, configuration schema, queue lifecycle, and worker
  heartbeat.
- No service-to-service credentials, URL routing, or control-client failure mode.
- Readiness depends on PostgreSQL and the current worker heartbeat rather than
  synthetic health for former roles.
- Scheduler, jobs, outbox, and quote ingestion restart together. This is an
  accepted availability tradeoff for substantially lower operational
  complexity.
- Scaling the worker horizontally is not a default deployment feature. Durable
  leases and scheduler leadership still prevent duplicate ownership if an
  operator deliberately does so.
- Existing split-topology Compose files and database migration state are not
  supported compatibility inputs.

## Alternatives rejected

### Split application services

Independent containers provide narrower restarts, but the previous seven
application lifecycles created more deployment and health complexity than this
single-installation workload needs.

### Shell supervisor

A shell launching background processes obscures signal propagation and child
failure. The worker remains one Python process with explicit cancellation and
health semantics.

### In-container init system

An init system would add another lifecycle abstraction without reducing the
application's durable state model.

## Rollback

Rollback pins the previous complete image and Compose definition. Database
rollback is never automatic; assess schema compatibility or restore a matching
backup before starting an older release.
