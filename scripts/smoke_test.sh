#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.demo.yml}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-trading-data-platform-demo-smoke}
API_HOST_PORT=${API_HOST_PORT:-18080}
API_URL=${API_URL:-http://127.0.0.1:${API_HOST_PORT}}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-40}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}
export COMPOSE_PROJECT_NAME API_HOST_PORT

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
cleanup() { compose down --volumes --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail_with_logs() {
  echo "FAIL: $1" >&2
  compose ps -a >&2 || true
  compose logs --no-color >&2 || true
  exit 1
}

wait_for_healthy() {
  local service=$1
  local container status
  for _ in $(seq 1 "$MAX_ATTEMPTS"); do
    container=$(compose ps -a -q "$service")
    if [[ -n "$container" ]]; then
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")
      [[ "$status" == "healthy" ]] && return
    fi
    sleep "$SLEEP_SECONDS"
  done
  fail_with_logs "$service did not become healthy"
}

wait_for_web() {
  for _ in $(seq 1 "$MAX_ATTEMPTS"); do
    curl --fail --silent --show-error "$API_URL/api/system/health" >/dev/null 2>&1 && return
    sleep "$SLEEP_SECONDS"
  done
  fail_with_logs "web endpoint did not become available"
}

assert_restart() {
  local service=$1 before after container
  container=$(compose ps -q "$service")
  before=$(docker inspect --format '{{.State.StartedAt}}' "$container")
  compose restart "$service" >/dev/null
  wait_for_healthy "$service"
  [[ "$service" == "web" ]] && wait_for_web
  container=$(compose ps -q "$service")
  after=$(docker inspect --format '{{.State.StartedAt}}' "$container")
  [[ "$before" != "$after" ]] || fail_with_logs "$service did not restart"
}

echo "=== three-service demo smoke ==="
docker compose -f "$COMPOSE_FILE" config --quiet
compose up -d --build

for service in postgres web worker; do
  wait_for_healthy "$service"
done
wait_for_web

services=$(compose config --services | sort)
[[ "$services" == $'postgres\nweb\nworker' ]] || fail_with_logs "compose does not define exactly postgres, web, worker"

root_status=$(curl --silent --output /dev/null --write-out '%{http_code}' -H 'Accept: text/html' "$API_URL/")
[[ "$root_status" == "200" ]] || fail_with_logs "credential-free demo root returned $root_status"
regime=$(curl --fail --silent --show-error "$API_URL/api/regime/current")
printf '%s' "$regime" | grep -q "controlled_expansion" || fail_with_logs "controlled_expansion fixture marker missing"
briefing=$(curl --fail --silent --show-error "$API_URL/api/briefing/latest")
printf '%s' "$briefing" | grep -q "demo/deterministic" || fail_with_logs "demo/deterministic fixture marker missing"

for _ in $(seq 1 "$MAX_ATTEMPTS"); do
  live_prices=$(compose exec -T postgres psql -U demo -d trading_data -Atc "SELECT COUNT(*) FROM live_prices")
  [[ ${live_prices:-0} -gt 0 ]] && break
  sleep "$SLEEP_SECONDS"
done
[[ ${live_prices:-0} -gt 0 ]] || fail_with_logs "worker demo quote publisher produced no rows"

python3 scripts/test_service_contracts.py --compose-file "$COMPOSE_FILE" --api-url "$API_URL"
assert_restart web
assert_restart worker

echo "=== three-service demo smoke passed ==="
