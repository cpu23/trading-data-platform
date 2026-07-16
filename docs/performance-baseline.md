# Performance acceptance baseline

## Scope

These are measured local acceptance numbers from 16 July 2026 after the reliability and UI remediation. They are not production-SLA claims and do not include live FRED, OANDA, OpenRouter, Reuters, or TwitterAPI.io latency.

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
| `/` | 200 | 27,447 | 120.89 | 116.28 | 125.51 |
| `/logs` | 200 | 7,944 | 2.41 | 2.31 | 2.63 |
| `/quality` | 200 | 18,471 | 55.04 | 51.75 | 55.29 |
| `/operations` | 200 | 4,034 | 106.88 | 95.33 | 108.93 |
| `/api/system/health` | 200 | 20,880 | 105.86 | 101.96 | 108.20 |

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

Acceptance screenshots were written outside the repository:

- `/home/mrw/trading-dashboard-phase12-interactive.png`
- `/home/mrw/trading-dashboard-phase12-interactive-mobile.png`

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
