#!/usr/bin/env python3
"""
Linux BLE bridge: passively scans Victron BLE advertisements and actively polls
LiTime BMS devices, writing all data to InfluxDB.

Production mode  — uses bleak + BlueZ to scan real BLE advertisements.
Test/fixture mode — reads BLE_FIXTURE_FILE (JSONL, pre-decoded Victron packets)
                    and exits after the last line; no real BLE hardware required.
"""
import asyncio
import json
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from drivers.victron import decode_advertisement, FIELD_GETTERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

INFLUX_URL       = os.environ.get("INFLUX_URL", "")
INFLUX_TOKEN     = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG       = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET    = os.environ.get("INFLUX_BUCKET", "")
SITES_FILE       = os.environ.get("SITES_FILE", "/app/sites.json")
BLE_FIXTURE_FILE = os.environ.get("BLE_FIXTURE_FILE", "")

VICTRON_MFR_ID = 0x02E1
_BMS_TYPES     = {"litime_bms"}
_BMS_BACKOFF   = [5, 10, 20, 40]   # seconds; capped at last entry


def load_device_map(sites_file: str) -> Dict[str, Dict[str, Any]]:
    """Load sites.json and return {MAC_UPPER: {site_id, device_id, label, key, type, mac}}."""
    path = pathlib.Path(sites_file)
    if not path.exists():
        log.warning("Sites file not found: %s", sites_file)
        return {}
    with open(path) as f:
        data = json.load(f)
    result: Dict[str, Dict[str, Any]] = {}
    for site in data.get("sites", []):
        site_id = site["id"]
        for dev in site.get("devices", []):
            mac      = dev.get("mac", "").upper()
            dev_type = dev.get("type", "unknown")
            # Victron devices require a MAC (matched from passive BLE advertisements).
            # BMS devices can omit it — they will auto-probe on first connect.
            if not mac and dev_type not in _BMS_TYPES:
                log.warning("Skipping %s/%s: no MAC and type is not a BMS",
                            site_id, dev.get("id", "?"))
                continue
            key = mac if mac else f"_{site_id}_{dev['id']}"
            result[key] = {
                "site_id":   site_id,
                "device_id": dev["id"],
                "label":     dev.get("label", dev["id"]),
                "key":       dev.get("key", ""),
                "type":      dev_type,
                "mac":       mac,
            }
    log.info("Loaded %d devices from %s", len(result), sites_file)
    return result


def _make_point(device_id: str, label: str, site_id: str,
                ts: datetime, fields: Dict[str, float]) -> Optional[Point]:
    if not fields:
        return None
    p = (Point("solar")
         .tag("device", device_id)
         .tag("label", label)
         .tag("site", site_id)
         .time(ts))
    for k, v in fields.items():
        p = p.field(k, v)
    return p


def _make_battery_point(device_id: str, label: str, site_id: str,
                        ts: datetime, fields: Dict[str, float]) -> Optional[Point]:
    if not fields:
        return None
    p = (Point("battery")
         .tag("device", device_id)
         .tag("label", label)
         .tag("site", site_id)
         .time(ts))
    for k, v in fields.items():
        p = p.field(k, v)
    return p


class InfluxWriter:
    def __init__(self):
        self._client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def write(self, point: Point):
        try:
            self._write_api.write(bucket=INFLUX_BUCKET, record=point)
        except Exception as e:
            log.error("InfluxDB write failed: %s", e)


def run_fixture_mode(fixture_file: str, device_map: Dict[str, Dict], writer: InfluxWriter) -> int:
    """Replay pre-decoded BLE packets from a JSONL file. Returns count of points written."""
    path = pathlib.Path(fixture_file)
    if not path.exists():
        log.error("Fixture file not found: %s", fixture_file)
        sys.exit(1)
    count = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                mac  = payload.get("mac", "").upper()
                ts   = datetime.now(timezone.utc)
                dev  = device_map.get(mac, {})
                dev_id  = dev.get("device_id", mac.replace(":", "").lower())
                label   = dev.get("label", mac)
                site_id = dev.get("site_id", "test")
                raw_fields = payload.get("raw", {})
                fields = {k: float(v) for k, v in raw_fields.items()
                          if isinstance(v, (int, float))}
                pt = _make_point(dev_id, label, site_id, ts, fields)
                if pt:
                    writer.write(pt)
                    count += 1
                    log.info("[%s/%s] wrote %d fields", site_id, dev_id, len(fields))
            except Exception as e:
                log.error("Fixture parse error: %s", e)
    log.info("Fixture replay complete: %d points written", count)
    return count


# Shared scanner reference — BMS pollers stop/start it around connect() to avoid
# org.bluez.Error.InProgress (BlueZ cannot connect while discovery is active).
_victron_scanner = None


async def run_ble_scanner(device_map: Dict[str, Dict], writer: InfluxWriter):
    """Production mode: scan BLE indefinitely, decode Victron advertisements."""
    global _victron_scanner
    from bleak import BleakScanner

    seen: Dict[str, int] = {}

    def _callback(device, adv_data):
        if VICTRON_MFR_ID not in adv_data.manufacturer_data:
            return
        mac  = device.address.upper()
        info = device_map.get(mac)
        if not info:
            log.debug("Unknown Victron MAC: %s", mac)
            return
        raw_bytes = adv_data.manufacturer_data[VICTRON_MFR_ID]
        fields    = decode_advertisement(raw_bytes, info["key"])
        if not fields:
            return
        ts = datetime.now(timezone.utc)
        pt = _make_point(info["device_id"], info["label"], info["site_id"], ts, fields)
        if pt:
            writer.write(pt)
            seen[mac] = seen.get(mac, 0) + 1
            n = seen[mac]
            if n == 1 or n % 300 == 0:
                log.info("[%s/%s] %d points (latest: %s)",
                         info["site_id"], info["device_id"], n,
                         " ".join(f"{k}={v:.1f}" for k, v in list(fields.items())[:3]))

    log.info("BLE scan started — watching %d Victron devices", len(device_map))
    scanner = BleakScanner(detection_callback=_callback)
    _victron_scanner = scanner
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        _victron_scanner = None
        try:
            await scanner.stop()
        except Exception:
            pass


async def _scanner_stop() -> bool:
    """Stop the Victron scanner if running. Returns True if it was running."""
    if _victron_scanner is None:
        return False
    try:
        await _victron_scanner.stop()
        return True
    except Exception:
        return False


async def _scanner_start():
    """Restart the Victron scanner if it exists."""
    if _victron_scanner is None:
        return
    try:
        await _victron_scanner.start()
    except Exception:
        pass


async def run_bms_poller(info: Dict, writer: InfluxWriter):
    """Connect to one LiTime BMS, poll every 5 s, reconnect with exponential backoff.

    BlueZ returns org.bluez.Error.InProgress if a Device.Connect() is issued while
    the adapter is in discovery mode.  We stop the Victron scanner before each
    connect attempt and restart it once the BMS is connected (concurrent scan +
    active connection is fine once the connection is established).
    """
    from drivers.litime import LiTimeBMS

    mac     = info["mac"]
    site_id = info["site_id"]
    dev_id  = info["device_id"]
    label   = info["label"]
    bms     = LiTimeBMS(mac)
    backoff_idx = 0

    def _on_data(fields: Dict[str, float]):
        ts = datetime.now(timezone.utc)
        pt = _make_battery_point(dev_id, label, site_id, ts, fields)
        if pt:
            writer.write(pt)
            log.info("[%s/%s] battery SOC=%.0f%% V=%.2fV A=%.2fA",
                     site_id, dev_id,
                     fields.get("soc", 0),
                     fields.get("battery_voltage", 0),
                     fields.get("battery_current", 0))

    bms.on_data_callback = _on_data

    # Give the scanner a moment to start before we stop it for the first connect.
    await asyncio.sleep(3)

    while True:
        connected_ok = False
        scan_was_running = False
        try:
            # Stop scanner: BlueZ cannot connect while discovery is active.
            scan_was_running = await _scanner_stop()
            if scan_was_running:
                await asyncio.sleep(0.5)  # let StopDiscovery complete

            await bms.connect()
            connected_ok = True
            backoff_idx  = 0

            # Scanner can run concurrently once the BMS connection is established.
            await _scanner_start()
            scan_was_running = False

            while bms.is_connected:
                await bms.poll()
                await asyncio.sleep(5)
            log.info("[%s/%s] BMS disconnected, reconnecting", site_id, dev_id)
        except Exception as e:
            log.error("[%s/%s] BMS error: %s — reconnect in %ds",
                      site_id, dev_id, e, _BMS_BACKOFF[backoff_idx])
        finally:
            if scan_was_running:
                await _scanner_start()
            await bms.disconnect()

        delay = _BMS_BACKOFF[backoff_idx]
        await asyncio.sleep(delay)
        if not connected_ok:
            backoff_idx = min(backoff_idx + 1, len(_BMS_BACKOFF) - 1)


async def run_production(device_map: Dict[str, Dict], writer: InfluxWriter):
    """Run passive Victron scanner and active BMS pollers concurrently."""
    victron_devices = {mac: info for mac, info in device_map.items()
                       if info.get("type") not in _BMS_TYPES}
    bms_devices     = [info for info in device_map.values()
                       if info.get("type") in _BMS_TYPES]

    # Separate BMS devices into those with a saved MAC (ready) and those without.
    ready_bms       = [info for info in bms_devices if info.get("mac")]
    unconfigured    = [info for info in bms_devices if not info.get("mac")]

    if len(unconfigured) > 1:
        # Multiple unconfigured BMSs — auto-probe would be ambiguous.
        for info in unconfigured:
            log.error(
                "[%s/%s] BMS has no MAC address — cannot start poller. "
                "Run 'bash identify_bms.sh' to identify and configure each BMS.",
                info["site_id"], info["device_id"],
            )
        bms_devices = ready_bms
    elif len(unconfigured) == 1:
        # Single unconfigured BMS — safe to auto-probe (probe_for_litime finds exactly one).
        bms_devices = ready_bms + unconfigured

    tasks = []
    if victron_devices:
        tasks.append(asyncio.create_task(run_ble_scanner(victron_devices, writer)))
    for info in bms_devices:
        tasks.append(asyncio.create_task(run_bms_poller(info, writer)))

    if not tasks:
        log.warning("No devices configured — nothing to do")
        return

    log.info("Production mode: %d Victron scanner(s), %d BMS poller(s)",
             1 if victron_devices else 0, len(bms_devices))
    await asyncio.gather(*tasks)


def main():
    if not INFLUX_URL or not INFLUX_TOKEN or not INFLUX_BUCKET:
        sys.exit("INFLUX_URL, INFLUX_TOKEN, INFLUX_BUCKET env vars are required")
    device_map = load_device_map(SITES_FILE)
    writer     = InfluxWriter()

    if BLE_FIXTURE_FILE:
        log.info("TEST MODE: replaying %s", BLE_FIXTURE_FILE)
        run_fixture_mode(BLE_FIXTURE_FILE, device_map, writer)
    else:
        log.info("PRODUCTION MODE: starting BLE scanner and BMS pollers")
        asyncio.run(run_production(device_map, writer))


if __name__ == "__main__":
    main()
