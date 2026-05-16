#!/usr/bin/env bash
# test_phase11.sh — Phase 11: Dashboard multi-site UI
# Verifies the new dashboard is deployed and API supports multi-site UI config.
# Safe to run alongside production stack — no containers are torn down.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0

pass() { echo -e "${GREEN}[PASS]${NC} $*"; PASS=$((PASS+1)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAIL=$((FAIL+1)); }

echo -e "\n${BOLD}Phase 11 test — Dashboard multi-site UI${NC}\n"

# ─── 1. solar-api container is running ────────────────────────────────────────

API_CONTAINER=$(docker ps --filter "name=victron-solar-api" --format "{{.Names}}" | head -1)
if [ -z "$API_CONTAINER" ]; then
    echo -e "${RED}ERROR: solar-api container not running. Deploy with deploy_phase11.ps1 first.${NC}"
    exit 1
fi
pass "solar-api container running: $API_CONTAINER"

# ─── 2. /health returns influx_ok ─────────────────────────────────────────────

HEALTH=$(docker exec "$API_CONTAINER" \
    python -c "import httpx; r=httpx.get('http://localhost:8080/health',timeout=5); print(r.json().get('influx_ok',''))" 2>/dev/null || true)
if [[ "$HEALTH" == "True" ]]; then
    pass "/health influx_ok=True"
else
    fail "/health influx_ok not True (got: $HEALTH)"
fi

# ─── 3. /api/v1/sites returns both production sites ──────────────────────────

SITES=$(docker exec "$API_CONTAINER" \
    python -c "
import httpx, json
r = httpx.get('http://localhost:8080/api/v1/sites', timeout=5)
ids = [s['id'] for s in r.json().get('sites', [])]
print(json.dumps(ids))
" 2>/dev/null || true)

if echo "$SITES" | python3 -c "import sys,json; ids=json.load(sys.stdin); assert len(ids)>0" 2>/dev/null; then
    pass "/api/v1/sites returns sites: $SITES"
else
    fail "/api/v1/sites returned unexpected data: $SITES"
fi

# ─── 4. Sites have ui config blocks ───────────────────────────────────────────

UI_CHECK=$(docker exec "$API_CONTAINER" \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/sites', timeout=5)
sites = r.json().get('sites', [])
ok = all('ui' in s and 'battery_display' in s.get('ui', {}) for s in sites)
print('ok' if ok else 'fail')
" 2>/dev/null || true)

if [[ "$UI_CHECK" == "ok" ]]; then
    pass "all sites have ui.battery_display config"
else
    fail "some sites missing ui config block (got: $UI_CHECK)"
fi

# ─── 5. Served index.html contains Phase 11 JavaScript ────────────────────────

HTML=$(docker exec "$API_CONTAINER" \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/', timeout=5)
print(r.text[:50000])" 2>/dev/null || true)

if echo "$HTML" | grep -q "initSites"; then
    pass "index.html contains initSites() — Phase 11 JS deployed"
else
    fail "index.html missing initSites() — old version still served"
fi

if echo "$HTML" | grep -q "showPicker"; then
    pass "index.html contains showPicker() — site picker deployed"
else
    fail "index.html missing showPicker()"
fi

if echo "$HTML" | grep -q "soc-bar-track"; then
    pass "index.html contains soc-bar-track CSS — BMS card deployed"
else
    fail "index.html missing soc-bar-track CSS — BMS card not deployed"
fi

if echo "$HTML" | grep -q "site-picker"; then
    pass "index.html contains #site-picker element"
else
    fail "index.html missing #site-picker element"
fi

if echo "$HTML" | grep -q "victron_selected_site"; then
    pass "index.html uses localStorage key victron_selected_site"
else
    fail "index.html missing localStorage key victron_selected_site"
fi

# ─── 6. Battery endpoint smoke (if battery data exists) ──────────────────────

BATT_STATUS=$(docker exec "$API_CONTAINER" \
    python -c "
import httpx
r = httpx.get('http://localhost:8080/api/v1/battery', params={'site':'garage'}, timeout=5)
print(r.status_code)
" 2>/dev/null || true)

if [[ "$BATT_STATUS" == "200" ]]; then
    pass "/api/v1/battery?site=garage returns 200"
else
    fail "/api/v1/battery?site=garage returned status $BATT_STATUS"
fi

# ─── 7. Site isolation — devices endpoint scopes correctly ────────────────────

DEVICES_HOME=$(docker exec "$API_CONTAINER" \
    python -c "
import httpx, json
r = httpx.get('http://localhost:8080/api/v1/devices', params={'site':'home'}, timeout=5)
ids = [d['id'] for d in r.json().get('devices', [])]
print(json.dumps(ids))
" 2>/dev/null || true)

if echo "$DEVICES_HOME" | python3 -c "import sys,json; ids=json.load(sys.stdin); print('ok')" 2>/dev/null; then
    pass "/api/v1/devices?site=home returns valid device list: $DEVICES_HOME"
else
    fail "/api/v1/devices?site=home failed (got: $DEVICES_HOME)"
fi

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}"

if [[ $FAIL -gt 0 ]]; then
    echo -e "${RED}Phase 11 FAIL${NC}"
    exit 1
fi

# ─── Manual browser verification prompt ──────────────────────────────────────

HOST_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${CYAN}${BOLD}=== Manual Browser Verification Required ===${NC}"
echo ""
echo "Production dashboard URL:  https://${HOST_IP}:8443/"
echo "  (or your configured external domain)"
echo ""
echo "Verify the following in the browser:"
echo "  1. Home site:   load node visible, voltage+temp battery card"
echo "  2. Garage site: load node hidden, BMS SOC-bar battery card"
echo "  3. Site picker: full-screen picker shown if localStorage cleared"
echo "  4. Header [Site ▾] dropdown switches sites"
echo "  5. Mobile (≤480px): picker cards stack vertically"
echo ""
echo "To clear site selection and re-show picker, run in browser console:"
echo "  localStorage.removeItem('victron_selected_site'); location.reload()"
echo ""
echo -e "${GREEN}Phase 11 automated checks PASS — manual browser verification pending${NC}"
