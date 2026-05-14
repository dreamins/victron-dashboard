# BMS Troubleshooting — for next agent

This covers what to do when `ble-bridge` cannot connect to the LiTime BMS.

---

## How detection works

The passive Victron BLE scanner (`BleakScanner`) runs continuously and fires a callback for every nearby BLE advertisement. The BMS poller waits on a `asyncio.Event` that is set the moment the scanner sees the BMS MAC. Once seen, the scanner is stopped and `Device.Connect()` is called — guaranteed to succeed because BlueZ already has a device object from the advertisement.

Log to expect on healthy connect:
```
INFO [garage/litime_main] waiting for BMS to appear in BLE scan...
INFO [garage/litime_main] BMS detected, connecting now
INFO LiTime XX:XX:XX:XX:XX:XX connected
INFO [garage/litime_main] battery SOC=85%  V=13.20V  A=2.50A
```

---

## Symptom: "waiting for BMS to appear in BLE scan..." — never progresses

The BMS is not advertising. Most common causes, in order:

**1. BMS is connected to a phone app (most common)**
The LiTime/EG4 app on a phone holds the BLE connection. BLE peripherals stop advertising while connected. Close the app on every phone and tablet in the area. The bridge will connect within seconds.

Check: `bluetoothctl devices` on the server host — if the MAC is missing entirely, the BMS has not advertised recently.

**2. BMS is powered off or in deep sleep**
Power cycle the BMS (physical switch or disconnect/reconnect power). After power-on it advertises immediately.

**3. BMS stopped advertising after too many failed connect attempts (old firmware behavior)**
Power cycle the BMS to reset its advertising state.

**4. MAC address in `config/sites.json` is wrong**
Run `bash identify_bms.sh` to re-probe and overwrite the stale MAC.

---

## Symptom: "BMS error: ... InProgress"

BlueZ received a `Device.Connect()` while `StartDiscovery` was already running. This should not happen with the current code (scanner is always stopped before connect). If seen:
- Check whether two `ble-bridge` containers are running simultaneously: `docker ps | grep ble-bridge`
- Restart: `docker compose up -d --no-deps --force-recreate ble-bridge`

---

## Symptom: MAC missing from `config/sites.json`

Run the identification wizard. It probes every nearby BLE device, identifies the BMS by its c_13 response, and saves the MAC to `config/sites.json`:

```bash
bash identify_bms.sh
```

This is the only time active probing is needed. After the MAC is saved, all future connections use the passive scan approach.

---

## How to remove a BMS from config

Edit `config/sites.json` on the server and delete the device entry for the BMS, or remove the `"mac"` field to put it back into auto-probe mode. Then restart ble-bridge:

```bash
docker compose up -d --no-deps --force-recreate ble-bridge
```

A web UI for managing devices (add/remove/rename) is planned for Phase 12.

---

## Diagnostic commands (run on the Linux server)

```bash
# Are we currently waiting or connected?
docker logs --tail=20 victron-ble-bridge-1 2>&1

# Is the BMS visible to BlueZ at all?
bluetoothctl info XX:XX:XX:XX:XX:XX

# What devices does BlueZ currently see?
timeout 15 bluetoothctl scan on 2>&1 &
sleep 12 && bluetoothctl devices 2>&1

# Check config
cat config/sites.json | python3 -m json.tool | grep -A5 litime
```
