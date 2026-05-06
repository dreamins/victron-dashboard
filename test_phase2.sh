#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}[PASS]${NC} $*"; ((PASS++)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; ((FAIL++)); }

# ─── Setup ───────────────────────────────────────────────────────────────────

echo -e "\n${BOLD}Phase 2 test — server infrastructure${NC}\n"

docker compose -f docker-compose.yml -f docker-compose.test.yml up -d mosquitto influxdb

# Wait for mosquitto
for i in $(seq 1 20); do
    docker compose exec -T mosquitto mosquitto_pub \
        -h localhost -u decoder -P test_decoder_pass \
        -t victron/ping -m x &>/dev/null && break
    sleep 2
done

# Wait for influxdb
for i in $(seq 1 40); do
    docker compose exec -T influxdb influx ping &>/dev/null && break
    sleep 3
done

# ─── Tests ───────────────────────────────────────────────────────────────────

# 1. InfluxDB ping
docker compose exec -T influxdb influx ping &>/dev/null \
    && pass "influxdb ping" \
    || fail "influxdb ping"

# 2. Anonymous MQTT rejected (exit non-zero = rejected)
! docker compose exec -T mosquitto mosquitto_pub -h localhost -t test -m x &>/dev/null \
    && pass "anonymous MQTT rejected" \
    || fail "anonymous MQTT rejected"

# 3. Authenticated MQTT round-trip
docker compose exec -T mosquitto mosquitto_pub \
    -h localhost -u decoder -P test_decoder_pass \
    -t victron/selftest -m '{"ok":1}' -r &>/dev/null

MSG=$(docker compose exec -T mosquitto mosquitto_sub \
    -h localhost -u decoder -P test_decoder_pass \
    -t victron/selftest -C 1 -W 5 2>/dev/null || true)

[[ "$MSG" == *'"ok":1'* ]] \
    && pass "authenticated MQTT round-trip" \
    || fail "authenticated MQTT round-trip (got: $MSG)"

# clean retained message
docker compose exec -T mosquitto mosquitto_pub \
    -h localhost -u decoder -P test_decoder_pass \
    -t victron/selftest -m '' -r &>/dev/null || true

# 4. Write to victron_test bucket
docker compose exec -T influxdb influx write \
    --org home --bucket victron_test \
    'solar,device=test_mppt1 pv_power=42.0' &>/dev/null \
    && pass "write to victron_test" \
    || fail "write to victron_test"

# 5. Query victron_test bucket
sleep 1
RESULT=$(docker compose exec -T influxdb influx query --org home \
    'from(bucket:"victron_test") |> range(start:-1m) |> filter(fn:(r)=>r.device=="test_mppt1")' \
    2>/dev/null || true)

[[ "$RESULT" == *"test_mppt1"* ]] \
    && pass "query victron_test" \
    || fail "query victron_test"

# 6. All 4 buckets present
BUCKETS=$(docker compose exec -T influxdb influx bucket list --org home 2>/dev/null || true)

for bucket in victron victron_medium victron_hourly victron_test; do
    [[ "$BUCKETS" == *"$bucket"* ]] \
        && pass "bucket $bucket exists" \
        || fail "bucket $bucket missing"
done

# 7. All 4 tasks present
TASKS=$(docker compose exec -T influxdb influx task list --org home 2>/dev/null || true)

for task in downsample_instant_5m downsample_yield_5m downsample_instant_1h downsample_yield_1h; do
    [[ "$TASKS" == *"$task"* ]] \
        && pass "task $task exists" \
        || fail "task $task missing"
done

# ─── Cleanup ─────────────────────────────────────────────────────────────────

docker compose exec -T influxdb influx delete \
    --org home --bucket victron_test \
    --start 1970-01-01T00:00:00Z \
    --stop "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --predicate '_measurement="solar"' &>/dev/null || true

docker compose stop mosquitto influxdb &>/dev/null

# ─── Result ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
[[ $FAIL -eq 0 ]] && echo -e "${GREEN}Phase 2 PASS${NC}" || { echo -e "${RED}Phase 2 FAIL${NC}"; exit 1; }
