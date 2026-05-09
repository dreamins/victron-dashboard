# Victron Solar Monitor

A self-hosted solar energy dashboard for Victron BLE devices — no cloud, no Victron servers, all data stays local. An ESP32 passively sniffs BLE advertisements, relays them over MQTT to a Linux server, and a web dashboard shows live and historical data behind Google OAuth.

> **Hardware used:** 2× SmartSolar MPPT charge controllers + 1× Smart Battery Sense + ESP32-S3

---

## Screenshots

### Live Energy Flow (dark mode)
![Dashboard dark mode — animated energy flow between solar panels, charge controllers, and battery](docs/screenshots/dashboard-dark.png)

### Live Energy Flow (light mode)
![Dashboard light mode](docs/screenshots/dashboard-light.png)

### Historical Charts
![Chart.js power and voltage charts with selectable time ranges](docs/screenshots/charts.png)

### Daily Yield
![Bar chart of daily solar yield per device](docs/screenshots/daily-yield.png)

### Google OAuth Login
![Google sign-in gate before the dashboard is accessible](docs/screenshots/oauth-login.png)

---

## Features

- **Animated SVG energy flow** — live arrows show power moving from solar → charger → battery → load
- **Per-device cards** — PV power, battery voltage, charge current, load, yield today/total, charge state
- **Historical charts** — 1 h / 6 h / 24 h / 7 d / 30 d ranges with automatic resolution stitching (1-second live data always shown for the last hour regardless of range)
- **Daily yield bar chart** — timezone-aware; `yield_today` never splits across UTC midnight
- **Dark / light theme** — toggle in the header
- **Bridge vs device offline distinction** — banner for ESP32/MQTT down, greyed card for a silent device
- **Google OAuth gate** — only your email can reach the dashboard; all others get 403
- **HTTPS on a non-standard port** — Let's Encrypt TLS, nginx, port 8443 (reduces scanner noise vs 443)
- **No cloud dependency** — Victron devices are unaware anything is listening

---

## Architecture

```
Internet
   │
   ▼  port 8443 (TLS)
nginx
   │
   ▼
oauth2-proxy  ←── Google OAuth (checks allowed_emails)
   │
   ▼
solar-api (FastAPI :8080)
   │  bucket-stitched queries
   ▼
InfluxDB 2.x
  ├── victron         (raw, 30 d, ~1 s resolution)
  ├── victron_medium  (5-min aggregates, 1 year)
  └── victron_hourly  (1-hour aggregates, 10 years)
   ▲
ble-decoder  ←── decrypts AES-128-CTR, timestamps on receipt
   ▲
mosquitto (MQTT broker, LAN-only :1883, auth required)
   ▲
ESP32 victron-bridge (passive BLE scanner, ESPHome firmware)
   ▲
Victron devices (BLE advertisements, ~1/second per device)
```

**Data flow:** Victron devices broadcast encrypted BLE advertisements → ESP32 relays raw bytes + MAC to `victron/raw` MQTT topic → `ble-decoder` decrypts (AES-128-CTR, per-device key from `config/devices.json`) and writes to InfluxDB with server-side timestamp → `solar-api` stitches buckets for resolution continuity → dashboard polls every 2 seconds.

---

## Hardware Requirements

| Item | Notes |
|---|---|
| ESP32-S3 dev board | Tested: `esp32-s3-devkitc-1`. Must be physically near the Victron devices (BLE range, ~10 m). |
| Linux server | Ubuntu/Debian with Docker. Can be a Raspberry Pi, mini PC, or VPS. |
| Windows PC | For the one-time ESP32 flash only (ESPHome CLI). |
| Victron BLE devices | Any combination of SmartSolar MPPT, Battery Sense, or other `victron-ble`-supported devices. |

---

## Installation

### Prerequisites

**Server (Linux):**
```bash
# Docker + Docker Compose
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in
```

**Windows PC (for ESP32 flash only):**
```powershell
pip install esphome
```

---

### Phase 1 — Flash the ESP32

1. Clone the repo on your **Linux server** and run the setup wizard:
   ```bash
   git clone https://github.com/dreamins/victron-dashboard ~/victron-dashboard
   cd ~/victron-dashboard
   ./setup.sh 1
   ```
   This generates WiFi + MQTT credentials in `esp32/secrets.yaml`.

2. Copy `esp32/secrets.yaml` to your **Windows PC** (same folder as the repo clone).

3. Plug in the ESP32-S3 via USB. Open Device Manager → Ports to find the COM port (e.g. `COM5`).

4. Flash:
   ```powershell
   cd esp32
   esphome run victron-bridge.yaml --device COM5
   ```

5. After flashing, place the ESP32 **near your Victron devices** (within ~10 m). Power it via USB. The green LED and web UI at `http://<esp32-ip>/` confirm it is online.

> The BLE scanner runs at 50% duty cycle — this is intentional. It prevents WiFi disconnects caused by antenna contention. Do not change it.

---

### Phase 2 — Server Infrastructure

```bash
./setup.sh 2
```

Starts Mosquitto and InfluxDB, creates 3 storage buckets (`victron_medium`, `victron_hourly`, `victron_test`), and installs 4 Flux downsampling tasks.

---

### Phase 3 — Device Discovery & Keys

Each Victron device encrypts its BLE advertisements with a unique AES-128 key. You need to get that key from the VictronConnect app.

1. Run discovery (listens to MQTT for 60 seconds):
   ```bash
   ./setup.sh 3
   ```
   Output looks like:
   ```
   Found: SolarCharger       | MAC: AA:BB:CC:DD:EE:01 | Suggested ID: solarcharg_ee01
   Found: SolarCharger       | MAC: AA:BB:CC:DD:EE:02 | Suggested ID: solarcharg_ee02
   Found: SmartBatterySense  | MAC: AA:BB:CC:DD:EE:03 | Suggested ID: smartbattery_ee03
   ```

2. For each device, open **VictronConnect** on your phone → tap the device → ⋮ menu → **Product Info** → copy the **Encryption Key** (32 hex characters).

3. The script prompts for an ID, human label, and key for each device, then writes `config/devices.json` (gitignored).

---

### Phase 4 — BLE Decoder

```bash
./setup.sh 4
```

Starts the `ble-decoder` container. Verify live data is flowing:

```bash
docker compose logs -f ble-decoder
# Should show: "Wrote 3 points to InfluxDB" every ~1 second
```

---

### Phase 5 — Dashboard

```bash
./setup.sh 5
```

Starts `solar-api`. The dashboard is now available **on your LAN** at `http://<server-ip>:8080/`.

---

### Phase 6 — Auth + TLS (internet access)

This phase puts the dashboard behind Google OAuth and HTTPS so you can access it from anywhere.

**Before running this phase you need:**

1. A domain name pointing to your home public IP (A record, e.g. `solar.yourdomain.com`).
2. A Google Cloud OAuth 2.0 Client ID — [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials).
3. Port **8443 TCP** forwarded on your router to the server's LAN IP.

```bash
./setup.sh 6
```

The wizard asks for:
- Your domain name
- DNS provider + API key (for Let's Encrypt DNS-01 certificate — no port 80 required)
- Google OAuth Client ID and Secret
- The email address that will be allowed to log in

**Google Cloud Console setup:**

In the OAuth credential, add this exact redirect URI:
```
https://your.domain.com:8443/oauth2/callback
```

**Router port-forward (Ubiquiti UniFi example):**

Settings → Security → Port Forwarding → Add rule:
| Field | Value |
|---|---|
| Name | victron-dashboard |
| Interface | WAN |
| Port | 8443 |
| Forward IP | `<server LAN IP>` |
| Forward Port | 8443 |
| Protocol | TCP |

**Verify:**
```bash
curl -sI https://your.domain.com:8443/health
# → HTTP/2 200 + Strict-Transport-Security header
# → browser shows Google sign-in before reaching this
```

---

## Running Tests

Each phase has an isolated test that uses a separate Docker project and a dedicated `victron_test` InfluxDB bucket. Production data is never touched.

```bash
./test_phase1.sh   # MQTT auth
./test_phase2.sh   # InfluxDB buckets + downsampling tasks
./test_phase3.sh   # BLE decoder with fixture replay
./test_phase4.sh   # API endpoints
./test_phase5.sh   # Dashboard automated checks (leaves browser URL open)
```

---

## Development

```bash
# Replay saved fixture data (no real hardware needed)
python3 decoder/replay.py

# Seed InfluxDB with synthetic data for UI testing
python3 api/seed_test_data.py

# Run API test suite
python3 -m pytest api/tests/ -v

# Full production stack
docker compose up -d

# Test stack (isolated project, test credentials)
docker compose --project-name victron-test \
  -f docker-compose.yml -f docker-compose.test.yml up -d
```

---

## Adding a Screenshot

1. Take a screenshot of `https://your.domain.com:8443/` (or `http://<server-ip>:8080/` for LAN access).
2. Save it to `docs/screenshots/dashboard-dark.png` (and the other filenames referenced above).
3. Commit and push.

---

## License

MIT
