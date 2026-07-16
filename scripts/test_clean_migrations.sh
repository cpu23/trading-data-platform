#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.demo.yml}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-trading-data-platform-migration-test}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-40}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}
export COMPOSE_PROJECT_NAME

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
compose build migrate >/dev/null
compose up -d postgres >/dev/null

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  container=$(compose ps -q postgres)
  status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
  if [[ "$status" == "healthy" ]]; then
    echo "healthy: postgres (attempt $attempt)"
    break
  fi
  sleep "$SLEEP_SECONDS"
done
[[ ${status:-unset} == "healthy" ]] || { echo "FAIL postgres did not become healthy"; exit 1; }

first=$(compose run --rm migrate)
printf '%s\n' "$first" | grep -qi "Applied\|no pending" || { echo "FAIL initial migration run"; exit 1; }
echo "PASS initial migration run"

second=$(compose run --rm migrate)
printf '%s\n' "$second" | grep -qi "no pending" || { echo "FAIL migration rerun was not idempotent"; exit 1; }
echo "PASS migration rerun: no pending migrations"
