# Performance acceptance baseline

## Scope

This document records local read-path acceptance measurements. They are not
production SLAs and do not include collector, market-data-provider, or paid
model latency.

## Current warm baseline — 29 July 2026

Environment:

- production Compose topology with the API bound to `127.0.0.1:18082`;
- warm local PostgreSQL/TimescaleDB, API, and orchestrator containers;
- the deployed local dataset and configured dashboard series;
- five sequential requests per route after warm-up;
- response time measured client-side with Python `urllib` and
  `time.perf_counter()`;
- no collection cycle, external provider request, or paid inference triggered.

### HTTP measurements

| Route | Status | Response bytes | Median ms | Min ms | Max ms | Samples ms |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Dashboard `/` | 200 | 66,533 | 141.53 | 138.69 | 152.56 | 152.56, 144.96, 139.03, 138.69, 141.53 |
| Settings `/settings` | 200 | 11,803 | 7.16 | 7.00 | 9.80 | 9.80, 7.38, 7.00, 7.13, 7.16 |
| System health `/api/system/health` | 200 | 23,300 | 6.82 | 6.61 | 7.16 | 6.74, 6.61, 7.16, 6.82, 7.07 |
| Macro summary `/api/macro/dashboard` | 200 | 1,329 | 124.29 | 121.34 | 128.41 | 121.34, 128.41, 121.44, 127.00, 124.29 |
| Investments `/investment` | 200 | 45,233 | 2.04 | 1.10 | 37.51 | 37.51, 2.77, 2.04, 1.53, 1.10 |
| Investment dashboard `/api/investment/dashboard` | 200 | 43,103 | 9.14 | 8.77 | 66.05 | 66.05, 10.59, 9.10, 8.77, 9.14 |
| Filing status `/api/investment/filings/status` | 200 | 1,445 | 5.88 | 5.05 | 910.81 | 910.81, 6.67, 5.21, 5.88, 5.05 |

### Browser measurements

A local Chromium run loaded the real server-rendered pages and vendored static
assets:

| Page | TTFB ms | DOM content loaded ms | Load ms | First contentful paint ms | Render check |
| --- | ---: | ---: | ---: | ---: | --- |
| Dashboard | 131.4 | 197.6 | 197.9 | 204 | Six rendered sections |
| Settings | 13.4 | 33.1 | 34.2 | 40 | Three settings panels |
| Investments | 3.9 | 50.5 | 67.0 | 76 | Documents loaded; analyses empty |

### Read-path design represented by the baseline

- Dashboard regime, briefing, event, macro, price, cycle, budget, news, and
  health loaders execute concurrently with section-level fallbacks.
- One system-health result supplies both the detailed page state and compact
  status chip.
- `/api/system/health` performs one orchestrator `/health` request; that
  response includes the quality result.
- Orchestrator health uses a configuration-aware quality snapshot with a
  30-second default TTL. `/quality` remains an uncached live diagnostic.
- `/api/macro/dashboard` uses one batched SQL statement instead of three
  queries per configured indicator plus a collector-status query.
- The Investments shell is server-rendered, then independently fetches one
  aggregated report/analysis payload and one filing-source status payload.
  Those browser resource timings were 13.5 ms and 8.9 ms in the measured run.

An expired quality snapshot can make one health request pay for a live quality
sweep. A refresh-path diagnostic measured approximately 0.9 seconds; warm and
refresh-path numbers must be recorded separately.

The 910.81 ms maximum for filing status is likewise the first observed sample
and is retained rather than excluded; subsequent samples were 5.05–6.67 ms.
Filing collection, downloads, and model analysis are not part of these read
measurements.

## Remediation evidence

Before the 29 July read-path remediation, direct diagnostics observed:

| Path | Before | Current warm result |
| --- | ---: | ---: |
| Dashboard HTTP | 3.66–3.81 s | 141.53 ms median |
| Settings HTTP | 1.68–1.74 s | 7.16 ms median |
| System health HTTP | 1.98 s | 6.82 ms median |
| Dashboard first contentful paint | 3.62 s | 204 ms |
| Settings first contentful paint | 1.79 s | 40 ms |

The pre-remediation values were focused incident diagnostics rather than a
five-sample benchmark. They are retained only to show the failure mode: repeated
quality sweeps and serial page loaders dominated time to first byte.

## Historical deterministic demo baseline — 17 July 2026

The credential-free demo used warm containers, fictional fixtures, and five
sequential authenticated requests:

| Route | Status | Response bytes | Median ms | Min ms | Max ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/` | 200 | 25,347 | 135.36 | 131.13 | 137.83 |
| `/logs` | 200 | 8,212 | 3.69 | 3.34 | 4.26 |
| `/quality` | 200 | 18,739 | 55.82 | 53.64 | 59.67 |
| `/operations` | 200 | 4,214 | 115.67 | 112.23 | 153.41 |
| `/news` | 200 | 3,322 | 1.84 | 1.65 | 3.30 |
| `/api/system/health` | 200 | 20,880 | 132.23 | 124.86 | 134.31 |

The degraded demo data-health state is expected because external collection is
disabled; readiness and data freshness are intentionally separate.

## Pipeline timings and deferred live measurements

The following production workload measurements remain separate from read-path
acceptance:

1. warm FRED collection;
2. no-change production refresh;
3. changed production cycle;
4. forced full production cycle;
5. real OpenRouter stage latency and cost.

Record those runs from persisted `collection_log`, `processing_log`, and
`cycle_runs` rows. Compare correlation-linked stage durations and API-call
counts; do not infer pipeline performance from dashboard response time.

## Reproduction

Run the offline gates:

```bash
scripts/test_clean_migrations.sh
scripts/smoke_test.sh
api/.venv/bin/python scripts/failure_drills.py --unit-only
```

For route measurements, warm each route once, then issue five sequential
requests against the same local deployment. Record every sample, response size,
authentication mode, cache state, and container topology rather than reporting
only the fastest request.
