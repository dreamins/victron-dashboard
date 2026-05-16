#!/usr/bin/env bash
# test_phase10.sh — Phase 10: Multi-site API
# Runs all 49 API tests against an isolated test InfluxDB seeded with both sites.
# Safe to run alongside the production stack (uses --project-name victron-test).
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

echo -e "\n${BOLD}Phase 10 test — Multi-site API${NC}\n"

# ─── Setup ────────────────────────────────────────────────────────────────────

"${COMPOSE_TEST[@]}" stop solar-api influxdb 2>/dev/null || true
sleep 2

MQTT_BIND_IP=127.0.0.1 "${COMPOSE_TEST[@]}" up -d --build influxdb solar-api \
    2>&1 | grep -v "^time=" || true

# Wait for InfluxDB (up to 120s — init scripts run on first start)
echo "Waiting for InfluxDB..."
for i in $(seq 1 40); do
    run "${COMPOSE_TEST[@]}" exec -T influxdb influx ping && break
    sleep 3
done

# Wait for API health endpoint (up to 60s)
echo "Waiting for solar-api..."
for i in $(seq 1 30); do
    HEALTH=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
        python -c "import httpx; r=httpx.get('http://localhost:8080/health',timeout=3); print(r.json()['influx_ok'])" 2>/dev/null || true)
    [[ "$HEALTH" == "True" ]] && break
    sleep 2
done

# Seed test data for both sites into victron_test bucket (22h stays within 24h retention)
echo "Seeding test data (test site + test_garage site)..."
"${COMPOSE_TEST[@]}" exec -T solar-api python /app/seed_test_data.py --hours 22 \
    2>&1 | grep -v "^time=" || true

sleep 3  # let InfluxDB index the writes

# ─── Pytest ───────────────────────────────────────────────────────────────────

echo ""
PYTEST_OUTPUT=$("${COMPOSE_TEST[@]}" exec -T solar-api \
    python -m pytest /app/tests/test_api.py -v --tb=short 2>&1 | grep -v "^time=" || true)
echo "$PYTEST_OUTPUT"

PYTEST_PASSED=$(echo "$PYTEST_OUTPUT" | grep -c " PASSED" || true)
PYTEST_FAILED=$(echo "$PYTEST_OUTPUT" | grep -c " FAILED" || true)
PYTEST_ERRORS=$(echo "$PYTEST_OUTPUT" | grep -c " ERROR" || true)

PASS=$((PASS + PYTEST_PASSED))
FAIL=$((FAIL + PYTEST_FAILED + PYTEST_ERRORS))

# ─── Smoke: both sites in /api/v1/sites ──────────────────────────────────────

SITES=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx, json
r = httpx.get('http://localhost:8080/api/v1/sites', timeout=10)
ids = [s['id'] for s in r.json().get('sites', [])]
print(json.dumps(ids))
" 2>/dev/null || true)

if echo "$SITES" | grep -q '"test"' && echo "$SITES" | grep -q '"test_garage"'; then
    pass "sites endpoint returns both test and test_garage"
else
    fail "sites endpoint missing expected sites (got: $SITES)"
fi

# ─── Smoke: site isolation — test does not bleed into test_garage ─────────────

ISOLATION=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/devices', params={'site':'test'}, timeout=10)
ids = [d['id'] for d in r.json().get('devices', [])]
print('ok' if 'test_garage_mppt1' not in ids else 'fail')
" 2>/dev/null || true)

if [[ "$ISOLATION" == "ok" ]]; then
    pass "site isolation — test site excludes test_garage devices"
else
    fail "site isolation — test_garage_mppt1 appeared in site=test response"
fi

# ─── Smoke: garage BMS visible under site=test_garage ─────────────────────────

GARAGE_BMS=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/battery', params={'site':'test_garage'}, timeout=10)
body = r.json()
soc = body.get('test_garage_bms', {}).get('fields', {}).get('soc')
print(f'{soc:.0f}' if soc is not None else 'missing')
" 2>/dev/null || true)

if [[ "$GARAGE_BMS" =~ ^[0-9]+$ ]]; then
    pass "garage BMS battery endpoint returns SOC=${GARAGE_BMS}% for test_garage_bms"
else
    fail "garage BMS not found under site=test_garage (got: $GARAGE_BMS)"
fi

# ─── Cleanup ──────────────────────────────────────────────────────────────────

for meas in solar battery; do
    run "${COMPOSE_TEST[@]}" exec -T influxdb influx delete \
        --org home --bucket victron_test \
        --start 1970-01-01T00:00:00Z \
        --stop "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --predicate "_measurement=\"${meas}\"" || true
done

"${COMPOSE_TEST[@]}" stop solar-api influxdb 2>/dev/null || true

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}Phase 10 PASS${NC}"
else
    echo -e "${RED}Phase 10 FAIL${NC}"
    exit 1
fi
