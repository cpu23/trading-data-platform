# ADR 0003: PostgreSQL and TimescaleDB as the system of record

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

The platform stores time-series observations, events, versioned analytical
outputs, evidence lineage, operational logs, costs, and publication state.
Atomic publication and historical querying are core requirements.

## Decision

Use PostgreSQL 16 with TimescaleDB as the authoritative store for raw,
derived, and operational records. Use relational constraints and transactions
for lineage and publication, JSONB for versioned payloads, and hypertables or
retention functions where time-series behavior benefits from them.

## Consequences

- Raw evidence and derived intelligence remain queryable together.
- Transactional publication prevents partially visible snapshots.
- Schema changes require explicit migrations and compatibility tests.
- The database is a significant operational dependency and must be backed up.

## Alternatives considered

- Separate time-series and document databases.
- Flat files as the primary analytical store.
- A vector database for narrative memory.

