# Architecture Decision Records

ADRs record decisions that materially shape the platform. They describe the
current decision and its consequences; they are not a backlog or a substitute
for implementation documentation.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-local-first-modular-monolith.md) | Use a local-first modular monolith deployed with Docker Compose | Accepted |
| [0002](0002-server-rendered-progressive-disclosure-ui.md) | Use FastAPI, Jinja, and HTMX for progressive-disclosure delivery | Accepted |
| [0003](0003-postgresql-timescaledb-system-of-record.md) | Use PostgreSQL and TimescaleDB as the system of record | Accepted |
| [0004](0004-isolated-configuration-driven-collectors.md) | Isolate collectors behind normalized, configuration-driven contracts | Accepted |
| [0005](0005-versioned-atomic-intelligence-publication.md) | Stage and atomically publish versioned intelligence snapshots | Accepted |
| [0006](0006-provider-neutral-structured-ai.md) | Use provider-neutral, structured, policy-constrained AI generation | Accepted |
| [0007](0007-four-role-market-intelligence-pipeline.md) | Use analyst, skeptic, auditor, and editor roles | Accepted |
| [0008](0008-separate-live-prices-from-collection-cycles.md) | Keep live prices outside collection cycles | Accepted |
| [0009](0009-private-state-and-session-authentication.md) | Store private operator state separately and use signed sessions | Accepted |
| [0010](0010-frequency-aware-source-caching.md) | Use source-aware caching and freshness semantics | Accepted |

## Record format

New records should contain:

- Status
- Date
- Context
- Decision
- Consequences
- Alternatives considered

Accepted ADRs are immutable in intent. If a decision changes, add a superseding
ADR and update the index rather than rewriting history.

