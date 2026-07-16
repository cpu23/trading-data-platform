# Deterministic failure drills

`python scripts/failure_drills.py --unit-only` runs the upstream-free CI checks and returns nonzero on any failure. `--docker` additionally invokes the existing compose smoke test.

| Drill | Expected contract |
|---|---|
| DB unavailable | readiness 503; liveness remains `ok` in API and orchestrator |
| malformed configuration | fail closed before work |
| collector failure | failed cycle is truthful; dependent processors are skipped |
| LLM timeout | typed safe telemetry and deadline; no raw body |
| partial DB write | prior spend and fingerprint remain retained |
| restart | invoke/reference smoke does not duplicate a run |
| concurrent identical cycle | one accepted, one conflict |
| news publication failure | cursor unchanged; prior feed remains valid |
| migration checksum mismatch | abort before pending migrations |
