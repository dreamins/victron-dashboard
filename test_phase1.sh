#!/usr/bin/env bash
# Phase 1 isolated test — runs entirely on the Linux server, no ESP32 needed.
# Usage: ./test_phase1.sh
# Requires: docker, python3, pip
set -euo pipefail

BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAILED=1; }
info() { echo -e "${YELLOW}[....] $*${NC}"; }
FAILED=0

cleanup() {
    info "Cleaning up..."
    docker compose stop mosquitto 2>/dev/null || true
    kill "$REPLAY_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo -e "\n${BOLD}=== Phase 1 isolated test ===${NC}\n"

# ── 1. Python dependency ─────────────────────────────────────────────────────
info "Checking paho-mqtt..."
if python3 -c "import paho.mqtt.client" 2>/dev/null; then
    pass "paho-mqtt available"
else
    info "Installing paho-mqtt..."
    pip install -q paho-mqtt
    python3 -c "import paho.mqtt.client" && pass "paho-mqtt installed" || { fail "paho-mqtt install failed"; exit 1; }
fi

# ── 2. Start mosquitto in test mode ─────────────────────────────────────────
info "Starting mosquitto (test mode)..."
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d mosquitto
sleep 3

if docker compose ps mosquitto | grep -q "Up\|running"; then
    pass "mosquitto container running"
else
    fail "mosquitto container failed to start"
    docker compose logs mosquitto
    exit 1
fi

# ── 3. Anonymous connect must be rejected ────────────────────────────────────
info "Testing anonymous connection is rejected..."
if docker run --rm --network host eclipse-mosquitto:2 \
       mosquitto_pub -h localhost -p 1883 -t test -m x 2>&1 | grep -qi "refused\|not authorised\|error"; then
    pass "Anonymous connection rejected"
else
    fail "Anonymous connection was NOT rejected — auth is broken"
fi

# ── 4. Authenticated round-trip ─────────────────────────────────────────────
info "Testing authenticated round-trip..."
docker run --rm --network host eclipse-mosquitto:2 \
    mosquitto_pub -h localhost -p 1883 -u esp32-bridge -P test_esp32_pass \
    -t victron/selftest -m '{"ok":1}' 2>&1

RECEIVED=$(docker run --rm --network host eclipse-mosquitto:2 \
    mosquitto_sub -h localhost -p 1883 -u decoder -P test_decoder_pass \
    -t victron/selftest -C 1 -W 3 2>&1 || true)

if echo "$RECEIVED" | grep -q '"ok"'; then
    pass "Authenticated MQTT round-trip succeeded"
else
    fail "Authenticated MQTT round-trip failed (got: $RECEIVED)"
fi

# ── 5. Replay tool publishes fixture data ────────────────────────────────────
info "Starting fixture replay..."
python3 decoder/replay.py --loop &
REPLAY_PID=$!
sleep 2

if kill -0 "$REPLAY_PID" 2>/dev/null; then
    pass "replay.py started (pid $REPLAY_PID)"
else
    fail "replay.py exited unexpectedly"
fi

# ── 6. Verify victron/raw messages are flowing ──────────────────────────────
info "Subscribing to victron/raw for 5 seconds..."
MESSAGES=$(docker run --rm --network host eclipse-mosquitto:2 \
    mosquitto_sub -h localhost -p 1883 -u decoder -P test_decoder_pass \
    -t 'victron/raw' -W 5 2>&1 || true)

COUNT=$(echo "$MESSAGES" | grep -c '"mac"' || true)
if [[ "$COUNT" -gt 0 ]]; then
    pass "victron/raw flowing — received $COUNT message(s)"
    echo "$MESSAGES" | head -3 | sed 's/^/    /'
else
    fail "No messages on victron/raw (replay or broker problem)"
fi

# ── 7. Verify all 3 fixture device MACs appear ───────────────────────────────
for MAC in "AA:BB:CC:DD:EE:01" "AA:BB:CC:DD:EE:02" "AA:BB:CC:DD:EE:03"; do
    if echo "$MESSAGES" | grep -q "$MAC"; then
        pass "MAC $MAC seen in victron/raw"
    else
        fail "MAC $MAC NOT seen in victron/raw"
    fi
done

# ── Result ───────────────────────────────────────────────────────────────────
echo ""
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "${BOLD}${GREEN}All Phase 1 tests passed.${NC}"
    echo ""
    echo "Acceptance criteria met:"
    echo "  ✓ MQTT broker running with authentication"
    echo "  ✓ Anonymous connections rejected"
    echo "  ✓ Authenticated round-trip working"
    echo "  ✓ Fixture replay publishing to victron/raw"
    echo "  ✓ All 3 device MACs present in fixture stream"
    echo ""
    echo "Next: run ./setup.sh 1 on this server, then flash the ESP32."
else
    echo -e "${BOLD}${RED}Some tests FAILED — see output above.${NC}"
    exit 1
fi
