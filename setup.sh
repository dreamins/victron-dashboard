#!/usr/bin/env bash
# =============================================================================
# Solar Monitor — interactive setup wizard
# =============================================================================
#
# PURPOSE
#   Single entry point for first-time setup and re-runs. Walks the user through
#   every configuration step in order, does all work silently, and only asks for
#   information that cannot be automated (WiFi password, device encryption keys,
#   domain name, Google OAuth credentials).
#
# USAGE
#   ./setup.sh          — run all steps (idempotent; safe to re-run)
#
# STEPS (in order, each idempotent)
#   1. _setup_server        — detect LAN IP, generate ALL credentials, start
#                             the MQTT broker and database
#   2. _setup_flash         — print the scp + esphome flash command for the
#                             ESP32; wait for user confirmation
#   3. _setup_devices       — run discover.py interactively to collect device
#                             labels and AES-128 encryption keys from the user;
#                             writes config/devices.json (gitignored)
#   4. _setup_dashboard     — build and start the BLE decoder + API/dashboard;
#                             verify the health endpoint is reachable
#   5. _setup_remote_access — obtain a Let's Encrypt TLS certificate via DNS-01,
#                             configure Google OAuth (oauth2-proxy), start nginx;
#                             this step is skippable (Ctrl+C)
#
# KEY FILES READ / WRITTEN
#   .env                  — all generated secrets and config (gitignored)
#   esp32/secrets.yaml    — WiFi + MQTT credentials for the ESP32 (gitignored)
#   config/devices.json   — per-device MAC + label + AES key (gitignored)
#   config/allowed_emails — Google accounts allowed through OAuth (gitignored,
#                           regenerated from ALLOWED_EMAIL in .env on every run)
#
# .env VARIABLES (written by this script)
#   MQTT_BIND_IP          — server LAN IP (mosquitto binds here only)
#   MQTT_ESP32_PASS       — MQTT password for the ESP32 bridge client
#   MQTT_DECODER_PASS     — MQTT password for the ble-decoder client
#   INFLUXDB_TOKEN        — InfluxDB admin API token
#   INFLUXDB_ADMIN_PASS   — InfluxDB admin UI password
#   DOMAIN                — FQDN for HTTPS access (e.g. solar.example.com)
#   GOOGLE_CLIENT_ID      — Google OAuth 2.0 client ID
#   GOOGLE_CLIENT_SECRET  — Google OAuth 2.0 client secret
#   OAUTH2_COOKIE_SECRET  — random 32-byte secret for oauth2-proxy session cookies
#   ALLOWED_EMAIL         — Google account email allowed to access the dashboard
#   TZ_OFFSET_HOURS       — (optional) UTC offset for daily yield grouping
#
# SERVICES MANAGED (via docker compose)
#   mosquitto             — MQTT broker, LAN-only, auth required
#   influxdb              — time-series database (4 buckets + 4 downsample tasks)
#   ble-decoder           — decrypts BLE payloads, writes to InfluxDB
#   solar-api             — FastAPI app serving dashboard + REST API on :8080
#   oauth2-proxy          — Google OAuth gate in front of solar-api
#   nginx                 — TLS termination on :8443, forwards to oauth2-proxy
#
# IDEMPOTENCY
#   Each step checks whether its output already exists before doing any work.
#   Re-running is always safe. To redo a step, remove its sentinel from .env
#   (e.g. remove INFLUXDB_TOKEN to redo server setup, remove DOMAIN to redo TLS).
#
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BOLD=$'\033[1m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
CYAN=$'\033[0;36m'
NC=$'\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}!${NC}  $*"; }
die()  { echo -e "\n  ${RED}✗${NC}  $*\n" >&2; exit 1; }
step() { echo -e "\n${BOLD}${CYAN}▸ $*${NC}"; }
hr()   { echo -e "\n${BOLD}────────────────────────────────────────────────${NC}"; }

# Install a package if the command is not already present.
_ensure() {
    local cmd="$1" pkg="$2"
    command -v "$cmd" &>/dev/null && return
    echo -n "  Installing ${pkg}..."
    sudo apt-get update -q && sudo apt-get install -y -q "$pkg" >/dev/null 2>&1 \
        || die "Could not install ${pkg}. Install it manually and re-run."
    echo ""
    ok "${pkg} installed"
}

set_env() {
    local key="$1" val="$2"
    touch .env
    { grep -v "^${key}=" .env || true; } > .env.tmp
    echo "${key}=${val}" >> .env.tmp
    mv .env.tmp .env
}

get_env() { grep "^${1}=" .env 2>/dev/null | cut -d= -f2; }

# ─── Step 1: Network, credentials, core services ─────────────────────────────
# Generates ALL secrets upfront so no later step needs openssl. Starts mosquitto
# and influxdb (influxdb init scripts in influxdb/init/ run once on first start
# and create the victron_medium / victron_hourly / victron_test buckets and the
# four Flux downsampling tasks; they do NOT re-run on container restarts).

_setup_server() {
    # Already done?
    if [[ -n "$(get_env INFLUXDB_TOKEN)" && -n "$(get_env MQTT_BIND_IP)" ]]; then
        ok "Server already configured"
        return
    fi

    _ensure docker  "docker.io"
    _ensure openssl "openssl"
    _ensure nmcli   "network-manager"

    # Detect server IP
    step "Detecting server address"
    LAN_IP=$(hostname -I | awk '{print $1}')
    [[ -n "$LAN_IP" ]] || die "Could not detect a LAN IP address."
    ok "Server address: ${BOLD}${LAN_IP}${NC}"
    warn "This address must not change. Set a DHCP reservation in your router for this machine."

    # WiFi for the sensor bridge
    hr
    echo -e "\n${BOLD}WiFi setup${NC}"
    echo "  The sensor bridge needs to connect to your local WiFi network."
    echo ""
    ok "Available networks:"
    nmcli -f SSID,SIGNAL device wifi list --rescan yes 2>/dev/null \
        | awk 'NR==1 || /\S/' | head -12 | sed 's/^/     /'
    echo ""
    read -rp "  WiFi network name: " WIFI_SSID
    [[ -n "$WIFI_SSID" ]] || die "WiFi name cannot be empty."
    read -rsp "  WiFi password:     " WIFI_PASS; echo ""
    [[ -n "$WIFI_PASS" ]]  || die "WiFi password cannot be empty."

    # Generate all credentials
    step "Generating credentials"
    ESP32_PASS=$(openssl rand -hex 16)
    DECODER_PASS=$(openssl rand -hex 16)
    OTA_PASS=$(openssl rand -hex 8)
    FALLBACK_PASS=$(openssl rand -hex 8)
    INFLUXDB_TOKEN=$(openssl rand -hex 32)
    INFLUXDB_ADMIN_PASS=$(openssl rand -hex 16)

    set_env MQTT_BIND_IP       "$LAN_IP"
    set_env MQTT_ESP32_PASS    "$ESP32_PASS"
    set_env MQTT_DECODER_PASS  "$DECODER_PASS"
    set_env INFLUXDB_TOKEN     "$INFLUXDB_TOKEN"
    set_env INFLUXDB_ADMIN_PASS "$INFLUXDB_ADMIN_PASS"

    # Write sensor bridge config
    cat > esp32/secrets.yaml <<EOF
wifi_ssid: "${WIFI_SSID}"
wifi_password: "${WIFI_PASS}"
mqtt_broker: "${LAN_IP}"
mqtt_username: "esp32-bridge"
mqtt_password: "${ESP32_PASS}"
ota_password: "${OTA_PASS}"
fallback_password: "${FALLBACK_PASS}"
EOF
    ok "Credentials generated and saved"

    # Start core services
    step "Starting services"
    echo -n "  Starting..."
    docker compose up -d mosquitto influxdb >/dev/null 2>&1
    for i in $(seq 1 40); do
        docker compose exec -T influxdb influx ping &>/dev/null && break
        echo -n "."
        sleep 3
    done
    echo ""
    docker compose exec -T influxdb influx ping &>/dev/null \
        || die "Services did not start in time. Run: docker compose logs"
    ok "Services running"
}

# ─── Step 2: Flash the ESP32 ─────────────────────────────────────────────────
# esp32/secrets.yaml was written by _setup_server. The user must scp it to the
# machine connected to the ESP32 via USB, then run esphome. After this step the
# ESP32 publishes raw BLE payloads to the MQTT topic `victron/raw` at ~3 msg/s.
# Board target: esp32-s3-devkitc-1. BLE duty cycle is 50% (intentional — the
# single 2.4 GHz antenna is shared between BLE and WiFi; continuous scan causes
# WiFi disconnects). See esp32/victron-bridge.yaml for full config.

_setup_flash() {
    # Already flashed if secrets.yaml was consumed and ESP32 has connected
    if [[ ! -f esp32/secrets.yaml ]]; then
        ok "Sensor bridge already flashed"
        return
    fi

    LAN_IP=$(get_env MQTT_BIND_IP)
    SERVER_USER=$(whoami)

    hr
    cat <<EOF

${BOLD}Flash the sensor bridge${NC}

  The configuration file has been generated on this server.

  On the machine connected to the ESP32 via USB, run these two commands:

    ${BOLD}scp ${SERVER_USER}@${LAN_IP}:$(pwd)/esp32/secrets.yaml esp32/${NC}
    ${BOLD}esphome run esp32/victron-bridge.yaml${NC}

  (Install ESPHome first if needed:  pip install esphome)

  Once flashed, place the ESP32 within ~10 m of your Victron devices
  and power it from any USB adapter.

EOF
    read -rp "  Press Enter once the ESP32 is running and positioned near the devices... " _
    echo ""
}

# ─── Step 3: Device encryption keys ──────────────────────────────────────────
# Victron devices broadcast AES-128-CTR encrypted BLE advertisements. Each
# device has a unique 32-hex-char key visible in VictronConnect: tap device →
# ⋮ → Product Info → Encryption Key. discover.py listens to the MQTT topic
# `victron/raw` and interactively prompts for a label and key for each new MAC.
# Output is config/devices.json (gitignored). The ble-decoder reads this file
# on startup; changing it requires `docker compose restart ble-decoder`.
# discover.py also supports --duration to control how long it listens.

_setup_devices() {
    # Already done?
    if [[ -f config/devices.json ]]; then
        local count
        count=$(python3 -c \
            "import json; d=json.load(open('config/devices.json')); print(len(d.get('devices',[])))" \
            2>/dev/null || echo 0)
        if [[ "$count" -gt 0 ]]; then
            ok "${count} device(s) already configured"
            # Restart decoder to pick up any changes
            docker compose restart ble-decoder >/dev/null 2>&1 || true
            return
        fi
    fi

    hr
    cat <<EOF

${BOLD}Victron device setup${NC}

  Each Victron device encrypts its data with a unique key.
  You'll need to retrieve it from the VictronConnect app:

    Open VictronConnect  →  tap the device  →  ⋮  →  Product Info  →  Encryption Key

  The scan will run for up to 60 seconds. Have VictronConnect open and ready.

EOF
    read -rp "  Press Enter to start scanning for devices... " _
    echo ""

    LAN_IP=$(get_env MQTT_BIND_IP)
    DECODER_PASS=$(get_env MQTT_DECODER_PASS)

    # Build decoder image if needed
    docker compose build ble-decoder >/dev/null 2>&1

    # Start services so MQTT is available for discover.py
    docker compose up -d mosquitto influxdb >/dev/null 2>&1

    python3 decoder/discover.py \
        --broker "$LAN_IP" \
        --username decoder \
        --password "$DECODER_PASS" \
        --output config/devices.json

    if [[ -f config/devices.json ]]; then
        local count
        count=$(python3 -c \
            "import json; d=json.load(open('config/devices.json')); print(len(d.get('devices',[])))" \
            2>/dev/null || echo 0)
        if [[ "$count" -gt 0 ]]; then
            ok "${count} device(s) configured"
        else
            warn "No devices saved. Re-run ./setup.sh to try again."
        fi
    fi
}

# ─── Step 4+5: Start decoder and dashboard ───────────────────────────────────
# ble-decoder: subscribes to `victron/raw`, decrypts each payload using the
#   device key from config/devices.json, timestamps on receipt (server-side
#   authority — ESP32 has no RTC), writes to InfluxDB `victron` bucket.
#   Has an in-memory retry buffer (500 readings, ~2.5 min at 3 msg/s) with
#   exponential-backoff writes so InfluxDB restarts don't lose data.
# solar-api: FastAPI app at :8080. Serves static dashboard at / and REST API
#   at /api/v1/. Stitches raw/medium/hourly InfluxDB buckets so charts always
#   show 1-second resolution for the last hour regardless of selected range.
#   Caps responses at 500 points. No auth at this layer (enforced by nginx).

_setup_dashboard() {
    step "Starting data pipeline and dashboard"

    docker compose up -d --build mosquitto influxdb ble-decoder solar-api >/dev/null 2>&1

    # Wait for decoder to connect
    echo -n "  Connecting to devices..."
    for i in $(seq 1 20); do
        docker compose logs ble-decoder 2>/dev/null | grep -q "MQTT connected" && break
        echo -n "."
        sleep 2
    done
    echo ""

    # Wait for API to be healthy
    LAN_IP=$(get_env MQTT_BIND_IP)
    for i in $(seq 1 15); do
        curl -sf "http://localhost:8080/health" >/dev/null 2>&1 && break
        sleep 2
    done

    if curl -sf "http://localhost:8080/health" >/dev/null 2>&1; then
        ok "Dashboard is live"
        echo ""
        echo -e "  ${BOLD}Local access:${NC}  http://${LAN_IP}:8080/"
    else
        warn "Dashboard did not start cleanly. Check: docker compose logs solar-api"
    fi
}

# ─── Step 6: Remote access (TLS + Google OAuth) ──────────────────────────────
# nginx: terminates TLS on :8443, forwards all traffic to oauth2-proxy.
#   Certificate from Let's Encrypt via DNS-01 challenge (no port 80 needed).
#   Supports Cloudflare, DigitalOcean, Route53, or manual TXT record.
#   Sets X-Forwarded-Port: 8443 so oauth2-proxy builds the correct redirect URI.
# oauth2-proxy v7.6.0: enforces Google OAuth. Pinned version — v7.x latest
#   ignores OAUTH2_PROXY_UPSTREAM env var (pflag strings-slice bug); upstream
#   must be passed as CLI arg `command: "--upstream=http://solar-api:8080/"`.
#   Cookie SameSite=lax is required (strict blocks the Google OAuth redirect).
#   Allowed accounts are listed in config/allowed_emails (one email per line),
#   regenerated from ALLOWED_EMAIL in .env on every run of this script.
# Port forward required: router WAN:8443 → server LAN:8443 (TCP).

_setup_remote_access() {
    # Already configured?
    if [[ -n "$(get_env DOMAIN)" && -n "$(get_env GOOGLE_CLIENT_ID)" ]]; then
        _p6_regen_allowed_emails
        docker compose up -d nginx oauth2-proxy >/dev/null 2>&1
        _p6_print_done
        return
    fi

    hr
    cat <<EOF

${BOLD}Remote access setup (optional)${NC}

  This step sets up HTTPS and Google sign-in so you can reach the dashboard
  from anywhere. Press Ctrl+C to skip and use local access only.

EOF
    read -rp "  Press Enter to continue with remote access setup... " _

    # Domain
    echo ""
    echo "  Enter your domain name (e.g. solar.yourdomain.com)."
    echo "  A DNS A record must already point it to your public IP."
    echo ""
    read -rp "  Domain: " DOMAIN
    [[ -n "$DOMAIN" ]] || die "Domain cannot be empty."
    set_env DOMAIN "$DOMAIN"

    # TLS certificate
    _setup_tls "$DOMAIN"

    # Google OAuth
    _setup_oauth "$DOMAIN"

    # Allowed email
    echo ""
    echo "  Enter the Google account email address that will have access."
    echo ""
    read -rp "  Your email: " ALLOWED_EMAIL
    [[ -n "$ALLOWED_EMAIL" ]] || die "Email cannot be empty."
    set_env ALLOWED_EMAIL "$ALLOWED_EMAIL"
    _p6_regen_allowed_emails

    # Start
    step "Enabling HTTPS and sign-in"
    docker compose up -d nginx oauth2-proxy >/dev/null 2>&1

    echo -n "  Waiting for HTTPS to become ready..."
    for i in $(seq 1 15); do
        docker compose ps nginx 2>/dev/null | grep -q "running\|Up" && break
        echo -n "."
        sleep 2
    done
    echo ""

    _p6_print_done
}

_setup_tls() {
    local domain="$1"

    if [[ -f "/etc/letsencrypt/live/${domain}/fullchain.pem" ]]; then
        ok "TLS certificate already exists for ${domain}"
        _p6_setup_renewal_hook
        return
    fi

    step "Obtaining TLS certificate"
    echo ""
    echo "  A certificate will be obtained automatically via your DNS provider."
    echo "  No port 80 needs to be open."
    echo ""

    _ensure certbot "certbot"

    echo "  Select your DNS provider:"
    echo "    1) Cloudflare"
    echo "    2) DigitalOcean"
    echo "    3) Route 53 (AWS)"
    echo "    4) Other (you'll add the DNS TXT record yourself)"
    echo ""
    read -rp "  Choice [1-4]: " DNS_CHOICE

    read -rp "  Email for certificate renewal notices: " CERT_EMAIL
    [[ -n "$CERT_EMAIL" ]] || die "Email cannot be empty."

    case "$DNS_CHOICE" in
        1) _p6_certbot_cloudflare    "$domain" "$CERT_EMAIL" ;;
        2) _p6_certbot_digitalocean  "$domain" "$CERT_EMAIL" ;;
        3) _p6_certbot_route53       "$domain" "$CERT_EMAIL" ;;
        4) _p6_certbot_manual        "$domain" "$CERT_EMAIL" ;;
        *) die "Invalid choice." ;;
    esac

    _p6_setup_renewal_hook
}

_setup_oauth() {
    local domain="$1"
    local redirect_uri="https://${domain}:8443/oauth2/callback"

    step "Google sign-in setup"
    cat <<EOF

  Create an OAuth app at: ${BOLD}https://console.cloud.google.com/apis/credentials${NC}

    → Create Credentials → OAuth 2.0 Client ID
    → Application type: Web application
    → Authorized redirect URIs → add exactly:

        ${BOLD}${redirect_uri}${NC}

    → Create → copy the Client ID and Client Secret

EOF
    read -rp "  Press Enter once you have the Client ID and Secret... " _
    echo ""
    read -rp "  Client ID:     " GOOGLE_CLIENT_ID
    read -rsp "  Client Secret: " GOOGLE_CLIENT_SECRET; echo ""
    [[ -n "$GOOGLE_CLIENT_ID" ]]     || die "Client ID cannot be empty."
    [[ -n "$GOOGLE_CLIENT_SECRET" ]] || die "Client Secret cannot be empty."

    OAUTH2_COOKIE_SECRET=$(openssl rand -hex 16)
    set_env GOOGLE_CLIENT_ID     "$GOOGLE_CLIENT_ID"
    set_env GOOGLE_CLIENT_SECRET "$GOOGLE_CLIENT_SECRET"
    set_env OAUTH2_COOKIE_SECRET "$OAUTH2_COOKIE_SECRET"
    ok "Sign-in configured"
}

_p6_regen_allowed_emails() {
    local email
    email=$(get_env ALLOWED_EMAIL)
    if [[ -n "$email" ]]; then
        mkdir -p config
        printf '%s\n' "$email" > config/allowed_emails
    fi
}

_p6_setup_renewal_hook() {
    local hook_dir="/etc/letsencrypt/renewal-hooks/deploy"
    local project_dir; project_dir="$(pwd)"
    sudo mkdir -p "$hook_dir"
    sudo bash -c "cat > ${hook_dir}/reload.sh" <<EOF
#!/bin/bash
cd ${project_dir}
docker compose exec -T nginx nginx -s reload
EOF
    sudo chmod +x "${hook_dir}/reload.sh"
    sudo systemctl enable --now certbot.timer 2>/dev/null || true
}

_p6_print_done() {
    local domain;  domain=$(get_env DOMAIN)
    local lan_ip;  lan_ip=$(get_env MQTT_BIND_IP)

    # Verify HTTPS silently
    local https_ok=false
    curl -sf --max-time 5 "https://${domain}:8443/health" >/dev/null 2>&1 && https_ok=true

    hr
    echo ""
    echo -e "  ${BOLD}Setup complete.${NC}"
    echo ""
    if $https_ok; then
        ok "HTTPS is working"
        echo ""
        echo -e "  ${BOLD}Dashboard:${NC}  https://${domain}:8443/"
    else
        warn "HTTPS not reachable yet"
        echo ""
        echo -e "  ${BOLD}Dashboard:${NC}  https://${domain}:8443/"
        echo ""
        echo "  If you haven't already, forward port 8443 on your router to ${lan_ip}."
    fi
    echo ""
    echo -e "  ${BOLD}Local:${NC}      http://${lan_ip}:8080/  (no sign-in required on your LAN)"
    echo ""
}

_p6_certbot_cloudflare() {
    local domain="$1" email="$2"
    apt list --installed 2>/dev/null | grep -q python3-certbot-dns-cloudflare \
        || sudo apt-get install -y -q python3-certbot-dns-cloudflare

    echo ""
    echo "  Create a Cloudflare API Token (Zone → DNS → Edit) at:"
    echo "  Account → My Profile → API Tokens → Create Token"
    echo ""
    read -rsp "  Cloudflare API Token: " CF_TOKEN; echo ""

    local creds="/etc/letsencrypt/cloudflare.ini"
    sudo bash -c "printf 'dns_cloudflare_api_token = %s\n' '${CF_TOKEN}' > ${creds}"
    sudo chmod 600 "$creds"
    sudo certbot certonly \
        --dns-cloudflare --dns-cloudflare-credentials "$creds" \
        --dns-cloudflare-propagation-seconds 30 \
        -d "$domain" --non-interactive --agree-tos --email "$email"
    ok "Certificate issued"
}

_p6_certbot_digitalocean() {
    local domain="$1" email="$2"
    apt list --installed 2>/dev/null | grep -q python3-certbot-dns-digitalocean \
        || sudo apt-get install -y -q python3-certbot-dns-digitalocean

    echo ""
    echo "  Create a Personal Access Token (write scope) at:"
    echo "  https://cloud.digitalocean.com/account/api/tokens"
    echo ""
    read -rsp "  DigitalOcean API Token: " DO_TOKEN; echo ""

    local creds="/etc/letsencrypt/digitalocean.ini"
    sudo bash -c "printf 'dns_digitalocean_token = %s\n' '${DO_TOKEN}' > ${creds}"
    sudo chmod 600 "$creds"
    sudo certbot certonly \
        --dns-digitalocean --dns-digitalocean-credentials "$creds" \
        --dns-digitalocean-propagation-seconds 30 \
        -d "$domain" --non-interactive --agree-tos --email "$email"
    ok "Certificate issued"
}

_p6_certbot_route53() {
    local domain="$1" email="$2"
    apt list --installed 2>/dev/null | grep -q python3-certbot-dns-route53 \
        || sudo apt-get install -y -q python3-certbot-dns-route53

    echo ""
    echo "  The IAM user needs: route53:ChangeResourceRecordSets,"
    echo "  route53:ListHostedZones, route53:GetChange on the zone for ${domain}."
    echo ""
    read -rp  "  AWS Access Key ID:     " AWS_ACCESS_KEY_ID
    read -rsp "  AWS Secret Access Key: " AWS_SECRET_ACCESS_KEY; echo ""

    sudo AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
         AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
         certbot certonly --dns-route53 \
        -d "$domain" --non-interactive --agree-tos --email "$email"
    ok "Certificate issued"
}

_p6_certbot_manual() {
    local domain="$1" email="$2"
    echo ""
    echo "  certbot will show a DNS TXT record value to add."
    echo "  Add it in your DNS provider, wait ~60 s, then press Enter."
    echo ""
    sudo certbot certonly \
        --manual --preferred-challenges dns \
        -d "$domain" --agree-tos --email "$email"
    ok "Certificate issued"
}

# ─── Main ─────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo -e "${BOLD}Solar Monitor Setup${NC}"
    echo -e "────────────────────"

    _setup_server
    _setup_flash
    _setup_devices
    _setup_dashboard
    _setup_remote_access
}

main
