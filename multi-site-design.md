# Multi-Site Solar Monitor — System Design v5.0

**Version:** 5.0
**Status:** Design — pending build
**Supersedes:** victron-system-design.md (v4.3) for new development; v4.3 remains the reference for Phases 1–6.

---

## 1. What Changes and Why

The v4.3 system serves one installation: three Victron BLE devices bridged by an ESP32, decoded server-side, stored in InfluxDB, served through a single-site dashboard. It works well but has several rigid assumptions baked into every layer:

- `devices.json` is a flat list with no site concept
- The decoder subscribes to one hardcoded MQTT topic (`victron/raw`)
- Every InfluxDB point goes into the `victron` bucket with no site tag
- The dashboard SVG is hardcoded for 2 MPPTs + battery + loads
- `setup.sh` is a single-installation wizard

The second installation (co-located with the Linux server) differs in three important ways:

1. **No ESP32 needed.** The Linux server is physically next to the Victron devices, so it can scan BLE directly using the built-in Atheros adapter (hci0, confirmed working after `firmware-atheros` install).
2. **No load output.** The 150/75 MPPTs have no load terminals — the dashboard must not show a load node for this site.
3. **LiTime BMS.** The battery has a BLE BMS using a custom binary protocol (not Victron's encrypted broadcast). It requires an active BLE connection and polling, not passive scanning.

This document defines the refactored architecture that supports both installations and is extensible to more.

---

## 2. Target Architecture

```
── Home installation ────────────────────────────────────────────────────
 Victron devices (BLE broadcast, encrypted)
   → ESP32 (ESPHome firmware, passive scanner, WiFi)
   → MQTT broker: victron/home/raw
                                    ↘
── Garage installation ──────────────  mosquitto (single shared broker)
 Victron 150/75 devices (BLE broadcast)  ↓
   → ble-bridge (bleak passive scanner)  ble-decoder (victron/+/raw)
   → MQTT broker: victron/garage/raw ↗       ↓
                                         InfluxDB (site tag on all points)
 LiTime BMS (BLE active, request/reply)       ↓
   → ble-bridge (bleak polling client)   solar-api (multi-site)
   → writes direct to InfluxDB ─────────────↗ ↓
                                          nginx + oauth2-proxy
                                               ↓
                                          Dashboard (site selector)
```

**Two ingestion paths:**

| Path | Used for | Transport |
|---|---|---|
| ESP32 → MQTT → ble-decoder | Remote Victron devices (home) | WiFi + MQTT |
| ble-bridge → InfluxDB | Local Victron + LiTime BMS (garage) | Direct BLE + HTTP |

The MQTT path remains unchanged for backward compatibility with the existing ESP32. The direct path is used only for devices the server can reach by BLE.

---

## 3. Configuration: `config/sites.json`

Replaces `config/devices.json`. **Gitignored** — never committed. Schema defined by `config/sites.json.example`.

```json
{
  "sites": [
    {
      "id": "home",
      "label": "Home Solar",
      "tz_offset_hours": 4,
      "bridge": "esp32",
      "ui": {
        "show_loads": true,
        "battery_display": "sense",
        "mppt_count": 2
      },
      "devices": [
        {
          "id": "mppt_1",
          "label": "MPPT South",
          "type": "victron_mppt",
          "mac": "AA:BB:CC:DD:EE:FF",
          "key": "hexkey32chars"
        },
        {
          "id": "mppt_2",
          "label": "MPPT East",
          "type": "victron_mppt",
          "mac": "AA:BB:CC:DD:EE:FF",
          "key": "hexkey32chars"
        },
        {
          "id": "battery_sense",
          "label": "Battery",
          "type": "victron_battery_sense",
          "mac": "AA:BB:CC:DD:EE:FF",
          "key": "hexkey32chars"
        }
      ]
    },
    {
      "id": "garage",
      "label": "Garage Solar",
      "tz_offset_hours": 4,
      "bridge": "linux_ble",
      "ui": {
        "show_loads": false,
        "battery_display": "bms",
        "mppt_count": 2
      },
      "devices": [
        {
          "id": "mppt_1",
          "label": "MPPT-1",
          "type": "victron_mppt",
          "mac": "AA:BB:CC:DD:EE:FF",
          "key": "hexkey32chars"
        },
        {
          "id": "mppt_2",
          "label": "MPPT-2",
          "type": "victron_mppt",
          "mac": "AA:BB:CC:DD:EE:FF",
          "key": "hexkey32chars"
        },
        {
          "id": "litime_main",
          "label": "LiTime Battery",
          "type": "litime_bms",
          "mac": "AA:BB:CC:DD:EE:FF"
        }
      ]
    }
  ]
}
```

**Field reference:**

| Field | Values | Meaning |
|---|---|---|
| `bridge` | `esp32`, `linux_ble` | How Victron packets reach the server |
| `ui.show_loads` | bool | Whether to render load output node in flow diagram |
| `ui.battery_display` | `sense`, `bms` | Drives battery widget type |
| `ui.mppt_count` | 1–4 | How many MPPT nodes to render |
| `type` | see below | Device protocol and decoder logic |

**Device types:**

| Type | Protocol | Decoder |
|---|---|---|
| `victron_mppt` | AES-128-CTR BLE broadcast | `victron-ble` library |
| `victron_battery_sense` | AES-128-CTR BLE broadcast | `victron-ble` library |
| `victron_inverter` | AES-128-CTR BLE broadcast | `victron-ble` library (future) |
| `litime_bms` | Custom 105-byte binary, BLE active | `ble-bridge` driver |
| `eg4_bms` | Modbus RTU 83-byte, BLE active | `ble-bridge` driver (future) |

---

## 4. Components

### 4.1 ble-bridge (new service, Docker)

**Responsibility:** BLE scanning and polling for the local (`linux_ble`) site.

Runs only on a machine physically near the Victron devices and BMS. In this deployment that is the production server itself (hci0 confirmed working).

**Two concurrent subsystems within one process:**

**A. Passive Victron scanner**
- Uses `bleak` + `victron-ble` to scan for manufacturer data matching Victron company ID (0x02E1)
- Decrypts using per-device keys from `sites.json`
- Publishes decoded field dicts to InfluxDB `solar` measurement with `site` and `device` tags
- Never makes an outbound BLE connection (passive scan, same model as ESP32)

**B. Active BMS poller**
- For each `litime_bms` (or `eg4_bms`) device in the configured site, maintains a persistent bleak BLE client connection
- LiTime: sends `c_13` request every 5 s, parses 105-byte response
- On disconnect: exponential backoff reconnect [5, 10, 20, 40 s]
- Writes to InfluxDB `battery` measurement with `site` and `device` tags

**Docker configuration:**

```yaml
ble-bridge:
  build: ./ble-bridge
  privileged: true          # required for hci0 access inside Docker
  network_mode: host        # avoids NAT for direct InfluxDB HTTP
  volumes:
    - ./config/sites.json:/app/sites.json:ro
    - /var/run/dbus:/var/run/dbus  # BlueZ dbus socket
  environment:
    - BRIDGE_SITE=${BRIDGE_SITE}   # which site this instance serves
    - INFLUX_URL=http://localhost:8086
    - INFLUX_TOKEN=${INFLUXDB_TOKEN}
    - INFLUX_ORG=home
  restart: unless-stopped
```

`BRIDGE_SITE` selects which site's devices to scan. A second physical server would set `BRIDGE_SITE=home` and scan for the home devices instead. (That scenario uses an ESP32 in v5.0; ble-bridge is only used for the garage site today.)

**Publishes to:** InfluxDB directly (no MQTT involvement)
**Measurements written:** `solar` (Victron), `battery` (LiTime/EG4)
**Tags on every point:** `site`, `device`, `label`

### 4.2 ble-decoder (refactored from `decoder/`)

**Responsibility:** MQTT → InfluxDB for remote ESP32 bridges.

Changes from v4.3:
- Loads `sites.json` instead of `devices.json`
- Subscribes to `victron/+/raw` (MQTT wildcard) instead of `victron/raw`
- Extracts `site_id` from topic path (`victron/home/raw` → `site=home`)
- Adds `.tag("site", site_id)` to every InfluxDB point
- Builds MAC→device lookup across all sites that use `bridge: esp32`
- Otherwise identical logic (test format, retry buffer, exponential backoff)

ESP32 firmware update required (one-time reflash):
- Add `site_id` variable to `secrets.yaml`
- Change publish topic from `victron/raw` to `victron/${site_id}/raw`

### 4.3 solar-api (refactored)

**Responsibility:** Serve time-series data for all sites over HTTP.

Changes from v4.3:
- Loads `sites.json` at startup, exposes site metadata
- New endpoint: `GET /api/v1/sites`
- All existing endpoints accept optional `?site=` query param
- When `?site=` specified: InfluxDB queries add `|> filter(fn: (r) => r.site == "...")` 
- When `?site=` absent: returns data across all sites (backward compatible)
- Extended `VALID_FIELDS`: adds `soc`, `soh`, `cycles`, `battery_current`, `cell_min`, `cell_max`, `cell_avg`
- New endpoint: `GET /api/v1/battery?site=&device=` — latest BMS reading (SOC, SOH, cell stats, cycles)
- `GET /api/v1/devices` returns per-site device lists with `device_type` and `bridge` in metadata
- `GET /api/v1/daily?site=` filters yield totals by site

### 4.4 Dashboard (`api/static/index.html`)

**Responsibility:** Multi-site web UI.

Changes from v4.3:
- **Site selector:** appears in header when >1 site is configured; hidden for single-site
- **Energy flow SVG:** topology driven by `ui` block from `/api/v1/sites`:
  - `mppt_count` → render 1–4 MPPT nodes
  - `show_loads` → show/hide load output node and flow arrows
  - `battery_display` → switch battery widget
- **Battery widget variants:**
  - `sense`: voltage + temperature (existing)
  - `bms`: SOC percentage bar, cell delta (max−min), temperature, cycles, charge/discharge current
- **Chart field list:** filtered to fields present for the selected site
- **Per-site online logic:** `BRIDGE_OFFLINE` and `DEVICE_OFFLINE` remain distinct; bridge online check uses site-specific last-seen window

### 4.5 ESP32 firmware (minor update)

One change: the publish topic is parameterized. `secrets.yaml` gains one field:

```yaml
site_id: home
```

`victron-bridge.yaml` changes `victron/raw` to `victron/${site_id}/raw` in the MQTT publish action. Everything else is unchanged.

This requires a one-time OTA flash or USB reflash of the home ESP32.

### 4.6 `setup.sh` (multi-site wizard)

The existing 6-phase wizard is extended. New Phase 7 runs before Phase 1 if `config/sites.json` doesn't exist.

Phase 7 wizard flow:
1. Ask: how many sites? (1–4)
2. For each site: ID, label, timezone offset, bridge type (esp32 / linux_ble)
3. For each site: add devices (type, MAC, key if Victron, MAC-only if LiTime)
4. Write `config/sites.json`
5. For each site with `bridge: linux_ble`, verify hci0 is present; if not, install `firmware-atheros` and retry
6. For each site with `bridge: esp32`, write `esp32/secrets.yaml` with that site's `site_id`
7. Write `.env` with `BRIDGE_SITE=<linux_ble site id>`

`setup.sh` is idempotent: re-running skips already-completed steps. Re-running with an existing `sites.json` offers to keep or regenerate it.

---

## 5. InfluxDB Data Model

### Measurements

| Measurement | Written by | Contains |
|---|---|---|
| `solar` | ble-decoder (ESP32 path), ble-bridge (Linux path) | Victron MPPT and Battery Sense readings |
| `battery` | ble-bridge | LiTime / EG4 BMS readings |

### Tags (on every point)

| Tag | Example | Source |
|---|---|---|
| `site` | `home`, `garage` | MQTT topic or `sites.json` config |
| `device` | `mppt_1`, `litime_main` | `sites.json` device id |
| `label` | `MPPT South` | `sites.json` device label |

### Fields: `solar` measurement (unchanged from v4.3)

`pv_power`, `pv_voltage`, `battery_voltage`, `charge_current`, `load_current`, `load_power`, `load_state`, `charge_state`, `yield_today`, `yield_total`, `error_code`, `temperature`, `battery_current`, `charger_error`

### Fields: `battery` measurement (new)

| Field | Type | Unit | Notes |
|---|---|---|---|
| `battery_voltage` | float | V | Pack voltage |
| `battery_current` | float | A | + = charging, − = discharging |
| `soc` | float | % | State of charge |
| `soh` | float | % | State of health |
| `cycles` | float | — | Cycle count |
| `temperature` | float | °C | Cell temperature sensor |
| `cell_min` | float | V | Minimum cell voltage this sample |
| `cell_max` | float | V | Maximum cell voltage this sample |
| `cell_avg` | float | V | Mean cell voltage this sample |

Individual cell voltages (16 cells) are NOT stored in time-series — only min/max/avg. The live cell detail view on the dashboard reads from the latest BMS frame held in memory (via `/api/v1/battery`), not from InfluxDB history.

### Downsampling tasks

Existing 4 Flux tasks are updated to filter by measurement and pass the `site` and `device` tags through. No structural changes to bucket layout.

---

## 6. API Contract (v5.0 additions)

| Endpoint | New? | Description |
|---|---|---|
| `GET /api/v1/sites` | ✅ | Site list: id, label, bridge_type, ui config, device_type list |
| `GET /api/v1/devices?site=` | extended | Device list filtered by site; adds `device_type`, `site` fields |
| `GET /api/v1/current?site=` | extended | Latest reading per device, optional site filter |
| `GET /api/v1/history?site=&device=&field=&start=&interval=` | extended | Bucket-stitched series; site filter added |
| `GET /api/v1/daily?site=&days=&tz_offset=` | extended | Daily yield, site-filtered |
| `GET /api/v1/battery?site=&device=` | ✅ | Latest BMS snapshot: SOC, SOH, cycles, cell min/max/avg, temperature |
| `GET /health` | unchanged | InfluxDB liveness |

All existing v4.3 endpoints remain valid with no `site=` parameter (return all-site data), preserving backward compatibility.

---

## 7. Credential and Privacy Handling

**Never committed to git:**
- `config/sites.json` — contains MAC addresses and AES keys
- `config/sites.json.example` — shows schema with placeholder values only (`"mac": "AA:BB:CC:DD:EE:FF"`, `"key": "00112233..."`)
- `.env` — all runtime secrets
- `esp32/secrets.yaml` — WiFi + MQTT credentials

**Generated by `setup.sh`:**
- MQTT passwords (per bridge user)
- InfluxDB admin token
- OAuth cookie secret
- `sites.json` (from interactive wizard — user types MACs and keys, never stored in shell history)

**BLE names:** `sites.json` labels are free-form strings the user sets. The example file uses generic names (`"MPPT South"`, `"LiTime Battery"`). No real names, addresses, or identifying information appear in committed files.

**Git discipline:**
- `.gitignore` already covers `config/devices.json`; extends to `config/sites.json`
- Pre-commit hook (added by `setup.sh`) rejects commits containing MAC-address patterns (`[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}`) in tracked files

---

## 8. Build Phases

Phases 1–6 are complete (see v4.3 design doc). The following are new:

### Phase 7 — Multi-site foundation (decoder + config)

**Goal:** Home installation continues working with site tags; code is ready for a second site.

**Changes:**
- `config/sites.json` schema and `.example` file
- `decoder/decoder.py`: load sites.json, wildcard MQTT subscribe, add `site` tag
- ESP32 firmware: parameterize MQTT topic with `site_id`
- `.gitignore`: add `config/sites.json`
- Pre-commit hook: MAC pattern check
- `setup.sh` Phase 7 wizard (generates sites.json)
- Unit tests: decoder loads sites.json, maps MACs across sites, site tag in output

**Acceptance:**
- All 22 existing API tests pass unchanged
- Decoder test suite: 8/8 pass with sites.json-based config
- Home installation live: InfluxDB points have `site=home` tag
- `GET /api/v1/sites` returns site list (requires API changes from Phase 9)

### Phase 8 — Linux BLE bridge (Victron scanning)

**Goal:** Garage Victron MPPTs decode and write to InfluxDB without an ESP32.

**Changes:**
- `ble-bridge/` directory: `ble_bridge.py`, `drivers/victron.py`, `Dockerfile`, `requirements.txt`
- `docker-compose.yml`: add `ble-bridge` service (privileged, host network, dbus mount)
- Passive BLE scanner using `bleak` + `victron-ble`
- Writes to `solar` measurement with `site=garage`
- `setup.sh`: install `firmware-atheros` if hci0 absent; verify hci0 before starting stack

**Acceptance (requires garage Victron devices powered on):**
- `ble-bridge` container starts without error
- Within 30 s of Victron power-on, InfluxDB has points for garage MPPT devices
- `site=garage` tag present on all garage points
- Home installation unaffected
- `docker-compose.test.yml`: mock BLE scanner replays fixture packets, verifies InfluxDB writes (isolated test, no real hardware needed)

### Phase 9 — LiTime BMS support

**Goal:** LiTime battery state (SOC, SOH, cells, temperature, current) visible in InfluxDB and API.

**Changes:**
- `ble-bridge/drivers/litime.py` (ported from Gemini_Eg4_app with tests)
- Active BLE poller in `ble_bridge.py`: connect → poll → reconnect loop
- Writes to `battery` measurement
- `api/main.py`: `GET /api/v1/battery`, extended `VALID_FIELDS`, `battery` measurement queries
- Downsampling Flux tasks updated to include `battery` measurement

**Acceptance (requires LiTime BMS powered on):**
- InfluxDB `battery` bucket has LiTime points with correct SOC/SOH/cell values
- `GET /api/v1/battery?site=garage&device=litime_main` returns valid JSON
- On BMS disconnect, bridge reconnects within 40 s
- All LiTime protocol tests pass (ported from Gemini_Eg4_app)

### Phase 10 — Multi-site API

**Goal:** API serves site-filtered data with full test coverage.

**Changes:**
- `api/main.py`: `GET /api/v1/sites`, `?site=` on all endpoints
- `api/tests/test_endpoints.py`: extend to cover multi-site queries, site filter, battery endpoint
- `api/seed_test_data.py`: seeds both `solar` and `battery` measurements for both sites

**Acceptance:**
- 30+ tests pass (expanded from 22)
- `GET /api/v1/sites` returns both sites with correct metadata
- `?site=home` excludes garage data and vice versa
- `?site=` absent returns merged data (backward compat)
- Battery endpoint returns correct fields for LiTime device type

### Phase 11 — Dashboard multi-site UI

**Goal:** Dashboard shows site selector; energy flow and battery widgets adapt to site config.

**Changes:**
- `api/static/index.html`: site selector, topology switching, BMS battery widget
- Energy flow SVG: parameterized by mppt_count, show_loads
- Battery widget: two variants (sense / bms)
- Chart field list: filtered by available fields for selected site

**Acceptance (automated + manual browser verification):**
- 7/7 existing automated checks still pass
- Switching site selector changes all panels to selected site data
- Garage site: load node hidden, BMS battery widget shown (SOC bar, cell delta, cycles)
- Home site: load node visible, voltage+temp battery widget shown
- Manual browser verification on desktop and mobile

### Phase 12 — Setup.sh multi-site wizard

**Goal:** One-command deployment for both sites from a fresh server.

**Changes:**
- `setup.sh` Phase 7 wizard (multi-site config generation)
- Bluetooth prerequisite check + `firmware-atheros` auto-install
- Pre-commit MAC pattern hook installation
- Idempotency for all new phases

**Acceptance:**
- Run `setup.sh` on a freshly cloned repo → full stack running with both sites
- Re-run `setup.sh` → no destructive changes, all services still running
- Pre-commit hook blocks a commit containing a real MAC address

---

## 9. Constraints Carried Forward from v4.3

- **Server-side timestamps only.** ble-bridge timestamps on observation, never trusts device clocks.
- **Bucket stitching.** API still stitches raw/medium/hourly for history queries.
- **500-point ceiling.** Interval auto-computation unchanged.
- **BRIDGE_OFFLINE vs DEVICE_OFFLINE.** Distinction preserved per site.
- **OAuth at nginx.** API has no auth layer. All traffic through oauth2-proxy.
- **Field-specific aggregation.** yield_today / yield_total use MAX; all others use MEAN.
- **No push to GitHub without explicit user approval.**
- **No personal data in git.** MAC addresses, real labels, BLE names stay in gitignored files.

---

## 10. Open Items (not blocking Phase 7)

- **Victron inverter.** When added to the garage site, add `victron_inverter` type. The `victron-ble` library already supports GX Device and inverter device classes. Dashboard would gain an AC output node in the flow diagram.
- **EG4 BMS.** Driver is already in Gemini_Eg4_app. Port follows same pattern as LiTime.
- **Second ESP32 elimination (home site).** If a Linux machine with BT is ever available at the home site, the ESP32 can be retired and `bridge: linux_ble` used there too. No architectural change required.
- **Multi-user per-site access control.** Currently all OAuth-allowed emails see all sites. Per-site access is a future concern.
