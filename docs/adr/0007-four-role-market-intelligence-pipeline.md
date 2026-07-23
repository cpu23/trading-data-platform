# ADR 0007: Four-role market-intelligence pipeline

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

A single synthesis call tends to hide uncertainty, accept weak causal claims,
or introduce unsupported facts while editing. The platform needs useful
interpretation without manufacturing consensus.

## Decision

Run four independent, batched roles:

1. Analyst proposes the strongest evidence-bounded interpretation.
2. Skeptic challenges causality, confidence, and missing evidence.
3. Auditor checks support, freshness, contradictions, and policy.
4. Editor synthesizes only validated role claims.

Retain disagreement metadata and derive editor evidence from validated claims.
Require every configured asset exactly once. Skip paid inference when the
normalized input fingerprint has not changed.

## Consequences

- Contradictions and uncertainty remain visible rather than silently reconciled.
- The editor cannot legitimately introduce new source facts.
- Four calls cost more than one, but optimized role profiles keep the observed
  cycle cost low.
- Validation and role contracts are more complex than a single prompt.

## Alternatives considered

- One large synthesis prompt.
- An unconstrained autonomous agent loop.
- Majority voting that discards minority disagreement.

