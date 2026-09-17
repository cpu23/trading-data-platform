#!/usr/bin/env bash
# Production runtime rollout: rebuild the current source tree, verify database
# schema compatibility, and (re)create the three-service topology without
# ever touching named volumes or running destructive operations.
#
# Safe, idempotent, non-destructive:
#   1. builds the current tree into the pinned image tags (replaces stale
#      images; `up` recreates containers whose image changed),
#   2. brings postgres up first and waits for its health contract,
#   3. validates schema compatibility on the database without modifying data;
#      refuses unsupported legacy or uninitialized schemas before touching
#      application containers,
#   4. creates or recreates the runtime services (web, worker) with
#      --remove-orphans (removes orphan CONTAINERS only, never volumes),
#   5. fails on any missing or unhealthy service and prints bounded service state
#      (one `ps` snapshot plus a bounded log tail).
#
# Named volumes pgdata, newsdata, logsdata, and operatorstate are preserved by
# design: this script contains no teardown, volume removal, prune, or force
# flag, and takes no arguments, so an operator cannot accidentally pass one.

set -euo pipefail

COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.yml}
export COMPOSE_FILE
MAX_ATTEMPTS=${MAX_ATTEMPTS:-90}
SLEEP_SECONDS=${SLEEP_SECONDS:-2}

# The two long-running application services this script creates/recreates.
# postgres is brought up explicitly first and verified before these.
RUNTIME_SERVICES="web worker"

compose() {
  docker compose "$@"
}

fail_with_logs() {
  echo "ERROR: $*" >&2
  echo "--- bounded service state ---" >&2
  compose ps >&2 || true
  compose logs --no-color --tail=100 >&2 || true
  exit 1
}

await_healthy() {
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
  fail_with_logs "$service did not become healthy (missing or unhealthy)"
}

check_schema_compatibility() {
  echo "--- verifying database schema compatibility ---"
  local required_tables=(
    cycle_runs
    jobs
    role_heartbeats
    market_events
    event_outbox
    source_freshness_state
    quote_state
    budget_reservations
    macro_series
    source_documents
    daily_briefings
    regime_classifications
    collection_log
    processing_log
  )
  local required_columns=(
    "jobs.run_kind"
    "jobs.requested_component"
    "jobs.result_ref"
    "jobs.job_type"
    "jobs.correlation_id"
    "jobs.state"
    "jobs.dedupe_key"
    "jobs.input_fingerprint"
    "jobs.payload"
    "role_heartbeats.role"
    "role_heartbeats.instance_id"
    "role_heartbeats.status"
    "role_heartbeats.last_heartbeat_at"
    "role_heartbeats.detail"
    "cycle_runs.correlation_id"
    "cycle_runs.status"
    "cycle_runs.accepted_at"
    "cycle_runs.run_kind"
    "cycle_runs.requested_component"
    "cycle_runs.result_status"
    "cycle_runs.summary"
    "event_outbox.id"
    "event_outbox.event_id"
    "event_outbox.topic"
    "market_events.id"
    "market_events.schema_version"
    "market_events.event_type"
    "source_freshness_state.source"
    "source_freshness_state.state"
    "quote_state.symbol"
    "quote_state.price"
    "quote_state.observed_at"
    "budget_reservations.id"
    "budget_reservations.budget_day"
    "budget_reservations.correlation_id"
    "processing_log.input_fingerprint"
    "processing_log.skip_reason"
    "processing_log.forced"
    "processing_log.correlation_id"
    "collection_log.correlation_id"
  )
  local table_sql="SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
  local column_sql="SELECT table_name || '.' || column_name FROM information_schema.columns WHERE table_schema = 'public';"
  local existing_tables
  local existing_columns
  if ! existing_tables=$(compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-trading_data}" -Atc "'"$table_sql"'"' 2>/dev/null); then
    fail_with_logs "unable to query postgres tables"
  fi
  if ! existing_columns=$(compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-trading_data}" -Atc "'"$column_sql"'"' 2>/dev/null); then
    fail_with_logs "unable to query postgres columns"
  fi
  local missing_tables=()
  for table in "${required_tables[@]}"; do
    if ! grep -Fxq "$table" <<< "$existing_tables"; then
      missing_tables+=("$table")
    fi
  done
  local missing_columns=()
  for col in "${required_columns[@]}"; do
    if ! grep -Fxq "$col" <<< "$existing_columns"; then
      missing_columns+=("$col")
    fi
  done
  local has_error=0
  if [[ ${#missing_tables[@]} -gt 0 ]]; then
    echo "ERROR: database is missing required tables: ${missing_tables[*]}" >&2
    has_error=1
  fi
  if [[ ${#missing_columns[@]} -gt 0 ]]; then
    echo "ERROR: database is missing required critical columns: ${missing_columns[*]}" >&2
    has_error=1
  fi
  if [[ $has_error -ne 0 ]]; then
    echo "The current database volume is uninitialized or uses an unsupported legacy schema." >&2
    echo "Refusing to start application containers over incompatible database." >&2
    echo "For fresh installations, initialize a new volume with db/schema.sql." >&2
    echo "For existing volumes, review docs/operations.md for backup and upgrade procedures." >&2
    exit 1
  fi
  echo "schema compatibility check passed: required tables and critical columns verified"
}

echo "=== trading-data-platform production rollout ==="
echo "compose file: $COMPOSE_FILE"
echo "preserving named volumes: pgdata newsdata logsdata operatorstate"

echo "--- building current source into pinned image tags ---"
compose build

echo "--- bringing up postgres ---"
compose up -d postgres
await_healthy postgres

check_schema_compatibility

echo "--- creating or recreating runtime services with remove-orphans ---"
compose up -d --remove-orphans $RUNTIME_SERVICES

echo "--- awaiting service health contracts ---"
for service in $RUNTIME_SERVICES; do
  await_healthy "$service"
done

echo "--- final service state ---"
compose ps

echo "=== rollout complete: all three services present and healthy ==="
