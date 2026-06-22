# ADR 0010: Frequency-aware source caching and freshness

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

Data sources publish at different frequencies, and some payloads are immutable
within a known period. A uniform “last fetched” test produces false stale
warnings and unnecessary requests.

## Decision

Track observation, acquisition, release, and revision time where available.
Evaluate freshness according to source frequency and configured schedule.
Allow source-specific acquisition modes. In particular, fetch the Forex
Factory weekly export live on the first run for a target week, reuse that
week's immutable cache on later runs, and label stale-cache fallback
explicitly.

## Consequences

- Health state reflects expected publication cadence rather than wall-clock age
  alone.
- Weekly calendar collection avoids redundant upstream traffic.
- Operators can distinguish live, cached, and stale-cache data.
- Every special cache policy must be explicit and tested per source.

## Alternatives considered

- Fetch every source on every cycle.
- Treat any cached response as degraded.
- Use one freshness threshold for all observations.

