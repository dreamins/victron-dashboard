#!/usr/bin/env python3
"""
Linux BLE bridge: passively scans Victron BLE advertisements and writes to InfluxDB.

Production mode  — uses bleak + BlueZ to scan real BLE advertisements.
Test/fixture mode — reads BLE_FIXTURE_FILE (JSONL, same format as decoder fixtures)
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


def load_device_map(sites_file: str) -> Dict[str, Dict[str, Any]]:
    """Load sites.json and return {MAC_UPPER: {site_id, device_id, label, key}}."""
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
            mac = dev.get("mac", "").upper()
            if not mac:
                continue
            result[mac] = {
                "site_id":   site_id,
                "device_id": dev["id"],
                "label":     dev.get("label", dev["id"]),
                "key":       dev.get("key", ""),
                "type":      dev.get("type", "unknown"),
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


async def run_ble_scanner(device_map: Dict[str, Dict], writer: InfluxWriter):
    """Production mode: scan BLE indefinitely, decode Victron advertisements."""
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

    log.info("BLE scan started — watching %d known devices", len(device_map))
    async with BleakScanner(detection_callback=_callback):
        while True:
            await asyncio.sleep(3600)


def main():
    if not INFLUX_URL or not INFLUX_TOKEN or not INFLUX_BUCKET:
        sys.exit("INFLUX_URL, INFLUX_TOKEN, INFLUX_BUCKET env vars are required")
    device_map = load_device_map(SITES_FILE)
    writer     = InfluxWriter()

    if BLE_FIXTURE_FILE:
        log.info("TEST MODE: replaying %s", BLE_FIXTURE_FILE)
        run_fixture_mode(BLE_FIXTURE_FILE, device_map, writer)
    else:
        log.info("PRODUCTION MODE: starting BLE scanner")
        asyncio.run(run_ble_scanner(device_map, writer))


if __name__ == "__main__":
    main()
