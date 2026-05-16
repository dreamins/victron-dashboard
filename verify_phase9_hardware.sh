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

# Install firmware for common BT5 dongles.  On Debian, firmware-realtek is in
# non-free or non-free-firmware; enable it if needed.
_ensure_non_free() {
    local src=/etc/apt/sources.list
    if ! grep -qE 'non-free(-firmware)?' "$src" 2>/dev/null; then
        sudo -n sed -i 's/main$/main non-free non-free-firmware/' "$src" 2>/dev/null || true
        sudo -n apt-get update -qq 2>/dev/null || true
    fi
}
if ! dpkg -l firmware-realtek 2>/dev/null | grep -q '^ii'; then
    echo "  Installing firmware-realtek (RTL8761B dongle support)..."
    _ensure_non_free
    sudo -n apt-get install -y firmware-realtek 2>/dev/null || \
        echo "  WARNING: firmware-realtek not installable — check apt sources"
fi
if ! dpkg -l firmware-brcm80211 2>/dev/null | grep -q '^ii'; then
    echo "  Installing firmware-brcm80211 (Broadcom dongle support)..."
    _ensure_non_free
    sudo -n apt-get install -y firmware-brcm80211 2>/dev/null || true
fi

# USB soft-replug the new dongle (hci1) so the kernel re-probes with the
# newly installed firmware.  Firmware is loaded at USB probe time — a mere
# bluetooth service restart does NOT re-probe the USB device.
_usb_replug_hci() {
    local hci="$1"
    local hci_sysfs
    hci_sysfs=$(readlink -f "/sys/class/bluetooth/$hci" 2>/dev/null) || return
    # Walk up to find the USB device directory (has idVendor file).
    local p="$hci_sysfs" usb_path="" usb_id=""
    while [ "$p" != "/" ]; do
        if [ -f "$p/idVendor" ]; then
            usb_path="$p"
            usb_id=$(basename "$p")
            echo "  $hci USB device: $(cat "$p/idVendor"):$(cat "$p/idProduct")  ($usb_id)"
            break
        fi
        p=$(dirname "$p")
    done
    if [ -n "$usb_id" ]; then
        echo "  USB soft-replug $usb_id to reload firmware..."
        echo "$usb_id" | sudo tee /sys/bus/usb/drivers/usb/unbind >/dev/null 2>&1 || true
        sleep 1
        echo "$usb_id" | sudo tee /sys/bus/usb/drivers/usb/bind >/dev/null 2>&1 || true
        sleep 3
    fi
}

# Identify which hci interfaces are not the Atheros (hci0) and replug them.
for hci_path in /sys/class/bluetooth/hci*/; do
    hci_name=$(basename "$hci_path")
    [ "$hci_name" = "hci0" ] && continue  # hci0 is the known Atheros — leave it
    _usb_replug_hci "$hci_name"
done

# (Re-)start bluetoothd after USB replug so it registers the freshly probed adapter.
rfkill unblock bluetooth 2>/dev/null || true
if sudo -n systemctl restart bluetooth 2>/dev/null; then
    echo "  Bluetooth service restarted"
    sleep 4
fi

# Bring up all adapters.
for hci_path in /sys/class/bluetooth/hci*/; do
    hci_name=$(basename "$hci_path")
    hciconfig "$hci_name" up 2>/dev/null || true
done

BLE_ADAPTER=$(python3 - <<'PYEOF'
import subprocess, sys

try:
    out = subprocess.check_output(["hciconfig", "-a"], text=True, stderr=subprocess.DEVNULL)
except Exception:
    sys.exit(0)

adapters = {}
current = None
for line in out.splitlines():
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

# Prefer any non-Atheros adapter.
for name, info in adapters.items():
    if "Atheros" not in info["manufacturer"]:
        print(name)
        sys.exit(0)

# Fall back to highest version.
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
    sleep 2
    sed -i '/^BLE_ADAPTER=/d' .env
    echo "BLE_ADAPTER=$BLE_ADAPTER" >> .env
    export BLE_ADAPTER="$BLE_ADAPTER"
else
    echo "  No non-default adapter detected — using system default"
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
