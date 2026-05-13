#!/usr/bin/env bash
# test_phase8.sh — Phase 8: Linux BLE bridge isolated test
# Verifies: fixture mode writes, site=garage tag, both devices written, correct fields.
# No real BLE hardware required.
set -euo pipefail

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.test.yml"
PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

echo "=== Phase 8: Linux BLE bridge ==="

# ── Build ble-bridge image ────────────────────────────────────────────────────
echo "Building ble-bridge image..."
$COMPOSE build ble-bridge

# ── Unit tests (no InfluxDB needed) ──────────────────────────────────────────
echo "Running unit tests..."
$COMPOSE run --rm --no-deps \
  -e INFLUX_URL=http://dummy \
  -e INFLUX_TOKEN=dummy \
  -e INFLUX_BUCKET=dummy \
  ble-bridge python -m pytest /app/tests/test_decoder.py -v
pass "unit tests (drivers/victron.py + load_device_map)"

# ── Start test stack ──────────────────────────────────────────────────────────
echo "Starting InfluxDB..."
$COMPOSE up -d influxdb
echo "Waiting for InfluxDB to be ready..."
for i in $(seq 1 30); do
    if $COMPOSE exec -T influxdb influx ping --host http://localhost:8086 >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

# ── Fixture replay + integration tests ───────────────────────────────────────
echo "Running integration tests (fixture replay → InfluxDB)..."
$COMPOSE run --rm \
  -e INFLUX_URL=http://influxdb:8086 \
  -e INFLUX_TOKEN=test_influx_token_aabbccdd1122 \
  -e INFLUX_BUCKET=victron_test \
  ble-bridge python -m pytest /app/tests/test_fixture_replay.py -v
pass "integration tests (fixture replay, site=garage tag, both devices, pv_power field)"

# ── Cleanup ───────────────────────────────────────────────────────────────────
$COMPOSE down --remove-orphans >/dev/null 2>&1 || true

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
