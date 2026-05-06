#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }

run() { "$@" 2>/dev/null; }

echo -e "\n${BOLD}Phase 3 test — BLE decoder${NC}\n"

# ─── Setup ────────────────────────────────────────────────────────────────────

docker compose stop ble-decoder mosquitto influxdb 2>/dev/null || true
sleep 2

MQTT_BIND_IP=127.0.0.1 docker compose -f docker-compose.yml -f docker-compose.test.yml \
    up -d --build mosquitto influxdb ble-decoder 2>&1 | grep -v "^time=" || true

# Wait for mosquitto
for i in $(seq 1 20); do
    run docker compose exec -T mosquitto \
        mosquitto_pub -h localhost -u decoder -P test_decoder_pass \
        -t victron/ping -m x && break
    sleep 2
done

# Wait for influxdb
for i in $(seq 1 40); do
    run docker compose exec -T influxdb influx ping && break
    sleep 3
done

# Wait for decoder to connect to MQTT
for i in $(seq 1 20); do
    docker compose logs ble-decoder 2>/dev/null | grep -q "MQTT connected" && break
    sleep 2
done

# ─── Replay fixture through decoder ───────────────────────────────────────────

# replay.py is inside the ble-decoder image at /app/replay.py
# Run it inside the container so it can reach mosquitto via Docker network
run docker compose -f docker-compose.yml -f docker-compose.test.yml \
    exec -T ble-decoder python /app/replay.py \
    --broker mosquitto --port 1883 \
    --username esp32-bridge --password test_esp32_pass \
    --fixture /app/fixtures/victron_sample.jsonl \
    --rate 3 || true

sleep 5  # let writer thread flush all buffered points

# ─── Tests ────────────────────────────────────────────────────────────────────

# 1. Decoder is still running
if docker compose ps ble-decoder 2>/dev/null | grep -qiE "up|running"; then
    pass "decoder is running"
else
    fail "decoder is not running (crashed?)"
fi

# 2. No "no key configured" errors — all fixture MACs are in devices_fixture.json
if docker compose logs ble-decoder 2>/dev/null | grep -q "no key configured"; then
    fail "decoder logged 'no key configured' (fixture devices.json mismatch?)"
else
    pass "no 'no key configured' errors"
fi

# 3–5. Each test device has at least one data point in victron_test
for device in test_mppt1 test_mppt2 test_battery_sense; do
    RESULT=$(run docker compose exec -T influxdb influx query --org home \
        "from(bucket:\"victron_test\") |> range(start:-5m) |> filter(fn:(r)=>r.device==\"${device}\") |> count()" || true)
    if [[ "$RESULT" == *"$device"* ]]; then
        pass "device $device has data in victron_test"
    else
        fail "device $device has no data in victron_test"
    fi
done

# 6. Production bucket is empty — test data must not bleed into victron
PROD=$(run docker compose exec -T influxdb influx query --org home \
    'from(bucket:"victron") |> range(start:-5m) |> count()' || true)
if echo "$PROD" | grep -qE "test_mppt|test_battery"; then
    fail "test data leaked into production 'victron' bucket"
else
    pass "production bucket isolated"
fi

# ─── Retry resilience test ────────────────────────────────────────────────────

# Stop influxdb, run replay (writes fail → decoder buffers), restart influxdb, verify flush

docker compose stop influxdb 2>/dev/null
sleep 2

# Run one pass of replay while InfluxDB is down
run docker compose -f docker-compose.yml -f docker-compose.test.yml \
    exec -T ble-decoder python /app/replay.py \
    --broker mosquitto --port 1883 \
    --username esp32-bridge --password test_esp32_pass \
    --fixture /app/fixtures/victron_sample.jsonl \
    --rate 3 || true

sleep 8  # let writer attempt writes and log retries (influxdb-client timeout is 3s + 1s retry gap)

# 7. Decoder logged retry attempts
if docker compose logs ble-decoder 2>/dev/null | grep -qE "retry in|influx write.*failed"; then
    pass "decoder logged retry attempts during outage"
else
    fail "decoder did not log retry attempts (buffer/retry may not be working)"
fi

# Bring influxdb back
docker compose start influxdb 2>/dev/null
for i in $(seq 1 30); do
    run docker compose exec -T influxdb influx ping && break
    sleep 3
done
sleep 8  # let decoder drain the buffer

# 8. Decoder is still alive after outage
if docker compose ps ble-decoder 2>/dev/null | grep -qiE "up|running"; then
    pass "decoder survived influxdb outage"
else
    fail "decoder crashed during influxdb outage"
fi

# ─── Cleanup ──────────────────────────────────────────────────────────────────

run docker compose exec -T influxdb influx delete \
    --org home --bucket victron_test \
    --start 1970-01-01T00:00:00Z \
    --stop "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --predicate '_measurement="solar"' || true

docker compose stop ble-decoder mosquitto influxdb 2>/dev/null || true

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}Phase 3 PASS${NC}"
else
    echo -e "${RED}Phase 3 FAIL${NC}"
    exit 1
fi
