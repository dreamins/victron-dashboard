#!/usr/bin/env bash
# test_phase8.sh — Phase 8: Linux BLE bridge isolated test
# Verifies: decode logic, device map loading, fixture replay → InfluxDB (site=garage tag).
# No real BLE hardware required. Safe to run alongside production stack.
set -euo pipefail

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.test.yml"
PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== Phase 8: Linux BLE bridge ==="

# ── Build ble-bridge image ────────────────────────────────────────────────────
echo "Building ble-bridge image..."
$COMPOSE build --no-cache ble-bridge

# ── Unit tests (no InfluxDB needed) ──────────────────────────────────────────
echo "Running unit tests..."
$COMPOSE run --rm --no-deps \
  -e INFLUX_URL=http://dummy \
  -e INFLUX_TOKEN=dummy \
  -e INFLUX_BUCKET=dummy \
  ble-bridge python -m pytest /app/tests/test_decoder.py -v
pass "unit tests (decode_advertisement, load_device_map — 14 tests)"

# ── Integration: fixture replay → InfluxDB ───────────────────────────────────
# Requires InfluxDB to already be running (reuses the production container).
# Uses victron_test bucket with the test token from docker-compose.test.yml.
echo "Checking InfluxDB reachability..."
INFLUX_CONTAINER=$(docker ps --filter "name=victron-influxdb" --format "{{.Names}}" | head -1)
if [ -z "$INFLUX_CONTAINER" ]; then
    echo "WARNING: InfluxDB container not running — skipping integration tests"
    echo "         Start the stack first: docker compose up -d influxdb"
else
    echo "InfluxDB found: $INFLUX_CONTAINER"
    echo "Running integration tests (fixture replay → InfluxDB)..."
    $COMPOSE run --rm --no-deps \
      -e INFLUX_URL=http://influxdb:8086 \
      -e INFLUX_TOKEN=test_influx_token_aabbccdd1122 \
      -e INFLUX_BUCKET=victron_test \
      --network victron_default \
      ble-bridge python -m pytest /app/tests/test_fixture_replay.py -v
    pass "integration tests (fixture replay, site=garage tag, both devices, pv_power field)"
fi

# ── Cleanup: only the ble-bridge test container ───────────────────────────────
docker container prune -f --filter "label=com.docker.compose.project=victron" >/dev/null 2>&1 || true

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
