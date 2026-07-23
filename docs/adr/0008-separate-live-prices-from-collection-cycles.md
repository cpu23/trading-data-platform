# ADR 0008: Separate live prices from collection cycles

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

Live quote updates have different timing, availability, and persistence needs
from scheduled macroeconomic collection. Fetching OANDA snapshots during every
cycle added latency and confused stream health with analytical completeness.

## Decision

Run OANDA as a continuous orchestrator-managed stream. Expose the latest quote
snapshot to the Web/API container, which relays updates to the browser. Do not
fetch OANDA prices as a full-cycle collector. Provide deterministic simulated
quotes in demo mode.

## Consequences

- Market prices update independently of analytical cycles.
- An OANDA outage does not invalidate unrelated macro collection.
- Stream status must be reported separately from collector freshness.
- The current design provides recent in-process quotes rather than a complete
  tick-history platform.

## Alternatives considered

- Poll OANDA only during cycles.
- Persist every tick into the analytical database.
- Have browsers connect directly to OANDA.

