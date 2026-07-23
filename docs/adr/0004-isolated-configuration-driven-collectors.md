# ADR 0004: Isolated, configuration-driven collectors

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

External sources differ in formats, release frequency, revisions, credentials,
and failure behavior. One source outage must not prevent unrelated data from
updating or force analytical code to understand provider-specific schemas.

## Decision

Implement each source as an isolated adapter behind a shared collector
contract. Put coverage, schedules, endpoints, series definitions, and semantic
features in configuration. Normalize records with namespaced identifiers,
observation and acquisition timestamps, source metadata, frequency, region,
and release/revision information when available.

## Consequences

- Sources can be enabled, disabled, replaced, and tested independently.
- Processor inputs use semantic features rather than FRED-specific assumptions.
- Provider schema changes are localized to adapters and fixtures.
- Configuration validation becomes part of the runtime contract.

## Alternatives considered

- Hard-code all source handling in the cycle runner.
- Let each processor call providers directly.
- Normalize only at query time.

