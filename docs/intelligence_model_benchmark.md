# Market Intelligence Model Benchmark

Benchmark date: 2026-06-20.

All variants used the same stored macro-regime, calendar, positioning evidence,
watchlist, schemas, safety policy, and asset-economic mappings.

## Selected production profile

- Model: `openai/gpt-oss-120b`
- Primary provider: `WandB`
- Fallback provider: `Novita`
- Analyst, skeptic, auditor, editor reasoning: `low`
- Maximum completion tokens per role: `7000`
- Site-wide operator default reasoning remains `high`

Two final directionally constrained trials completed in 73 and 72 seconds with
no repairs. Cost was $0.00181 and $0.00193. A later pair completed in 79 and 93
seconds after stronger economic-direction prompts; both published successfully.

The deployed production verification completed on 2026-06-20 in 86.4 seconds.
All four roles validated on their first attempt through W&B, total cost was
$0.00207, and the 11-opinion snapshot published atomically.

## Comparison

| Variant | Observed time | Cost | Reliability | Quality notes |
|---|---:|---:|---|---|
| GPT-OSS/W&B, low all roles | 68–93s | $0.0017–$0.0025 | Best | Concise; stable after deterministic lineage and directional mappings |
| GPT-OSS/W&B, medium roles + low editor | 83–251s | $0.0021–$0.0051 | Variable | More verbose; frequent repair calls without consistent quality gain |
| GPT-OSS/Novita | 101–207s | $0.0058–$0.0104 | Poorer | Faster individual calls, but more truncation and repairs |
| GPT-OSS fast + DeepSeek V4/SiliconFlow auditor | 143–218s | $0.0045–$0.0055 | Good | Richer commodity reasoning, but 2–3x slower |
| GPT-OSS fast + DeepSeek V4/Baidu auditor | About 3 min | Higher | Repair required | Did not improve the speed-quality frontier |
| DeepSeek V4 all roles | 4+ min capped | About $0.008 | Poor | Editor truncation; uncapped historical run took 14m14s |

## Prompt and contract changes

- Limited role claims and editor narratives.
- Added explicit asset economic channels and directional effects.
- Added CFTC market-to-asset mappings to the prompt.
- Enforced asset-level evidence permissions.
- Canonicalized claim IDs in code.
- Derived editor evidence lineage deterministically.
- Dropped unsupported optional narratives.
- Added safe low-confidence summaries for genuinely sparse assets.

The benchmark harness is `orchestrator/benchmark_intelligence.py`.
