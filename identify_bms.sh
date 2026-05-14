#!/usr/bin/env bash
# identify_bms.sh — One-time BMS identification wizard.
#
# Probes all nearby LiTime BMS devices, shows their live SOC/voltage,
# and asks you to assign each to a named slot in config/sites.json.
# MACs are saved permanently so ble-bridge connects directly on restart.
#
# Usage (from ~/victron-dashboard, BMS powered on and nearby):
#   bash identify_bms.sh
#
# You only need to run this once per BMS device.  If you have just one
# BMS, the assignment happens automatically with no questions asked.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found — is Docker installed?"
    exit 1
fi

echo "=== LiTime BMS Identification Wizard ==="
echo "Make sure all BMS devices are powered on and nearby."
echo ""

# Mount sites.json at a writable path inside the container.
# (The compose file mounts it :ro; we add a separate :rw mount at a
# different target path and point SITES_FILE at that.)
docker compose run --rm --no-deps \
    --volume "$(pwd)/config/sites.json:/app/sites_writable.json" \
    --env    "SITES_FILE=/app/sites_writable.json" \
    ble-bridge \
    python /app/identify_bms.py

echo ""
echo "Restarting ble-bridge with updated configuration..."
docker compose up -d --no-deps ble-bridge
echo "ble-bridge restarted — connecting to identified BMS device(s)."
