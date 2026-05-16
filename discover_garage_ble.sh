#!/usr/bin/env bash
# discover_garage_ble.sh — Scan for Victron BLE devices from the ble-bridge container.
# Prints MAC addresses of any nearby Victron device (MPPT, Battery Sense, etc.).
# After discovery: add MACs + keys (from Victron Connect app) to config/sites.json.
set -uo pipefail
cd "$(dirname "$0")"

BLE_ADAPTER=$(grep -E '^BLE_ADAPTER=' .env 2>/dev/null | cut -d= -f2- || true)

echo "=== Victron BLE Device Discovery ==="
echo ""

docker compose exec \
    -e BLE_ADAPTER="${BLE_ADAPTER:-}" \
    -e DISCOVER_TIMEOUT=30 \
    ble-bridge python /app/discover_victron.py

echo ""
echo "=== Next Steps ==="
echo "1. Note the MAC addresses printed above"
echo "2. In the Victron Connect app on your phone:"
echo "   → Connect to each MPPT → Settings → Product Info"
echo "   → Tap the eye icon next to 'BLE key' to reveal it"
echo "3. Add to config/sites.json under the 'garage' site:"
echo "     {\"id\": \"garage_mppt1\", \"label\": \"Garage MPPT 1\","
echo "      \"type\": \"victron_mppt\", \"mac\": \"<MAC>\","
echo "      \"key\": \"<32-hex-char BLE key>\","
echo "      \"write_interval_s\": 60}"
echo "4. Restart the bridge:"
echo "     docker compose restart ble-bridge"
