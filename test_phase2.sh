#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }

run() { "$@" 2>/dev/null; }

# Separate project name keeps test containers isolated from the production stack.
# Production services (mosquitto on ***REDACTED_SERVER_IP***:1883, influxdb) are never touched.
COMPOSE_TEST=(docker compose --project-name victron-test
    -f docker-compose.yml -f docker-compose.test.yml)

echo -e "\n${BOLD}Phase 2 test — server infrastructure${NC}\n"

# ─── Setup ───────────────────────────────────────────────────────────────────

# Stop any leftover containers from a previous test run (not production)
"${COMPOSE_TEST[@]}" stop mosquitto influxdb 2>/dev/null || true
sleep 2

# MQTT_BIND_IP=127.0.0.1 — test mosquitto binds loopback, won't conflict with
# production mosquitto on ***REDACTED_SERVER_IP***:1883
MQTT_BIND_IP=127.0.0.1 "${COMPOSE_TEST[@]}" up -d mosquitto influxdb \
    2>&1 | grep -v "^time=" || true

# Wait for mosquitto (up to 40s)
for i in $(seq 1 20); do
    run "${COMPOSE_TEST[@]}" exec -T mosquitto \
        mosquitto_pub -h localhost -u decoder -P test_decoder_pass \
        -t victron/ping -m x && break
    sleep 2
done

# Wait for influxdb (up to 120s — first run executes init scripts)
for i in $(seq 1 40); do
    run "${COMPOSE_TEST[@]}" exec -T influxdb influx ping && break
    sleep 3
done

# ─── Tests ───────────────────────────────────────────────────────────────────

# 1. InfluxDB ping
if run "${COMPOSE_TEST[@]}" exec -T influxdb influx ping; then
    pass "influxdb ping"
else
    fail "influxdb ping"
fi

# 2. Anonymous MQTT rejected
if run "${COMPOSE_TEST[@]}" exec -T mosquitto mosquitto_pub -h localhost -t test -m x; then
    fail "anonymous MQTT should be rejected"
else
    pass "anonymous MQTT rejected"
fi

# 3. Authenticated MQTT round-trip
PUB_RC=0
run "${COMPOSE_TEST[@]}" exec -T mosquitto mosquitto_pub \
    -h localhost -u decoder -P test_decoder_pass \
    -t victron/selftest -m '{"ok":1}' -r || PUB_RC=$?

MSG=""
if [[ $PUB_RC -eq 0 ]]; then
    MSG=$(run "${COMPOSE_TEST[@]}" exec -T mosquitto mosquitto_sub \
        -h localhost -u decoder -P test_decoder_pass \
        -t victron/selftest -C 1 -W 5 || true)
fi

if [[ "$MSG" == *'"ok":1'* ]]; then
    pass "authenticated MQTT round-trip"
else
    fail "authenticated MQTT round-trip (pub_rc=$PUB_RC, got: '$MSG')"
fi

# clean retained message
run "${COMPOSE_TEST[@]}" exec -T mosquitto mosquitto_pub \
    -h localhost -u decoder -P test_decoder_pass \
    -t victron/selftest -m '' -r || true

# 4. Write to victron_test bucket
if run "${COMPOSE_TEST[@]}" exec -T influxdb influx write \
    --org home --bucket victron_test \
    'solar,device=test_mppt1 pv_power=42.0'; then
    pass "write to victron_test"
else
    fail "write to victron_test"
fi

# 5. Query victron_test bucket
sleep 1
RESULT=$(run "${COMPOSE_TEST[@]}" exec -T influxdb influx query --org home \
    'from(bucket:"victron_test") |> range(start:-1m) |> filter(fn:(r)=>r.device=="test_mppt1")' || true)

if [[ "$RESULT" == *"test_mppt1"* ]]; then
    pass "query victron_test"
else
    fail "query victron_test (got: '$RESULT')"
fi

# 6. All 4 buckets present
BUCKETS=$(run "${COMPOSE_TEST[@]}" exec -T influxdb influx bucket list --org home || true)

for bucket in victron victron_medium victron_hourly victron_test; do
    if [[ "$BUCKETS" == *"$bucket"* ]]; then
        pass "bucket $bucket exists"
    else
        fail "bucket $bucket missing"
    fi
done

# 7. All 4 tasks present
TASKS=$(run "${COMPOSE_TEST[@]}" exec -T influxdb influx task list --org home || true)

for task in downsample_instant_5m downsample_yield_5m downsample_instant_1h downsample_yield_1h; do
    if [[ "$TASKS" == *"$task"* ]]; then
        pass "task $task exists"
    else
        fail "task $task missing"
    fi
done

# ─── Cleanup ─────────────────────────────────────────────────────────────────

run "${COMPOSE_TEST[@]}" exec -T influxdb influx delete \
    --org home --bucket victron_test \
    --start 1970-01-01T00:00:00Z \
    --stop "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --predicate '_measurement="solar"' || true

"${COMPOSE_TEST[@]}" stop mosquitto influxdb 2>/dev/null || true

# ─── Result ──────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}Phase 2 PASS${NC}"
else
    echo -e "${RED}Phase 2 FAIL${NC}"
    exit 1
fi
