#!/usr/bin/env bash
set -euo pipefail

echo "=== Smoke test: trading-data-platform ==="

# Validate compose files
docker compose config --quiet && echo "✓ docker compose config valid"
docker compose -f docker-compose.demo.yml config --quiet && echo "✓ demo compose config valid"

# Start demo mode
docker compose -f docker-compose.demo.yml up -d --build
trap 'docker compose -f docker-compose.demo.yml down 2>/dev/null' EXIT

# Wait for API
echo "Waiting for API..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8001/api/system/health -u demo:demo > /dev/null 2>&1; then
    echo "✓ API is up (attempt $i)"
    break
  fi
  sleep 2
done

# Verify health endpoint
HEALTH=$(curl -sf http://127.0.0.1:8001/api/system/health -u demo:demo)
echo "✓ Health: $(echo $HEALTH | python3 -c 'import sys,json; print(json.load(sys.stdin)["overall"])' 2>/dev/null || echo 'parse failed')"

# Verify regime endpoint
curl -sf http://127.0.0.1:8001/api/regime/current -u demo:demo > /dev/null && echo "✓ Regime endpoint OK"

# Verify dashboard loads
curl -sf http://127.0.0.1:8001/ -u demo:demo > /dev/null && echo "✓ Dashboard page loads"

# Verify logs page
curl -sf http://127.0.0.1:8001/logs -u demo:demo > /dev/null && echo "✓ Logs page loads"

# Verify quality page
curl -sf http://127.0.0.1:8001/quality -u demo:demo > /dev/null && echo "✓ Quality page loads"

echo ""
echo "=== All smoke tests passed ==="
