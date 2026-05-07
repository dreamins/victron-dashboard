#!/usr/bin/env bash
# Phase 5: verify dashboard is served, leave containers up for browser check.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; CYAN='\033[0;36m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }
run()  { "$@" 2>/dev/null; }

COMPOSE_TEST=(docker compose --project-name victron-test
    -f docker-compose.yml -f docker-compose.test.yml)

echo -e "\n${BOLD}Phase 5 test — Dashboard UI${NC}\n"

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

# Wait for API health (up to 60s)
for i in $(seq 1 30); do
    HEALTH=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
        python -c "import httpx; r=httpx.get('http://localhost:8080/health',timeout=3); print(r.json()['influx_ok'])" 2>/dev/null || true)
    [[ "$HEALTH" == "True" ]] && break
    sleep 2
done

# Seed 22h of data (victron_test has 24h retention — stay within bounds)
echo "Seeding test data..."
"${COMPOSE_TEST[@]}" exec -T solar-api python /app/seed_test_data.py --hours 22 \
    2>&1 | grep -v "^time=" || true

sleep 3

# ─── Automated checks ─────────────────────────────────────────────────────────

# 1. Dashboard root returns HTTP 200
HTTP_STATUS=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "import httpx; r=httpx.get('http://localhost:8080/',timeout=5); print(r.status_code)" 2>/dev/null || true)

if [[ "$HTTP_STATUS" == "200" ]]; then
    pass "GET / returns HTTP 200"
else
    fail "GET / returned ${HTTP_STATUS:-no response} (expected 200)"
fi

# 2. Response is HTML (not JSON error)
IS_HTML=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "import httpx; r=httpx.get('http://localhost:8080/',timeout=5); print('<!DOCTYPE html>' in r.text)" 2>/dev/null || true)

if [[ "$IS_HTML" == "True" ]]; then
    pass "index.html served with DOCTYPE"
else
    fail "response is not HTML"
fi

# 3. Chart.js bundled in the page
HAS_CHARTJS=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "import httpx; r=httpx.get('http://localhost:8080/',timeout=5); print('chart.js' in r.text.lower())" 2>/dev/null || true)

if [[ "$HAS_CHARTJS" == "True" ]]; then
    pass "index.html references Chart.js"
else
    fail "index.html missing Chart.js reference"
fi

# 4. SVG flow diagram present
HAS_SVG=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "import httpx; r=httpx.get('http://localhost:8080/',timeout=5); print('<svg' in r.text)" 2>/dev/null || true)

if [[ "$HAS_SVG" == "True" ]]; then
    pass "index.html contains SVG flow diagram"
else
    fail "index.html missing SVG element"
fi

# 5. /api/v1/current returns seeded devices
CURRENT_COUNT=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/current', timeout=10)
print(len(r.json()))
" 2>/dev/null || true)

if [[ "$CURRENT_COUNT" =~ ^[1-9] ]]; then
    pass "/api/v1/current returns $CURRENT_COUNT device(s)"
else
    fail "/api/v1/current returned no devices (got: '${CURRENT_COUNT}')"
fi

# 6. /api/v1/devices returns bridge + device list
DEVICE_COUNT=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/devices', timeout=10)
b = r.json()
print(len(b.get('devices', [])))
" 2>/dev/null || true)

if [[ "$DEVICE_COUNT" =~ ^[1-9] ]]; then
    pass "/api/v1/devices returns $DEVICE_COUNT device(s)"
else
    fail "/api/v1/devices returned no devices"
fi

# 7. /api/v1/history returns points for a known device
POINT_COUNT=$(run "${COMPOSE_TEST[@]}" exec -T solar-api \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/history',
    params={'device':'test_mppt1','field':'pv_power','start':'-6h','interval':'5m'},
    timeout=15)
print(len(r.json().get('points', [])))
" 2>/dev/null || true)

if [[ "$POINT_COUNT" =~ ^[1-9] ]]; then
    pass "/api/v1/history returns $POINT_COUNT points for test_mppt1"
else
    fail "/api/v1/history returned no points"
fi

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"

if [[ $FAIL -gt 0 ]]; then
    echo -e "${RED}Phase 5 automated checks FAIL${NC}"
    "${COMPOSE_TEST[@]}" stop solar-api influxdb 2>/dev/null || true
    exit 1
fi

echo -e "${GREEN}Phase 5 automated checks PASS${NC}"
echo ""
echo -e "${BOLD}${CYAN}Containers are running — open your browser now:${NC}"
echo -e "  ${CYAN}http://***REDACTED_SERVER_IP***:8081${NC}"
echo ""
echo -e "${BOLD}Manual UI acceptance checklist:${NC}"
echo "  [ ] Energy flow lines animate visibly"
echo "  [ ] Load bulb glows yellow when ON, grey outline when OFF"
echo "  [ ] Day/night indicator correct for current local time (Yerevan UTC+4)"
echo "  [ ] 2s polling: header shows 'updated Xs ago' counting up, resets on each poll"
echo "  [ ] BRIDGE_OFFLINE: stop solar-api → full-width red banner, all cards grey"
echo "  [ ] DEVICE_OFFLINE: one device card shows yellow 'Sensor silent'; others live"
echo "  [ ] 30d chart: fine-grained recent tail visible in DevTools Network response"
echo "  [ ] 7d yield chart: daily-reset pattern (rises by day, resets at local midnight)"
echo "  [ ] DevTools: 6h range chart requests interval=43s not interval=1s"
echo ""
echo -e "When done, clean up with:"
echo -e "  docker compose --project-name victron-test \\"
echo -e "    -f docker-compose.yml -f docker-compose.test.yml \\"
echo -e "    stop solar-api influxdb"
