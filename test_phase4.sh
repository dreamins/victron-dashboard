#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }

run() { "$@" 2>/dev/null; }

COMPOSE_TEST=(docker compose --project-name victron-test
    -f docker-compose.yml -f docker-compose.test.yml)

echo -e "\n${BOLD}Phase 4 test — API service${NC}\n"

# ─── Setup ────────────────────────────────────────────────────────────────────

"${COMPOSE_TEST[@]}" stop solar-api influxdb 2>/dev/null || true
sleep 2

MQTT_BIND_IP=127.0.0.1 "${COMPOSE_TEST[@]}" up -d --build influxdb solar-api \
    2>&1 | grep -v "^time=" || true

# Wait for InfluxDB (up to 120s — init scripts run on first start)
for i in $(seq 1 40); do
    run "${COMPOSE_TEST[@]}" exec -T influxdb influx ping && break
    sleep 3
done

# Wait for API health endpoint (up to 60s)
for i in $(seq 1 30); do
    HEALTH=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
        python -c "import httpx; r=httpx.get('http://localhost:8080/health',timeout=3); print(r.json()['influx_ok'])" 2>/dev/null || true)
    [[ "$HEALTH" == "True" ]] && break
    sleep 2
done

# Seed test data into victron_test (24h retention — use 22h to stay within bounds)
echo "Seeding test data..."
"${COMPOSE_TEST[@]}" exec -T solar-api python /app/seed_test_data.py --hours 22 \
    2>&1 | grep -v "^time=" || true

sleep 3  # let InfluxDB index the writes

# ─── Pytest ───────────────────────────────────────────────────────────────────

echo ""
PYTEST_OUTPUT=$("${COMPOSE_TEST[@]}" exec -T solar-api \
    python -m pytest /app/tests/ -v --tb=short 2>&1 | grep -v "^time=" || true)
echo "$PYTEST_OUTPUT"

# Count pass/fail from pytest output
PYTEST_PASSED=$(echo "$PYTEST_OUTPUT" | grep -c " PASSED" || true)
PYTEST_FAILED=$(echo "$PYTEST_OUTPUT" | grep -c " FAILED" || true)
PYTEST_ERRORS=$(echo "$PYTEST_OUTPUT" | grep -c " ERROR" || true)

PASS=$((PASS + PYTEST_PASSED))
FAIL=$((FAIL + PYTEST_FAILED + PYTEST_ERRORS))

# ─── Curl smoke tests ─────────────────────────────────────────────────────────

# Verify bucket stitching label in a 30d history query
HISTORY=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx, json
r = httpx.get('http://localhost:8080/api/v1/history',
    params={'device':'test_mppt1','field':'pv_power','start':'-30d','interval':'5m'},
    timeout=30)
print(json.dumps(r.json().get('buckets_used',[])))
" 2>/dev/null || true)

if [[ "$HISTORY" == *'"victron_test"'* ]]; then
    pass "bucket stitching — buckets_used contains victron_test"
else
    fail "bucket stitching — unexpected buckets_used: $HISTORY"
fi

# Verify /daily returns data
DAILY=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/daily', params={'days':3}, timeout=10)
days = r.json().get('days', [])
print(len(days))
" 2>/dev/null || true)

if [[ "$DAILY" =~ ^[1-9] ]]; then
    pass "daily endpoint returns $DAILY day(s)"
else
    fail "daily endpoint returned no days (got: '$DAILY')"
fi

# ─── Cleanup ──────────────────────────────────────────────────────────────────

run "${COMPOSE_TEST[@]}" exec -T influxdb influx delete \
    --org home --bucket victron_test \
    --start 1970-01-01T00:00:00Z \
    --stop "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --predicate '_measurement="solar"' || true

"${COMPOSE_TEST[@]}" stop solar-api influxdb 2>/dev/null || true

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}Phase 4 PASS${NC}"
else
    echo -e "${RED}Phase 4 FAIL${NC}"
    exit 1
fi
