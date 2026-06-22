# ADR 0001: Local-first modular monolith

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

The platform is operated primarily by one person, contains tightly related
collection and analytical workflows, and must remain understandable and cheap
to run. A distributed system would add deployment and failure modes without a
demonstrated scaling requirement.

## Decision

Deploy a modular monolith on one host using Docker Compose. Separate runtime
responsibilities into Web/API, orchestrator, PostgreSQL/TimescaleDB, and a
one-shot state initialization container. Keep module boundaries explicit in
code without introducing a message bus, agent framework, SPA, or vector
database.

## Consequences

- Local installation, backup, debugging, and upgrades remain tractable.
- The API and orchestrator can evolve independently while sharing one database.
- Internal HTTP calls and database coordination are sufficient at current load.
- Horizontal scaling and multi-host failover are not first-class capabilities.

## Alternatives considered

- Distributed microservices with a message bus.
- A single Python process containing UI, scheduling, and collection.
- Managed cloud services as mandatory infrastructure.

