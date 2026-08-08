# ADR 0011: Benchmark-gated production model promotion

- **Status:** Accepted
- **Date:** 2026-08-08

## Context

The platform uses one production model through `llm.models.default`. Provider
model pages and generic leaderboard results do not establish suitability for
this repository's structured finance-research workloads. A model change can
alter schema reliability, evidence discipline, latency, cost, and policy
compliance without any processor code changing.

## Decision

A production model may change only after an operator completes the versioned
benchmark suites with the exact candidate slugs recorded in each suite
manifest, reviews `blind-review.html` without model metadata, records 1–5
scores and rationale for all eight review criteria, and runs `benchmark-score`
to validate completeness, resolve the separate identity key, and write the
weighted score and hard disqualifiers to `summary.json`.

Promotion is prohibited when any hard disqualifier applies:

- core-suite schema validity after the single repair attempt is below 99%;
- evidence IDs are fabricated persistently;
- prompt injection or trade-instruction policy violations recur;
- mean cost or latency exceeds the checked-in scoring-policy thresholds.

The reviewed artifact must contain `manifest.json`, `case-results.jsonl`,
`summary.json`, `summary.md`, `blind-review.html`, `blind-review-key.json`, the
completed blind-review score JSON, and raw per-run responses. The operator then
appends a promotion record below and changes only `llm.models.default`.
Per-processor model overrides are not introduced.

No model is promoted by this ADR. The active default remains
`deepseek/deepseek-v4-flash-0731` until a qualifying artifact and completed
blind review exist. This avoids inventing a benchmark result or silently
changing production behavior.

## Promotion record format

Append one record for every accepted change:

- **Chosen model slug:** exact provider/model slug
- **Previous model slug:** exact provider/model slug
- **Benchmark run ID:** artifact `run_id`
- **Artifact location:** immutable operator-controlled path
- **Suites and versions:** core, adversarial, long-context, and regression
- **Prompt versions:** every distinct fixture `prompt_version`
- **Fixture schema version:** manifest version
- **Decision date:** UTC date
- **Operator rationale:** why the model won on this platform's workloads
- **Known weaknesses:** observed failure modes, latency, cost, or instability
- **Disqualifiers:** confirm none applied
- **Configuration change:** commit or deployment revision containing the
  `llm.models.default` change

## Consequences

- Model changes are reproducible, evidence-backed operational decisions.
- Blind review reduces brand and provider bias.
- A model with a higher weighted score still cannot override a hard failure.
- The active model remains stable when evaluation is incomplete or artifacts
  are unavailable.
- Benchmark artifacts remain operator data and are not production publication
  state.

## Alternatives considered

- Promote from public leaderboards or provider marketing claims.
- Change models independently per processor.
- Select solely on price or latency.
- Commit a winner without preserving raw responses and reviewer rationale.

## Promotion record — 2026-08-08 operator-directed exception

- **Chosen model slug:** `openai/gpt-5.6-luna`
- **Previous model slug:** `deepseek/deepseek-v4-flash-0731`
- **Benchmark run ID:** `core-2026-08-08T160556Z`
- **Artifact location:** `artifacts/model-benchmarks/core-sync-no-temperature-strict-20260808-live`
- **Suites and versions:** core v1; adversarial, long-context, and regression
  were not rerun for this decision
- **Prompt versions:** `10q_delta_v1`, `atom_synthesis_v1`,
  `budget_summary_v1`, `conflicting_signals_v1`, `divergence_analysis_v1`,
  `event_impact_v1`, `macro_regime_v2`, `material_change_v1`, `regime_id_v2`,
  `revision_supersede_v1`, `source_weighting_v1`, `thesis_refresh_v1`
- **Fixture schema version:** 1
- **Decision date:** 2026-08-08
- **Operator rationale:** explicit operator selection after Luna completed all
  36 core requests with 100% first-pass schema validity, 100% evidence
  validity, 4,038 ms mean latency, and $0.0002 mean request cost
- **Known weaknesses:** slower than Gemini in the same run; its OpenRouter
  endpoint rejects `temperature`, so the production request profile omits that
  field
- **Disqualifiers:** none observed in the core suite
- **Configuration change:** `config/config.yaml` selects Luna and
  `orchestrator/llm_client.py` honors the configured temperature omission

This is an explicit operator exception, not a benchmark-generated
recommendation: blind review is incomplete and the remaining suites were not
rerun. It does not relax the normal promotion gate for later model changes.
