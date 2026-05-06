#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

header() { echo -e "\n${BOLD}=== $* ===${NC}\n"; }
ok()     { echo -e "${GREEN}[+]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
die()    { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

set_env() {
    local key="$1" val="$2"
    touch .env
    { grep -v "^${key}=" .env || true; } > .env.tmp
    echo "${key}=${val}" >> .env.tmp
    mv .env.tmp .env
}

# ─── Phase 1: ESP32 Firmware Setup ──────────────────────────────────────────

phase1() {
    header "Phase 1 — ESP32 Firmware Setup"

    # Prerequisites
    for cmd in openssl nmcli; do
        command -v "$cmd" &>/dev/null || die "$cmd not found. Install it and re-run."
    done

    # Idempotency: skip if secrets.yaml already populated
    if [[ -f esp32/secrets.yaml ]] && grep -q 'mqtt_password: ".\+' esp32/secrets.yaml 2>/dev/null; then
        warn "esp32/secrets.yaml already exists with credentials — skipping generation"
        warn "Delete esp32/secrets.yaml to regenerate."
        print_flash_instructions
        return
    fi

    # LAN IP
    header "Server LAN IP"
    LAN_IP=$(hostname -I | awk '{print $1}')
    [[ -n "$LAN_IP" ]] || die "Could not detect LAN IP"
    ok "Detected LAN IP: ${BOLD}${LAN_IP}${NC}"

    echo ""
    warn "This IP must be stable. If it changes, the ESP32 cannot reach MQTT."
    echo -n "  Server MAC (for DHCP reservation): "
    ip link show "$(ip route | awk '/default/{print $5; exit}')" 2>/dev/null | awk '/ether/{print $2}' || echo "(run: ip link show)"
    echo ""
    read -rp "Have you set a DHCP reservation for this server in your router? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { warn "Set a DHCP reservation, then re-run."; exit 0; }

    # WiFi
    header "WiFi"
    ok "Scanning for networks..."
    nmcli -f SSID,SIGNAL device wifi list --rescan yes 2>/dev/null | head -15
    echo ""
    read -rp "WiFi SSID: " WIFI_SSID
    read -rsp "WiFi password: " WIFI_PASS
    echo ""

    # Credentials
    header "Generating credentials"
    ESP32_PASS=$(openssl rand -hex 16)
    DECODER_PASS=$(openssl rand -hex 16)
    OTA_PASS=$(openssl rand -hex 8)
    FALLBACK_PASS=$(openssl rand -hex 8)
    ok "MQTT credentials generated"

    # Write esp32/secrets.yaml
    cat > esp32/secrets.yaml <<EOF
wifi_ssid: "${WIFI_SSID}"
wifi_password: "${WIFI_PASS}"
mqtt_broker: "${LAN_IP}"
mqtt_username: "esp32-bridge"
mqtt_password: "${ESP32_PASS}"
ota_password: "${OTA_PASS}"
fallback_password: "${FALLBACK_PASS}"
EOF
    ok "esp32/secrets.yaml written"
    warn "This file is gitignored — back it up separately"

    # Write .env for docker-compose
    set_env MQTT_BIND_IP    "$LAN_IP"
    set_env MQTT_ESP32_PASS "$ESP32_PASS"
    set_env MQTT_DECODER_PASS "$DECODER_PASS"
    ok ".env updated (MQTT_BIND_IP, MQTT_ESP32_PASS, MQTT_DECODER_PASS)"

    print_flash_instructions
}

print_flash_instructions() {
    header "Next steps — Flash the ESP32"

    LAN_IP=$(grep MQTT_BIND_IP .env 2>/dev/null | cut -d= -f2 || hostname -I | awk '{print $1}')
    DECODER_PASS=$(grep MQTT_DECODER_PASS .env 2>/dev/null | cut -d= -f2 || echo "<see .env>")

    cat <<EOF

${BOLD}On the machine connected to the ESP32 via USB (your Windows machine):${NC}

  1. Install ESPHome:
       pip install esphome

  2. Copy esp32/secrets.yaml to that machine (scp, USB stick, or shared drive).
     It is gitignored — transfer it manually.

  3. Flash (first time via USB, subsequent updates are OTA over WiFi):
       cd esp32/
       esphome run victron-bridge.yaml

  4. Mount the ESP32 within ~10 m line-of-sight of the Victron devices.
     Power via any USB adapter.

${BOLD}Verify the ESP32 is running (from the Linux server):${NC}

  mosquitto_sub -h ${LAN_IP} -p 1883 \\
    -u decoder -P "${DECODER_PASS}" \\
    -t 'victron/#' -v

  Expected: ~3 JSON payloads/second on victron/raw once ESP32 is near devices.
  Anonymous connect test (should be refused):
    mosquitto_pub -h ${LAN_IP} -p 1883 -t test -m x

${BOLD}Isolated test (no ESP32 needed — run on the Linux server):${NC}

  docker compose -f docker-compose.yml -f docker-compose.test.yml up -d mosquitto
  python3 decoder/replay.py --loop &
  mosquitto_sub -h localhost -p 1883 \\
    -u decoder -P test_decoder_pass \\
    -t 'victron/raw' -v
  # Should show: victron/raw {"mac": "AA:BB:CC:DD:EE:0X", "data": "..."}
  kill %1
  docker compose stop mosquitto

${BOLD}Phase 1 complete.${NC} Proceed to Phase 2 (server infrastructure) in parallel.

EOF
}

# ─── Stubs for future phases ─────────────────────────────────────────────────

phase2() {
    header "Phase 2 — Server Infrastructure"

    command -v docker &>/dev/null || die "docker not found. Install Docker and re-run."
    command -v openssl &>/dev/null || die "openssl not found."
    [[ -f .env ]] || die ".env not found. Run ./setup.sh 1 first."
    grep -q "^MQTT_BIND_IP=" .env || die "MQTT_BIND_IP missing from .env. Run ./setup.sh 1 first."

    # Idempotency: skip credential generation if already present
    if grep -q "^INFLUXDB_TOKEN=" .env 2>/dev/null; then
        warn "InfluxDB credentials already in .env — skipping generation"
    else
        INFLUXDB_TOKEN=$(openssl rand -hex 32)
        INFLUXDB_ADMIN_PASS=$(openssl rand -hex 16)
        set_env INFLUXDB_TOKEN      "$INFLUXDB_TOKEN"
        set_env INFLUXDB_ADMIN_PASS "$INFLUXDB_ADMIN_PASS"
        ok "InfluxDB credentials generated"
    fi

    ok "Starting mosquitto and influxdb..."
    docker compose up -d mosquitto influxdb

    ok "Waiting for InfluxDB (may take ~30s on first run while init scripts execute)..."
    for i in $(seq 1 40); do
        docker compose exec -T influxdb influx ping &>/dev/null && break
        sleep 3
    done
    docker compose exec -T influxdb influx ping &>/dev/null || die "InfluxDB did not become ready in 120s"
    ok "InfluxDB is healthy"

    ok "Phase 2 complete — run ./test_phase2.sh to verify"
}
phase3() { header "Phase 3 — BLE Decoder";          die "Not yet implemented. See victron-system-design.md §12 Phase 3."; }
phase4() { header "Phase 4 — API Service";           die "Not yet implemented. See victron-system-design.md §12 Phase 4."; }
phase5() { header "Phase 5 — Dashboard UI";          die "Not yet implemented. See victron-system-design.md §12 Phase 5."; }
phase6() { header "Phase 6 — Auth + TLS";            die "Not yet implemented. See victron-system-design.md §12 Phase 6."; }

# ─── Entry point ─────────────────────────────────────────────────────────────

PHASE="${1:-1}"
case "$PHASE" in
    1) phase1 ;;
    2) phase2 ;;
    3) phase3 ;;
    4) phase4 ;;
    5) phase5 ;;
    6) phase6 ;;
    *) echo "Usage: $0 [1-6]"; exit 1 ;;
esac
