# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What This Project Is

A **self-hosted solar energy monitoring system** for Victron BLE devices. No cloud dependency — all data is local.

**Installation 1 (Home):** 2× Victron MPPT charge controllers + 1× Victron Battery Sense, bridged to the server via an ESP32 over MQTT.

**Installation 2 (Garage):** 2× Victron 150/75 MPPT charge controllers + 1× LiTime BMS battery. The Linux server is physically next to these devices, so it scans BLE directly — no ESP32 needed.

Both installations write to the same InfluxDB instance (tagged by `site`), served by one API and one dashboard with a site selector.

---

## Current Status

| Phase | Description | Status |
|---|---|---|
| 1 | ESP32 firmware (ESPHome, passive BLE scanner) | ✅ Complete |
| 2 | Server infrastructure (Docker, Mosquitto, InfluxDB, downsampling) | ✅ Complete |
| 3 | BLE decoder (MQTT → InfluxDB for home ESP32 path) | ✅ Complete |
| 4 | API service (FastAPI, bucket stitching, daily yield) | ✅ Complete |
| 5 | Dashboard UI (animated SVG flow, Chart.js, offline detection) | ✅ Complete |
| 6 | Auth + TLS (nginx, oauth2-proxy, Google OAuth) | ✅ Complete |
| 7 | Multi-site foundation (`sites.json`, `site` tag, `/api/v1/sites`) | ✅ Complete — 35/35 tests |
| 7.5 | Historical data migration (backfill `site=home` on all records) | ✅ Complete — 10.9M raw + 27k medium + 2.3k hourly records |
| 8 | Linux BLE bridge for garage Victron MPPTs | ✅ Complete — 16 unit + 4 integration tests |
| 9 | LiTime BMS support (active BLE poll, `battery` measurement) | Code complete — hardware verify pending |
| 10 | Multi-site API (full test coverage, battery endpoint) | Not started |
| 11 | Dashboard multi-site UI (site selector, BMS widget, topology switching) | Not started |
| 12 | Setup.sh multi-site wizard + dashboard device management (add/remove BMS) | Not started |

---

## Phase 8 — Complete

**Goal:** Garage Victron MPPTs decode and write to InfluxDB without an ESP32, using the Linux server's built-in Bluetooth adapter (`hci0`).

**Result:** 16/16 unit tests + 4/4 integration tests pass.

**Root cause of previous 401 failures:** `test_phase8.sh` was passing the hardcoded test token (`test_influx_token_aabbccdd1122`) to the integration test container instead of the real production token. Fixed by reading `INFLUXDB_TOKEN` from `.env`.

**Infrastructure facts:**
- Linux server: `<SERVER_USER>@<SERVER_IP>` (see your local notes — not committed)
- Production stack is running (do not `docker compose down`)
- InfluxDB only accessible inside Docker network as `http://influxdb:8086`
- Token lives in `.env` as `INFLUXDB_TOKEN=<64-char hex>`
- Org name is `home`; test bucket is `victron_test`

**Test command (run on Linux server):**
```bash
cd ~/victron-dashboard && bash test_phase8.sh 2>&1 | tail -40
```

---

## Full System Design (Summary)

The authoritative spec is `multi-site-design.md` (v5.0). Summary of what we're building:

```
── Home installation ─────────────────────────────────────────────────────
 Victron BLE devices
   → ESP32 (ESPHome, passive scanner, WiFi)
   → MQTT: victron/home/raw
                                    ↘
── Garage installation ────────────   mosquitto (single broker)
 Victron 150/75 MPPTs (BLE)           ↓
   → ble-bridge (bleak passive)    ble-decoder (victron/+/raw)
   → MQTT: victron/garage/raw ↗        ↓
                                   InfluxDB (site tag on all points)
 LiTime BMS (BLE active poll)          ↓
   → ble-bridge (direct write) ─── solar-api (multi-site)
                                       ↓
                                  nginx + oauth2-proxy
                                       ↓
                                  Dashboard (site selector)
```

### Phase 9 — LiTime BMS
- `ble-bridge/drivers/litime.py`: active BLE connection, poll every 5s, parse 105-byte response
- Writes to `battery` measurement with `soc`, `soh`, `cycles`, `temperature`, `cell_min`, `cell_max`, `cell_avg`, `battery_voltage`, `battery_current`
- On disconnect: exponential backoff reconnect [5, 10, 20, 40s]
- New API endpoint: `GET /api/v1/battery?site=&device=`

### Phase 10 — Multi-site API
- `GET /api/v1/sites` returns site list with `ui` config block per site
- All endpoints accept `?site=` filter
- `api/tests/test_endpoints.py` expanded to 30+ tests covering both sites and battery endpoint
- `api/seed_test_data.py` seeds `solar` + `battery` measurements for both sites

### Phase 11 — Dashboard multi-site UI
- Site selector appears in header when >1 site configured
- Energy flow SVG topology driven by `ui.mppt_count`, `ui.show_loads` from `/api/v1/sites`
- Battery widget: two variants — `sense` (voltage+temp) and `bms` (SOC bar, cell delta, cycles)
- Garage site: no load node, BMS battery widget
- Home site: load node visible, sense battery widget

### Phase 12 — Setup.sh multi-site wizard
- Interactive wizard: asks number of sites, IDs, labels, bridge types, device MACs+keys
- Writes `config/sites.json`
- Checks for hci0; installs `firmware-atheros` if absent
- Pre-commit hook: blocks commits containing real MAC addresses
- Fully idempotent
- **Dashboard device management UI** — add and remove BMS devices from the dashboard (not just CLI wizard). User must confirm which physical battery they're adding (show live SOC/V/temp) or confirm removal before changes are saved to `sites.json` and ble-bridge is restarted.

---

## Architecture

```
Internet → nginx (8443 TLS) → oauth2-proxy (Google OAuth) → solar-api (FastAPI :8080)
                                                                  ↕
                                              influxdb (4 buckets: raw/medium/hourly/test)
                                                         ↑               ↑
                                    ble-decoder (MQTT→InfluxDB)   ble-bridge (BLE→InfluxDB)
                                                ↑                       ↑
                                    mosquitto (MQTT :1883)        bleak + BlueZ (hci0)
                                                ↑
                                    ESP32 victron-bridge (home)
```

**Data flow (home):** Victron BLE → ESP32 → MQTT `victron/home/raw` → ble-decoder → InfluxDB `solar` measurement, `site=home`

**Data flow (garage):** Victron BLE → ble-bridge (bleak passive scan) → InfluxDB `solar` measurement, `site=garage`
LiTime BMS → ble-bridge (bleak active poll) → InfluxDB `battery` measurement, `site=garage`

---

## InfluxDB Data Model

**Tags on every point:** `site` (`home`, `garage`), `device` (device ID from sites.json), `label` (human name)

**`solar` measurement (Victron MPPT + Battery Sense):**
- Instantaneous: `pv_power`, `pv_voltage`, `battery_voltage`, `charge_current`, `load_current`, `load_power`, `load_state`, `charge_state`, `error_code`, `temperature`
- Cumulative (use MAX in aggregation): `yield_today`, `yield_total`

**`battery` measurement (LiTime / EG4 BMS):**
- `soc`, `soh`, `cycles`, `temperature`, `battery_voltage`, `battery_current`, `cell_min`, `cell_max`, `cell_avg`

**4 buckets:** `victron` (30d raw), `victron_medium` (1yr 5-min), `victron_hourly` (∞ 1-hr), `victron_test` (24h)

---

## API Contract

| Endpoint | Description |
|---|---|
| `GET /api/v1/sites` | Site list with ui config |
| `GET /api/v1/devices?site=` | Device list, online state, per site |
| `GET /api/v1/current?site=` | Latest reading per device |
| `GET /api/v1/history?site=&device=&range=` | Bucket-stitched time series |
| `GET /api/v1/daily?site=&tz_offset_hours=N` | Daily yield totals |
| `GET /api/v1/battery?site=&device=` | Latest BMS snapshot (Phase 9+) |
| `GET /health` | Liveness check |

---

## Key Files

| Path | Purpose |
|---|---|
| `ble-bridge/ble_bridge.py` | Main bridge: fixture mode + production BLE scanner |
| `ble-bridge/drivers/victron.py` | Victron BLE decode logic |
| `ble-bridge/tests/test_decoder.py` | 16 unit tests (no InfluxDB needed) |
| `ble-bridge/tests/test_fixture_replay.py` | 4 integration tests (requires InfluxDB) |
| `ble-bridge/fixtures/sites_fixture.json` | Test sites.json: garage with 2 MPPTs |
| `ble-bridge/fixtures/ble_packets.jsonl` | 4 pre-decoded test packets |
| `decoder/decoder.py` | MQTT→InfluxDB for home ESP32 path |
| `api/main.py` | FastAPI service |
| `api/static/index.html` | Dashboard UI |
| `docker-compose.yml` | Production stack |
| `docker-compose.test.yml` | Test overrides (isolated bucket, fixture mounts) |
| `test_phase8.sh` | Phase 8 test script — run on Linux server |
| `config/sites.json` | Per-device MACs + keys (gitignored) |
| `multi-site-design.md` | Authoritative design spec (v5.0) |

---

## Working Style

**Never declare a phase complete unless every acceptance criterion in `multi-site-design.md` §8 is met** — including real-hardware steps where specified.

**Never push to GitHub unless the user explicitly requests it.** Commit locally freely.

**Never give the user a list of commands to run manually.** Everything goes into scripts. The user runs one thing and gets PASS/FAIL. When Linux server output is needed, ask for ONE command.

**ALWAYS add automated tests with every code change.** Every commit backed by new or updated tests.

---

## Credentials

**Production credentials:** generated by `setup.sh` (never hardcoded). In `.env` and `esp32/secrets.yaml` (both gitignored).

**Test credentials:** hardcoded in `docker-compose.test.yml`:
- InfluxDB test token: `test_influx_token_aabbccdd1122` (only valid when InfluxDB is freshly initialized via test overlay — NOT valid against production InfluxDB)
- MQTT test pass: `test_decoder_pass`

**Critical:** Integration tests against a production InfluxDB instance must use the real token from `.env`, NOT the hardcoded test token.

---

## Key Design Decisions

**ESP32 BLE duty cycle is 50%** — time-slices the 2.4 GHz antenna. Do not change; continuous scan causes WiFi drops.

**Server-side timestamps only** — decoder and ble-bridge timestamp on receipt. ESP32/BLE devices never provide timestamps.

**Field-specific downsampling** — `yield_today` and `yield_total` use MAX; all others use MEAN. Mixing is a data integrity bug.

**500-point ceiling** — API computes intervals to cap responses at 500 points per series.

**BRIDGE_OFFLINE vs DEVICE_OFFLINE** — distinct states. Do not conflate.

**OAuth at nginx** — `solar-api` has no auth layer. Security depends on nginx routing all traffic through oauth2-proxy.

**ble-bridge uses bridge network + privileged + dbus mount** (NOT host networking) — keeps InfluxDB internal, connects via Docker network `http://influxdb:8086`. BlueZ access via `/var/run/dbus` socket mount.

**InfluxWriter.write() silently catches exceptions** — write failures are invisible to callers. `run_fixture_mode` count return is a local counter, NOT confirmation of successful DB write. Any test that relies on this count to assert data was written is wrong.

---

## Known Gotchas

**`docker compose run -e KEY=val` DOES override compose file env** — confirmed working; if 401 persists it is NOT a precedence bug.

**`docker compose run --no-deps`** does not start dependent services but the container CAN still reach already-running services on the same Docker network.

**influxdb-client `query_api().query()`** requires org to match exactly. Org name in production is `home`.

**`((N++))` in bash with `set -e`** returns exit code 1 when N=0. Use `N=$((N+1))`.

**Phase 3 decoder test format:** `{"mac": "...", "raw": {field: value}}` — pre-decoded, no decryption. Different from production `{"mac": "...", "data": "hexbytes"}` format.
