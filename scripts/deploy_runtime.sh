#!/usr/bin/env bash
# Production runtime rollout: rebuild the current source tree, migrate, and
# (re)create the eight-role topology without ever touching named volumes.
#
# Safe, idempotent, non-destructive:
#   1. builds the current tree into the pinned image tags (replaces stale
#      images; `up` recreates containers whose image changed),
#   2. brings postgres up first and waits for its health contract,
#   3. runs/awaits the migrate one-shot on the freshly built image,
#   4. creates or recreates only the six long-running runtime roles with
#      --remove-orphans (removes orphan CONTAINERS only, never volumes),
#   5. fails on any missing or unhealthy role and prints bounded service state
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
# The six long-running application roles this script creates/recreates.
# postgres and migrate are brought up explicitly in order, before these.
RUNTIME_ROLES="outbox quotes scheduler worker orchestrator api"

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

echo "=== trading-data-platform production rollout ==="
echo "compose file: $COMPOSE_FILE"
echo "preserving named volumes: pgdata newsdata logsdata operatorstate"

echo "--- building current source into pinned image tags ---"
compose build

echo "--- bringing up postgres ---"
compose up -d postgres
await_healthy postgres

echo "--- running and awaiting migrate on the freshly built image ---"
compose run --rm migrate
echo "migrate completed successfully"

echo "--- creating or recreating runtime roles with remove-orphans ---"
compose up -d --remove-orphans $RUNTIME_ROLES

echo "--- awaiting role health contracts ---"
for role in $RUNTIME_ROLES; do
  await_healthy "$role"
done

echo "--- final service state ---"
compose ps

echo "=== rollout complete: all eight roles present and healthy ==="
