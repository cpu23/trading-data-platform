# ADR 0006: Provider-neutral, structured, policy-constrained AI

- **Status:** Accepted
- **Date:** 2026-06-22

## Context

Model price, latency, capability, and availability change. AI output can also
be malformed, unsupported by supplied evidence, or cross the boundary from
market assessment into trading advice.

## Decision

Use an OpenAI-compatible client with configurable endpoint, model overrides,
reasoning effort, sampling capability fallback, provider routing, retries, and
per-attempt accounting. Require strict structured outputs. Validate schemas,
evidence eligibility, and prohibited advisory or technical-analysis language.
Allow one repair attempt; invalid output must not publish.

The policy permits economic and fundamental assessment but prohibits trade
recommendations, entries, exits, stops, targets, sizing, and allocation.

## Consequences

- Providers and models can be changed without rewriting processors.
- Every paid attempt has token, cost, latency, retry, and validation history.
- Safety is enforced deterministically in addition to prompt instructions.
- Some otherwise useful prose is rejected when it cannot satisfy the contract.

## Alternatives considered

- Provider-specific SDKs embedded in every processor.
- Free-form prose with post-hoc display filtering.
- Prompt-only safety controls.

