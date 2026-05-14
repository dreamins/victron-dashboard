#!/usr/bin/env bash
# verify_phase9_hardware.sh — Deploy updated stack and verify LiTime BMS connectivity.
# Requires: LiTime BMS powered on, production stack running.
# Run on Linux server: cd ~/victron-dashboard && bash verify_phase9_hardware.sh
set -euo pipefail

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); return 1; }

echo "=== Phase 9: hardware verification (LiTime BMS) ==="

# ── Step 1: Add litime_main to production sites.json if absent ────────────────
echo "Checking config/sites.json for litime_main..."
python3 - <<'EOF'
import json, sys

with open('config/sites.json') as f:
    config = json.load(f)

garage = next((s for s in config['sites'] if s['id'] == 'garage'), None)
if garage is None:
    garage = {
        "id": "garage",
        "label": "Garage Solar",
        "tz_offset_hours": 0,
        "bridge": "ble",
        "ui": {
            "show_loads": False,
            "battery_display": "bms",
            "mppt_count": 2
        },
        "devices": []
    }
    config['sites'].append(garage)
    print("  'garage' site created in config/sites.json")

if any(d['id'] == 'litime_main' for d in garage['devices']):
    print("  litime_main already present — no change needed")
    sys.exit(0)

garage['devices'].append({
    "id":    "litime_main",
    "label": "LiTime Battery",
    "type":  "litime_bms"
})
with open('config/sites.json', 'w') as f:
    json.dump(config, f, indent=2)
print("  litime_main added to config/sites.json")
EOF

# ── Step 2: Rebuild ble-bridge and solar-api images ───────────────────────────
echo "Building ble-bridge and solar-api images..."
docker compose build ble-bridge solar-api

# ── Step 3: Restart ble-bridge and solar-api (no-deps: leave everything else running)
echo "Restarting ble-bridge..."
docker compose up -d --no-deps ble-bridge

echo "Restarting solar-api (new /api/v1/battery endpoint)..."
docker compose up -d --no-deps solar-api

# ── Step 4: Wait for LiTime first connection ──────────────────────────────────
echo "Waiting 60s for LiTime BMS probe + connect + first data write..."
sleep 60

# ── Step 5: Verify battery data in InfluxDB ───────────────────────────────────
echo "Querying InfluxDB for battery measurement..."
REAL_TOKEN=$(grep -E '^INFLUXDB_TOKEN=' .env | cut -d= -f2-)
INFLUX_CONTAINER=$(docker ps --filter "name=victron-influxdb" --format "{{.Names}}" | head -1)

if [ -z "$INFLUX_CONTAINER" ]; then
    fail "InfluxDB container not running"
fi

COUNT=$(docker exec "$INFLUX_CONTAINER" influx query \
  --host http://localhost:8086 \
  --token "$REAL_TOKEN" \
  --org home \
  --raw \
  'from(bucket:"victron") |> range(start:-3m) |> filter(fn:(r)=>r._measurement=="battery" and r.site=="garage" and r.device=="litime_main") |> count() |> sum()' \
  2>/dev/null | grep -Eo '[0-9]+' | tail -1 || echo "0")

if [ "${COUNT:-0}" -gt 0 ]; then
    pass "InfluxDB has $COUNT battery field values from litime_main (site=garage)"
else
    echo "  ble-bridge recent logs:"
    BLE_CONTAINER=$(docker ps --filter "name=victron-ble-bridge" --format "{{.Names}}" | head -1)
    [ -n "$BLE_CONTAINER" ] && docker logs --tail=20 "$BLE_CONTAINER" 2>&1 || true
    fail "No battery data in InfluxDB within 3 minutes — BMS may not have connected"
fi

# ── Step 6: Verify API battery endpoint ───────────────────────────────────────
echo "Checking GET /api/v1/battery?site=garage&device=litime_main ..."
API_RESP=$(curl -sf "http://localhost:8080/api/v1/battery?site=garage&device=litime_main" || echo "{}")

SOC=$(echo "$API_RESP" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    bms = d.get('litime_main', {})
    fields = bms.get('fields', {})
    soc = fields.get('soc')
    if soc is not None:
        print(f'{soc:.0f}')
    else:
        sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null || echo "")

if [ -n "$SOC" ]; then
    VOLTAGE=$(echo "$API_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"{d['litime_main']['fields']['battery_voltage']:.2f}\")
" 2>/dev/null || echo "?")
    CURRENT=$(echo "$API_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"{d['litime_main']['fields']['battery_current']:.2f}\")
" 2>/dev/null || echo "?")
    pass "/api/v1/battery returns litime_main: SOC=${SOC}%  V=${VOLTAGE}V  A=${CURRENT}A"
else
    echo "  Response: $API_RESP"
    fail "/api/v1/battery did not return litime_main fields"
fi

# ── Step 7: Check ble-bridge log for reconnect pattern (sanity only) ──────────
BLE_CONTAINER=$(docker ps --filter "name=victron-ble-bridge" --format "{{.Names}}" | head -1)
if [ -n "$BLE_CONTAINER" ]; then
    echo "Recent ble-bridge log:"
    docker logs --tail=10 "$BLE_CONTAINER" 2>&1 | sed 's/^/  /'
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "================================"
echo "Results: $PASS passed, $FAIL failed"
echo "================================"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
