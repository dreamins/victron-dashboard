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
phase3() {
    header "Phase 3 — BLE Decoder"

    command -v docker &>/dev/null || die "docker not found. Install Docker and re-run."
    [[ -f .env ]] || die ".env not found. Run ./setup.sh 2 first."
    grep -q "^INFLUXDB_TOKEN=" .env || die "InfluxDB not configured. Run ./setup.sh 2 first."

    ok "Building ble-decoder image..."
    docker compose build ble-decoder

    ok "Starting mosquitto, influxdb, and ble-decoder..."
    docker compose up -d mosquitto influxdb ble-decoder

    ok "Waiting for InfluxDB to be ready..."
    for i in $(seq 1 40); do
        docker compose exec -T influxdb influx ping &>/dev/null && break
        sleep 3
    done
    docker compose exec -T influxdb influx ping &>/dev/null || die "InfluxDB did not become ready"

    ok "Waiting for decoder to connect to MQTT..."
    for i in $(seq 1 20); do
        docker compose logs ble-decoder 2>/dev/null | grep -q "MQTT connected" && break
        sleep 2
    done

    if docker compose logs ble-decoder 2>/dev/null | grep -q "MQTT connected"; then
        ok "Decoder connected to MQTT"
    else
        warn "Decoder has not connected yet — check: docker compose logs ble-decoder"
    fi

    # Device discovery guidance
    MQTT_BIND_IP=$(grep MQTT_BIND_IP .env 2>/dev/null | cut -d= -f2 || echo "<server-LAN-IP>")
    DECODER_PASS=$(grep MQTT_DECODER_PASS .env 2>/dev/null | cut -d= -f2 || echo "<see .env>")

    if [[ ! -f config/devices.json ]]; then
        warn "config/devices.json not found — device keys not yet configured"
        cat <<EOF

${BOLD}Device Discovery (requires ESP32 near Victron devices):${NC}

  python3 decoder/discover.py \\
    --broker ${MQTT_BIND_IP} \\
    --username decoder \\
    --password ${DECODER_PASS} \\
    --output config/devices.json

The script will:
  1. Listen to victron/raw for Victron advertisement MACs
  2. Identify each device type (SolarCharger, BatterySense, etc.)
  3. Prompt you for a label and the encryption key from VictronConnect

After saving config/devices.json, restart the decoder:
  docker compose restart ble-decoder

EOF
    else
        DEVICE_COUNT=$(python3 -c "import json; d=json.load(open('config/devices.json')); print(len(d.get('devices',[])))" 2>/dev/null || echo "?")
        ok "config/devices.json found (${DEVICE_COUNT} device(s) configured)"
        ok "Restarting decoder to pick up device config..."
        docker compose restart ble-decoder
    fi

    ok "Phase 3 setup complete — run ./test_phase3.sh to verify"
}
phase4() { header "Phase 4 — API Service";           die "Not yet implemented. See victron-system-design.md §12 Phase 4."; }
phase5() { header "Phase 5 — Dashboard UI";          die "Not yet implemented. See victron-system-design.md §12 Phase 5."; }
phase6() {
    header "Phase 6 — Auth + TLS"

    command -v docker &>/dev/null || die "docker not found."
    [[ -f .env ]] || die ".env not found. Run ./setup.sh 2 first."
    grep -q "^INFLUXDB_TOKEN=" .env || die "InfluxDB not configured. Run ./setup.sh 2 first."

    # ── Idempotency ──────────────────────────────────────────────────────────
    if grep -q "^DOMAIN=" .env 2>/dev/null \
    && grep -q "^GOOGLE_CLIENT_ID=" .env 2>/dev/null; then
        warn "Phase 6 already configured in .env — skipping interactive setup"
        warn "Remove DOMAIN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / OAUTH2_COOKIE_SECRET from .env to reconfigure."
        ok "Restarting nginx and oauth2-proxy with existing config..."
        docker compose up -d nginx oauth2-proxy
        _p6_print_status
        return
    fi

    # ── 1. Domain ────────────────────────────────────────────────────────────
    header "Domain Name"
    echo "Enter the fully-qualified domain name for the dashboard."
    echo "A DNS A record must already point this name to your public IP."
    echo "Example: solar.yourdomain.com"
    echo ""
    read -rp "Domain: " DOMAIN
    [[ -n "$DOMAIN" ]] || die "Domain cannot be empty."
    set_env DOMAIN "$DOMAIN"
    ok "Domain set to: ${BOLD}${DOMAIN}${NC}"

    # ── 2. TLS certificate (DNS-01) ──────────────────────────────────────────
    header "Let's Encrypt Certificate — DNS-01 Challenge"
    echo "certbot will prove ownership of ${DOMAIN} by creating a TXT record via your"
    echo "DNS provider's API. No open port 80 is required."
    echo ""

    # Ensure certbot is present
    if ! command -v certbot &>/dev/null; then
        ok "Installing certbot..."
        sudo apt-get update -q && sudo apt-get install -y -q certbot
    fi

    echo "Select your DNS provider:"
    echo "  1) Cloudflare"
    echo "  2) DigitalOcean"
    echo "  3) Route 53 (AWS)"
    echo "  4) Other / manual (you'll add the TXT record yourself)"
    read -rp "Choice [1-4]: " DNS_CHOICE

    EMAIL=$(git config --global user.email 2>/dev/null || true)
    [[ -n "$EMAIL" ]] || read -rp "Email for Let's Encrypt expiry notices: " EMAIL

    case "$DNS_CHOICE" in
        1) _p6_certbot_cloudflare  "$DOMAIN" "$EMAIL" ;;
        2) _p6_certbot_digitalocean "$DOMAIN" "$EMAIL" ;;
        3) _p6_certbot_route53      "$DOMAIN" "$EMAIL" ;;
        4) _p6_certbot_manual       "$DOMAIN" "$EMAIL" ;;
        *) die "Invalid choice." ;;
    esac

    # ── 3. Certbot auto-renewal hook ─────────────────────────────────────────
    _p6_setup_renewal_hook

    # ── 4. Google OAuth credentials ──────────────────────────────────────────
    header "Google OAuth 2.0 Credentials"
    REDIRECT_URI="https://${DOMAIN}:8443/oauth2/callback"
    cat <<EOF

${BOLD}Step 1 — Create an OAuth 2.0 Client ID in Google Cloud Console:${NC}

  URL: https://console.cloud.google.com/apis/credentials
  → Create Credentials → OAuth 2.0 Client ID
  → Application type: Web application
  → Authorized redirect URIs → Add exactly:

      ${BOLD}${REDIRECT_URI}${NC}

  → Create → copy the Client ID and Client Secret.

EOF
    read -rp "Press Enter once you have the Client ID and Secret ready... " _
    read -rp  "Google OAuth Client ID:     " GOOGLE_CLIENT_ID
    read -rsp "Google OAuth Client Secret: " GOOGLE_CLIENT_SECRET
    echo ""
    [[ -n "$GOOGLE_CLIENT_ID" ]]     || die "Client ID cannot be empty."
    [[ -n "$GOOGLE_CLIENT_SECRET" ]] || die "Client Secret cannot be empty."

    OAUTH2_COOKIE_SECRET=$(openssl rand -hex 16)
    set_env GOOGLE_CLIENT_ID     "$GOOGLE_CLIENT_ID"
    set_env GOOGLE_CLIENT_SECRET "$GOOGLE_CLIENT_SECRET"
    set_env OAUTH2_COOKIE_SECRET "$OAUTH2_COOKIE_SECRET"
    ok "OAuth credentials saved to .env"

    # ── 5. Start nginx + oauth2-proxy ────────────────────────────────────────
    header "Starting Auth + TLS Stack"
    docker compose up -d nginx oauth2-proxy

    ok "Waiting for nginx to become healthy..."
    for i in $(seq 1 15); do
        docker compose ps nginx 2>/dev/null | grep -q "running\|Up" && break
        sleep 2
    done

    _p6_print_status
}

_p6_certbot_cloudflare() {
    local domain="$1" email="$2"
    apt list --installed 2>/dev/null | grep -q python3-certbot-dns-cloudflare \
        || sudo apt-get install -y -q python3-certbot-dns-cloudflare

    echo ""
    echo "Create a Cloudflare API Token with permissions:"
    echo "  Zone — Zone — Read"
    echo "  Zone — DNS  — Edit"
    echo "(Account → My Profile → API Tokens → Create Token)"
    read -rsp "Cloudflare API Token: " CF_TOKEN
    echo ""

    local creds="/etc/letsencrypt/cloudflare.ini"
    sudo bash -c "printf 'dns_cloudflare_api_token = %s\n' '${CF_TOKEN}' > ${creds}"
    sudo chmod 600 "$creds"

    sudo certbot certonly \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$creds" \
        --dns-cloudflare-propagation-seconds 30 \
        -d "$domain" \
        --non-interactive --agree-tos --email "$email"
    ok "Certificate issued for ${domain}"
}

_p6_certbot_digitalocean() {
    local domain="$1" email="$2"
    apt list --installed 2>/dev/null | grep -q python3-certbot-dns-digitalocean \
        || sudo apt-get install -y -q python3-certbot-dns-digitalocean

    echo ""
    echo "Create a DigitalOcean Personal Access Token (write scope) at:"
    echo "  https://cloud.digitalocean.com/account/api/tokens"
    read -rsp "DigitalOcean API Token: " DO_TOKEN
    echo ""

    local creds="/etc/letsencrypt/digitalocean.ini"
    sudo bash -c "printf 'dns_digitalocean_token = %s\n' '${DO_TOKEN}' > ${creds}"
    sudo chmod 600 "$creds"

    sudo certbot certonly \
        --dns-digitalocean \
        --dns-digitalocean-credentials "$creds" \
        --dns-digitalocean-propagation-seconds 30 \
        -d "$domain" \
        --non-interactive --agree-tos --email "$email"
    ok "Certificate issued for ${domain}"
}

_p6_certbot_route53() {
    local domain="$1" email="$2"
    apt list --installed 2>/dev/null | grep -q python3-certbot-dns-route53 \
        || sudo apt-get install -y -q python3-certbot-dns-route53

    echo ""
    echo "Route 53 DNS-01 uses your AWS credentials (~/.aws/credentials or env vars)."
    echo "The IAM user needs: route53:ChangeResourceRecordSets, route53:ListHostedZones,"
    echo "route53:GetChange on the hosted zone for ${domain}."
    echo ""
    read -rp "AWS_ACCESS_KEY_ID:     " AWS_ACCESS_KEY_ID
    read -rsp "AWS_SECRET_ACCESS_KEY: " AWS_SECRET_ACCESS_KEY
    echo ""

    sudo AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
         AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
         certbot certonly \
        --dns-route53 \
        -d "$domain" \
        --non-interactive --agree-tos --email "$email"
    ok "Certificate issued for ${domain}"
}

_p6_certbot_manual() {
    local domain="$1" email="$2"
    cat <<EOF

${BOLD}Manual DNS-01 challenge:${NC}
certbot will print a TXT record value. Add it to your DNS, wait ~60 s for
propagation, then press Enter to continue.

EOF
    sudo certbot certonly \
        --manual \
        --preferred-challenges dns \
        -d "$domain" \
        --agree-tos --email "$email"
    ok "Certificate issued for ${domain}"
}

_p6_setup_renewal_hook() {
    local hook_dir="/etc/letsencrypt/renewal-hooks/deploy"
    local hook_file="${hook_dir}/reload-nginx.sh"
    local project_dir
    project_dir="$(pwd)"

    sudo mkdir -p "$hook_dir"
    sudo bash -c "cat > ${hook_file}" <<EOF
#!/bin/bash
cd ${project_dir}
docker compose exec -T nginx nginx -s reload
EOF
    sudo chmod +x "$hook_file"
    sudo systemctl enable --now certbot.timer 2>/dev/null || true
    ok "Certbot renewal hook installed; certbot.timer enabled"
}

_p6_print_status() {
    local DOMAIN
    DOMAIN=$(grep "^DOMAIN=" .env 2>/dev/null | cut -d= -f2 || echo "<domain>")
    local LAN_IP
    LAN_IP=$(grep "^MQTT_BIND_IP=" .env 2>/dev/null | cut -d= -f2 || hostname -I | awk '{print $1}')

    cat <<EOF

${BOLD}── Router Port-Forward (Ubiquiti UniFi) ─────────────────────────────────────${NC}

  Settings → Security (or Routing & Firewall) → Port Forwarding → Add rule:

    Name:         victron-dashboard
    Interface:    WAN
    Port:         8443
    Forward IP:   ${LAN_IP}
    Forward Port: 8443
    Protocol:     TCP

${BOLD}── Verify ────────────────────────────────────────────────────────────────────${NC}

  curl -sI https://${DOMAIN}:8443/health
    → expect: HTTP/2 200 with Strict-Transport-Security header
    → browser opens Google sign-in before reaching /health

  systemctl status certbot.timer
    → should show: active (waiting)

${BOLD}── Dashboard ─────────────────────────────────────────────────────────────────${NC}

  https://${DOMAIN}:8443/

  Note: LAN access without auth is still available at http://${LAN_IP}:8080/
  To restrict it: remove solar-api ports from docker-compose.yml and re-deploy.

EOF
    ok "Phase 6 setup complete."
}

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
