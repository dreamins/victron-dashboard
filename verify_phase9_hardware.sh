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

# ── Step 0: Detect best Bluetooth adapter and persist to .env ─────────────────
echo "Detecting Bluetooth adapters..."

# Install firmware for common BT5 dongles (RTL8761B = TP-Link UB500/ASUS BT500,
# Broadcom = various others).  No-op if already installed.
if ! dpkg -l firmware-realtek 2>/dev/null | grep -q '^ii' || \
   ! dpkg -l firmware-brcm80211 2>/dev/null | grep -q '^ii'; then
    echo "  Installing BT5 dongle firmware packages..."
    sudo -n apt-get install -y firmware-realtek firmware-brcm80211 2>/dev/null || \
        echo "  WARNING: firmware install requires sudo — BT5 dongle may not initialize"
fi

# Restart bluetoothd so it picks up newly available firmware / USB devices.
if sudo -n systemctl restart bluetooth 2>/dev/null; then
    echo "  Bluetooth service restarted"
    sleep 3
fi

# Unblock and bring up every present adapter.
rfkill unblock bluetooth 2>/dev/null || true
for hci_path in /sys/class/bluetooth/hci*/; do
    hci_name=$(basename "$hci_path")
    hciconfig "$hci_name" up 2>/dev/null || true
done

BLE_ADAPTER=$(python3 - <<'PYEOF'
import subprocess, sys, os

try:
    out = subprocess.check_output(["hciconfig", "-a"], text=True, stderr=subprocess.DEVNULL)
except Exception:
    sys.exit(0)

# Parse all adapters: {name: {manufacturer, hci_ver}}
adapters = {}
current = None
for line in out.splitlines():
    # New adapter block starts without leading whitespace and matches "hciN:"
    if line and not line[0].isspace():
        parts = line.split(":")
        if parts[0].startswith("hci"):
            current = parts[0]
            adapters.setdefault(current, {"manufacturer": "", "hci_ver": 0.0})
    elif current:
        s = line.strip()
        if "Manufacturer:" in s:
            adapters[current]["manufacturer"] = s
        if "HCI Version:" in s:
            try:
                ver = float(s.split("HCI Version:")[1].strip().split()[0])
                adapters[current]["hci_ver"] = ver
            except Exception:
                pass

print(f"  All adapters: {list(adapters.keys())}", file=sys.stderr)
for name, info in adapters.items():
    print(f"    {name}: ver={info['hci_ver']}  {info['manufacturer']}", file=sys.stderr)

# Priority 1: non-Atheros adapter (old dongle is Atheros BT4)
for name, info in adapters.items():
    if "Atheros" not in info["manufacturer"]:
        print(name)
        sys.exit(0)

# Priority 2: highest HCI version adapter
if adapters:
    best = max(adapters.items(), key=lambda x: x[1]["hci_ver"])
    print(best[0])
PYEOF
)

if [ -n "$BLE_ADAPTER" ]; then
    echo "  Selected adapter: $BLE_ADAPTER"
    # Power on via BlueZ so the container can find it via D-Bus.
    (echo "select $BLE_ADAPTER"; sleep 0.5; echo "power on"; sleep 0.5; echo "quit") \
        | bluetoothctl 2>&1 | grep -v "^$" | sed 's/^/    /' || true
    sleep 2  # let BlueZ finish registering the adapter
    sed -i '/^BLE_ADAPTER=/d' .env
    echo "BLE_ADAPTER=$BLE_ADAPTER" >> .env
    export BLE_ADAPTER="$BLE_ADAPTER"
else
    echo "  No adapter detected — using system default"
    sed -i '/^BLE_ADAPTER=/d' .env
    export BLE_ADAPTER=""
fi

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

# ── Step 3: Identify BMS devices that still need a MAC assigned ───────────────
NEEDS_ID=$(python3 - <<'EOF'
import json
with open('config/sites.json') as f:
    config = json.load(f)
needs = [
    d['id']
    for s in config.get('sites', [])
    for d in s.get('devices', [])
    if d.get('type') == 'litime_bms' and not d.get('mac')
]
print('\n'.join(needs))
EOF
)

if [ -n "$NEEDS_ID" ]; then
    echo "BMS device(s) need MAC identification: $NEEDS_ID"
    echo "Running identification wizard (BMS must be powered on)..."
    bash identify_bms.sh
else
    echo "All BMS devices already have MAC addresses — skipping identification."
    echo "Restarting ble-bridge..."
    docker compose up -d --no-deps ble-bridge
fi

echo "Restarting solar-api (new /api/v1/battery endpoint)..."
docker compose up -d --no-deps solar-api

# ── Step 4: Wait for BMS first connection ─────────────────────────────────────
echo "Waiting 60s for LiTime BMS connect + first data write..."
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
