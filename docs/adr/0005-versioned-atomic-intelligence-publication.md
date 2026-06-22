# ADR 0005: Versioned, atomic intelligence publication

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

A cycle produces related regime, briefing, asset assessment, narrative memory,
and delta outputs. Showing a mixture of old and new outputs after a processor
failure would misrepresent the analytical state.

## Decision

Give each cycle a correlation ID and stage derived outputs as validated records.
Publish all required outputs in one database transaction only when the cycle
succeeds. Retain lifecycle state, schema version, baseline lineage, evidence
references, and prior published records. Keep the previous published snapshot
visible throughout execution and after failure.

## Consequences

- The dashboard always represents one coherent published cycle.
- Failed and interrupted cycles remain auditable without becoming current.
- Historical comparisons and deterministic deltas have stable lineage.
- Publication logic is stricter: a required failure withholds the entire new
  snapshot.

## Alternatives considered

- Publish each processor result immediately.
- Overwrite one mutable “current state” row.
- Reconstruct cycle consistency in the API.

