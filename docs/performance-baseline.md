# Performance acceptance baseline

## Scope

These are measured local acceptance numbers from 17 July 2026 after the reliability and UI remediation. They are not production-SLA claims and do not include live FRED, OANDA, OpenRouter, Reuters, or TwitterAPI.io latency.

Environment:

- credential-free deterministic Docker demo;
- API bound to `127.0.0.1:18080`;
- warm local PostgreSQL/TimescaleDB and application containers;
- five sequential authenticated requests per route;
- response time measured client-side with Python `urllib` and `time.perf_counter()`;
- response size is the final body size from the fifth request.

## HTTP baseline

| Route | Status | Response bytes | Median ms | Min ms | Max ms |
|---|---:|---:|---:|---:|---:|
| `/` | 200 | 25,347 | 135.36 | 131.13 | 137.83 |
| `/logs` | 200 | 8,212 | 3.69 | 3.34 | 4.26 |
| `/quality` | 200 | 18,739 | 55.82 | 53.64 | 59.67 |
| `/operations` | 200 | 4,214 | 115.67 | 112.23 | 153.41 |
| `/news` | 200 | 3,322 | 1.84 | 1.65 | 3.30 |
| `/api/system/health` | 200 | 20,880 | 132.23 | 124.86 | 134.31 |

The demo health response reported `liveness: ok`, `readiness: ready`, and `data_health: degraded`. That degraded state is expected because external collection is disabled; it proves that readiness and data freshness are reported separately.

## Browser acceptance

A fully local Chromium/CDP run used vendored Chart.js and HTMX assets and blocked non-local hostnames. Measured browser state:

- six comparison-chart datasets, each with three dated fixture points;
- chart loading completed with `aria-busy=false`;
- selected and expanded instrument: `AUDJPY`;
- News section rendered the truthful unpublished empty state;
- desktop page overflow: false;
- 390 px page overflow: false (`scrollWidth=390`, `clientWidth=390`);
- console errors: zero.
- dedicated News page navigation and filters remained usable at desktop and 390 px;
- News source state and unpublished empty state were truthful, with no visible clipping, overlap, or overflow.

Acceptance screenshots were written outside the repository:

- `/home/mrw/trading-dashboard-phase12-interactive.png`
- `/home/mrw/trading-dashboard-phase12-interactive-mobile.png`
- `/home/mrw/trading-news-phase12-final-desktop.png`
- `/home/mrw/trading-news-phase12-final-mobile.png`

## Pipeline timings and deferred live measurements

The deterministic demo fixtures record representative fictional stage durations only; they are not live benchmark evidence. No legitimate pre-remediation timing dataset was retained, so this document does not invent a before/after percentage.

The following production measurements remain intentionally unverified because acceptance was required to avoid live and paid upstream calls:

1. warm FRED collection;
2. no-change production refresh;
3. changed production cycle;
4. forced full production cycle;
5. real OpenRouter stage latency and cost.

When production credentials and an approved call budget are available, record those runs from persisted `collection_log`, `processing_log`, and `cycle_runs` rows. Compare correlation-linked stage durations and API-call counts; do not infer performance from wall-clock impressions or demo fixture values.

## Reproduction

Run the offline gates:

```bash
scripts/test_clean_migrations.sh
scripts/smoke_test.sh
api/.venv/bin/python scripts/failure_drills.py --unit-only
```

For route measurements, start `docker-compose.demo.yml`, warm each route once, then issue five authenticated sequential requests against localhost. Record all samples rather than reporting only the fastest request.
