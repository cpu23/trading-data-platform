#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.demo.yml}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-trading-data-platform-demo-smoke}
export COMPOSE_PROJECT_NAME
MAX_ATTEMPTS=${MAX_ATTEMPTS:-40}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}
API_HOST_PORT=${API_HOST_PORT:-18080}
export API_HOST_PORT
API_URL=${API_URL:-http://127.0.0.1:${API_HOST_PORT}}
AUTH=${AUTH:-demo:demo}

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail_with_logs() {
  echo "ERROR: $*" >&2
  compose ps >&2 || true
  compose logs --no-color --tail=100 >&2 || true
  exit 1
}

wait_for_healthy() {
  local service=$1
  local attempt container status
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    container=$(compose ps -q "$service")
    if [[ -n "$container" ]]; then
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
      if [[ "$status" == "healthy" ]]; then
        echo "healthy: $service (attempt $attempt)"
        return 0
      fi
    fi
    sleep "$SLEEP_SECONDS"
  done
  fail_with_logs "$service did not become healthy"
}

wait_for_api() {
  local attempt
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    if curl --fail --silent --show-error --user "$AUTH" "$API_URL/api/system/health" >/dev/null 2>&1; then
      echo "ready: authenticated API (attempt $attempt)"
      return 0
    fi
    sleep "$SLEEP_SECONDS"
  done
  fail_with_logs "API did not become ready"
}

assert_api_unavailable() {
  local attempt
  for attempt in $(seq 1 15); do
    if ! curl --fail --silent --user "$AUTH" --max-time 2 "$API_URL/api/system/health" >/dev/null 2>&1; then
      echo "observed API unavailable after process failure"
      return 0
    fi
    sleep 1
  done
  fail_with_logs "API health stayed successful while a required process was killed"
}

assert_restart() {
  local service=$1
  local container before after
  container=$(compose ps -q "$service")
  [[ -n "$container" ]] || fail_with_logs "missing $service container"
  before=$(docker inspect --format '{{.State.StartedAt}}' "$container")
  compose stop -t 0 "$service" >/dev/null
  assert_api_unavailable
  compose start "$service" >/dev/null
  wait_for_healthy "$service"
  wait_for_api
  after=$(docker inspect --format '{{.State.StartedAt}}' "$container")
  [[ "$after" != "$before" ]] || fail_with_logs "$service did not restart"
  echo "restarted independently: $service"
}

echo "=== credential-free demo topology smoke ==="
docker compose -f "$COMPOSE_FILE" config --quiet
compose up -d --build

# The explicit one-shot must have completed successfully before either app starts.
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  if docker compose -f "$COMPOSE_FILE" ps --status exited migrate -q | grep -q .; then
    migrate_container=$(compose ps -a -q migrate)
    migrate_exit=$(docker inspect --format '{{.State.ExitCode}}' "$migrate_container")
    [[ "$migrate_exit" == "0" ]] || fail_with_logs "migration exited $migrate_exit"
    echo "migration one-shot completed successfully"
    break
  fi
  sleep "$SLEEP_SECONDS"
done
[[ ${migrate_exit:-unset} == "0" ]] || fail_with_logs "migration did not complete"

wait_for_healthy orchestrator
wait_for_healthy scheduler
wait_for_healthy worker
wait_for_healthy outbox
wait_for_healthy quotes
wait_for_healthy api
wait_for_api

unauthenticated_status=$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "$API_URL/api/system/health")
[[ "$unauthenticated_status" == "401" ]] ||
  fail_with_logs "protected API returned $unauthenticated_status without authentication"

orchestrator_health=$(compose exec -T orchestrator /app/orchestrator/.venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read().decode())")
printf '%s' "$orchestrator_health" | grep -q '"readiness":"ready"\|"readiness": "ready"' || fail_with_logs "orchestrator readiness contract failed"

migration_rerun=$(compose run --rm migrate)
printf '%s' "$migration_rerun" | grep -qi "no pending" || fail_with_logs "migration rerun was not idempotent"

regime=$(curl --fail --silent --show-error --user "$AUTH" "$API_URL/api/regime/current")
printf '%s' "$regime" | grep -q "controlled_expansion" || fail_with_logs "controlled_expansion fixture marker missing"
briefing=$(curl --fail --silent --show-error --user "$AUTH" "$API_URL/api/briefing/latest")
printf '%s' "$briefing" | grep -q "demo/deterministic" || fail_with_logs "demo/deterministic fixture marker missing"

# Credential-free demo mode must continuously exercise the real live-update path.
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  live_prices=$(compose exec -T postgres psql -U demo -d trading_data -Atc \
    "SELECT COUNT(*) FROM market_data WHERE source='demo-live'")
  live_events=$(compose exec -T postgres psql -U demo -d trading_data -Atc \
    "SELECT COUNT(*) FROM ui_events WHERE event_name='watchlist_changed'")
  if [[ "$live_prices" -gt 0 && "$live_events" -gt 0 ]]; then
    break
  fi
  sleep "$SLEEP_SECONDS"
done
[[ ${live_prices:-0} -gt 0 ]] || fail_with_logs "demo live price publisher produced no rows"
[[ ${live_events:-0} -gt 0 ]] || fail_with_logs "demo live publisher produced no UI invalidation"
stream=$(curl --max-time 3 --silent --show-error --user "$AUTH" \
  "$API_URL/stream?last_event_id=0" || true)
printf '%s' "$stream" | grep -q "watchlist_changed" ||
  fail_with_logs "demo SSE stream did not replay the watchlist invalidation"

python3 scripts/test_service_contracts.py \
  --compose-file "$COMPOSE_FILE" \
  --api-url "$API_URL" \
  --auth "$AUTH"

# Each PID-1 app process must stop independently, make the public health path
# unavailable, and recover cleanly without a shell supervisor. Restart-policy
# wiring is asserted by the structural topology tests.
assert_restart "api"
assert_restart "orchestrator"

echo "=== demo topology and process supervision smoke passed ==="
