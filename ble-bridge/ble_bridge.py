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
import signal
import sys
import time
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
BLE_ADAPTER      = os.environ.get("BLE_ADAPTER", "")  # e.g. "hci1" for a second dongle

VICTRON_MFR_ID    = 0x02E1
_BMS_TYPES        = {"litime_bms"}
_BMS_BACKOFF      = [5, 10, 20, 40]   # seconds; capped at last entry
_DEFAULT_WRITE_S  = 60                 # write each Victron device at most once per minute


async def _read_one_bms_frame(address: str, write_uuid: str, notify_uuid: str) -> Dict:
    """Connect to a BMS, collect one data frame, disconnect. Returns field dict."""
    from drivers.litime import LiTimeBMS
    result: Dict = {}
    ev = asyncio.Event()

    def _cb(fields: Dict):
        result.update(fields)
        ev.set()

    bms = LiTimeBMS(address, adapter=BLE_ADAPTER)
    bms._write_uuid   = write_uuid
    bms._notify_uuid  = notify_uuid
    bms.on_data_callback = _cb
    try:
        await bms.connect()
        await bms.poll()
        await asyncio.wait_for(ev.wait(), timeout=8.0)
    finally:
        await bms.disconnect()
    return result


class BridgeController:
    """Manages scanner and BMS poller tasks; supports reload and BMS scan.

    A single instance lives in run_production() and is shared with the
    aiohttp API server so /reload and /scan-bms can reach it.
    """

    def __init__(self, device_map: Dict[str, Any], writer: "InfluxWriter"):
        self.device_map: Dict[str, Any] = device_map
        self.writer = writer
        self._scanner_task: Optional[asyncio.Task] = None
        self._bms_tasks:    Dict[str, asyncio.Task] = {}

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _bms_key(info: Dict) -> str:
        return info.get("mac") or f"_{info['site_id']}_{info['device_id']}"

    @staticmethod
    def _victron_subset(dm: Dict) -> Dict:
        return {mac: info for mac, info in dm.items()
                if info.get("type") not in _BMS_TYPES}

    @staticmethod
    def _bms_list(dm: Dict):
        return [info for info in dm.values() if info.get("type") in _BMS_TYPES]

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self):
        """Spawn initial scanner + BMS poller tasks."""
        victron  = self._victron_subset(self.device_map)
        bms_devs = self._bms_list(self.device_map)

        ready = [i for i in bms_devs if i.get("mac")]
        uncfg = [i for i in bms_devs if not i.get("mac")]
        if len(uncfg) > 1:
            for info in uncfg:
                log.error("[%s/%s] BMS has no MAC — cannot start poller; "
                          "run scan-bms via dashboard to identify it",
                          info["site_id"], info["device_id"])
            bms_devs = ready
        elif len(uncfg) == 1:
            bms_devs = ready + uncfg

        self._scanner_task = asyncio.create_task(run_ble_scanner(victron, self.writer))
        for info in bms_devs:
            key = self._bms_key(info)
            self._bms_tasks[key] = asyncio.create_task(run_bms_poller(info, self.writer))

        log.info("BridgeController started: %d Victron device(s), %d BMS poller(s)",
                 len(victron), len(bms_devs))

    async def reload(self):
        """Re-read sites.json and diff-apply changes to running tasks."""
        new_map = load_device_map(SITES_FILE)

        old_bms_keys = {self._bms_key(v) for v in self.device_map.values()
                        if v.get("type") in _BMS_TYPES}
        new_bms_keys = {self._bms_key(v) for v in new_map.values()
                        if v.get("type") in _BMS_TYPES}

        # Cancel tasks for removed BMS devices
        for key in old_bms_keys - new_bms_keys:
            t = self._bms_tasks.pop(key, None)
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        # Restart scanner with updated Victron device list
        new_victron = self._victron_subset(new_map)
        if self._scanner_task and not self._scanner_task.done():
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.sleep(1.0)  # let BlueZ stop the old discovery session
        self._scanner_task = asyncio.create_task(run_ble_scanner(new_victron, self.writer))

        # Spawn tasks for newly added BMS devices
        new_bms_by_key = {self._bms_key(v): v for v in new_map.values()
                          if v.get("type") in _BMS_TYPES}
        for key in new_bms_keys - old_bms_keys:
            info = new_bms_by_key[key]
            self._bms_tasks[key] = asyncio.create_task(run_bms_poller(info, self.writer))

        self.device_map = new_map
        log.info("Bridge reloaded: %d device(s) (%d Victron, %d BMS)",
                 len(new_map), len(new_victron), len(new_bms_keys))

    async def scan_bms(self) -> list:
        """Pause BMS pollers + scanner, probe for BMS devices, restart everything.

        Active BMS pollers keep connected devices off-air (connected BLE devices
        stop advertising). We must cancel them so devices disconnect, wait for them
        to start advertising again, then scan. All pollers are restarted in finally.
        """
        from drivers.litime import probe_all_litime

        was_running = await _scanner_stop()

        # Cancel active BMS pollers and wait for their finally-blocks to finish.
        # Each poller's finally calls bms.disconnect() — we must wait for that to
        # complete before probing, otherwise the BMS stays connected and stops advertising.
        paused_tasks = []
        paused_keys  = list(self._bms_tasks.keys())
        for key in paused_keys:
            t = self._bms_tasks.pop(key, None)
            if t and not t.done():
                t.cancel()
                paused_tasks.append(t)
        if paused_tasks:
            log.info("scan-bms: paused %d BMS poller(s), waiting for disconnect...", len(paused_tasks))
            # gather with return_exceptions waits for finally blocks without re-raising CancelledError
            await asyncio.gather(*paused_tasks, return_exceptions=True)
            # LiTime BMS needs ~20s after a forced disconnect before it reliably accepts
            # new BLE connections and responds to GATT characteristic probes.
            await asyncio.sleep(20.0)

        if was_running:
            await asyncio.sleep(0.5)

        results = []
        try:
            found = []
            try:
                found = await asyncio.wait_for(
                    probe_all_litime(scan_timeout=15.0, probe_timeout=5.0, adapter=BLE_ADAPTER),
                    timeout=70.0,
                )
                log.info("scan-bms: probe complete, %d BMS device(s) found", len(found))
            except Exception as _exc:
                # BlueZ transient errors (InProgress, timeout) must not crash the scan
                # endpoint — return empty so callers get a valid [] response, not HTTP 500.
                log.warning("scan-bms: probe raised %s: %s — returning empty", type(_exc).__name__, _exc)
            for address, write_uuid, notify_uuid in found:
                frame: Dict = {}
                try:
                    frame = await asyncio.wait_for(
                        _read_one_bms_frame(address, write_uuid, notify_uuid),
                        timeout=20.0,
                    )
                except Exception as exc:
                    log.warning("scan-bms: frame read failed for %s: %s", address, exc)
                results.append({
                    "mac":         address,
                    "write_uuid":  write_uuid,
                    "notify_uuid": notify_uuid,
                    "soc":         frame.get("soc"),
                    "voltage":     frame.get("battery_voltage"),
                    "temp":        frame.get("temperature"),
                })
        finally:
            # Brief pause so BlueZ finishes closing frame-read connections before
            # the BMS pollers try to reconnect — avoids InProgress on their first connect.
            if found:
                await asyncio.sleep(2.0)
            # Restart BMS pollers for all configured BMS devices
            for info in self._bms_list(self.device_map):
                key = self._bms_key(info)
                if key not in self._bms_tasks or self._bms_tasks[key].done():
                    self._bms_tasks[key] = asyncio.create_task(run_bms_poller(info, self.writer))
            if paused_keys:
                log.info("scan-bms: restarted %d BMS poller(s)", len(paused_keys))
            if was_running:
                await _scanner_start()

        return results


def _persist_mac(sites_file: str, site_id: str, device_id: str, mac: str):
    """Write a discovered BMS MAC back to sites.json so it survives restarts."""
    path = pathlib.Path(sites_file)
    try:
        with open(path) as f:
            data = json.load(f)
        for site in data.get("sites", []):
            if site["id"] != site_id:
                continue
            for dev in site.get("devices", []):
                if dev["id"] == device_id:
                    dev["mac"] = mac
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("[%s/%s] discovered MAC %s saved to %s", site_id, device_id, mac, sites_file)
    except Exception as e:
        log.error("[%s/%s] failed to save discovered MAC: %s", site_id, device_id, e)


def _persist_uuids(sites_file: str, site_id: str, device_id: str,
                   write_uuid: str, notify_uuid: str) -> None:
    """Write discovered BLE UUIDs back to sites.json so probe_all_litime is skipped on restart."""
    path = pathlib.Path(sites_file)
    try:
        with open(path) as f:
            data = json.load(f)
        for site in data.get("sites", []):
            if site["id"] != site_id:
                continue
            for dev in site.get("devices", []):
                if dev["id"] == device_id:
                    dev["write_uuid"]  = write_uuid
                    dev["notify_uuid"] = notify_uuid
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("[%s/%s] UUIDs persisted to sites.json", site_id, device_id)
    except Exception as e:
        log.error("[%s/%s] failed to persist UUIDs: %s", site_id, device_id, e)


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
        # Only handle BLE-bridge sites — ESP32/MQTT sites are decoded by ble-decoder.
        bridge = site.get("bridge", "ble")
        if bridge in ("esp32", "mqtt"):
            log.debug("Skipping site %s (bridge=%s — handled by ble-decoder)",
                      site_id, bridge)
            continue
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
                "site_id":        site_id,
                "device_id":      dev["id"],
                "label":          dev.get("label", dev["id"]),
                "key":            dev.get("key", ""),
                "type":           dev_type,
                "mac":            mac,
                "write_interval": int(dev.get("write_interval_s", _DEFAULT_WRITE_S)),
                "capacity_ah":    dev.get("capacity_ah"),
                "write_uuid":     dev.get("write_uuid"),   # skips probe_all_litime on restart
                "notify_uuid":    dev.get("notify_uuid"),
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

# MAC → monotonic timestamp of last InfluxDB write (throttle per device).
_last_written: Dict[str, float] = {}

# MAC → Event: fires when the passive scanner first sees the MAC advertising.
# BMS pollers register here so connect() only runs after the device is in BlueZ's cache.
_bms_seen_events: Dict[str, asyncio.Event] = {}


async def _wait_for_bms_in_scan(mac: str, timeout: float = 300.0) -> bool:
    """Block until the running scanner spots mac, or until timeout seconds.

    Returns True if the MAC was seen, False on timeout.  No-ops and returns
    False immediately if no scanner is running (BMS-only setup).
    """
    if _victron_scanner is None:
        return False
    ev = asyncio.Event()
    _bms_seen_events[mac] = ev
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        _bms_seen_events.pop(mac, None)


async def run_ble_scanner(device_map: Dict[str, Dict], writer: InfluxWriter):
    """Production mode: scan BLE indefinitely, decode Victron advertisements."""
    global _victron_scanner
    from bleak import BleakScanner

    seen: Dict[str, int] = {}

    def _callback(device, adv_data):
        mac = device.address.upper()

        # Notify any BMS pollers waiting for this MAC to appear in scan.
        if mac in _bms_seen_events:
            _bms_seen_events[mac].set()

        if VICTRON_MFR_ID not in adv_data.manufacturer_data:
            return
        info = device_map.get(mac)
        if not info:
            log.debug("Unknown Victron MAC: %s", mac)
            return

        # Throttle writes: only record once per write_interval seconds per device.
        interval = info.get("write_interval", _DEFAULT_WRITE_S)
        now_mono = time.monotonic()
        if now_mono - _last_written.get(mac, 0.0) < interval:
            return

        raw_bytes = adv_data.manufacturer_data[VICTRON_MFR_ID]
        fields    = decode_advertisement(raw_bytes, info["key"])
        if not fields:
            return
        ts = datetime.now(timezone.utc)
        pt = _make_point(info["device_id"], info["label"], info["site_id"], ts, fields)
        if pt:
            writer.write(pt)
            _last_written[mac] = now_mono
            seen[mac] = seen.get(mac, 0) + 1
            n = seen[mac]
            if n == 1 or n % 10 == 0:
                log.info("[%s/%s] %d points written (interval=%ds, latest: %s)",
                         info["site_id"], info["device_id"], n, interval,
                         " ".join(f"{k}={v:.1f}" for k, v in list(fields.items())[:3]))

    log.info("BLE scan started — watching %d Victron devices (adapter=%s)",
             len(device_map), BLE_ADAPTER or "default")
    adapter_kw = {"bluez": {"adapter": BLE_ADAPTER}} if BLE_ADAPTER else {}
    scanner = BleakScanner(detection_callback=_callback, **adapter_kw)
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

    mac         = info["mac"]
    site_id     = info["site_id"]
    dev_id      = info["device_id"]
    label       = info["label"]
    capacity_ah = info.get("capacity_ah")
    bms         = LiTimeBMS(mac, adapter=BLE_ADAPTER)
    # Pre-load known UUIDs so probe_all_litime is skipped on connect
    if info.get("write_uuid"):
        bms._write_uuid  = info["write_uuid"]
        bms._notify_uuid = info.get("notify_uuid")
    backoff_idx = 0

    def _on_data(fields: Dict[str, float]):
        voltage = fields.get("battery_voltage", 0)
        if "remaining_charge_ah" in fields:
            # Use BMS-reported remaining charge (bytes [62:64] of c_13 frame, 10 mAh/unit)
            fields["remaining_wh"] = round(fields["remaining_charge_ah"] * voltage, 0)
        elif capacity_ah is not None:
            soc = fields.get("soc", 0)
            fields["remaining_wh"] = round(soc / 100.0 * capacity_ah * voltage, 0)
        ts = datetime.now(timezone.utc)
        pt = _make_battery_point(dev_id, label, site_id, ts, fields)
        if pt:
            writer.write(pt)
            rwh = f" ~{fields['remaining_wh']:.0f}Wh" if "remaining_wh" in fields else ""
            log.info("[%s/%s] battery SOC=%.0f%%%s V=%.2fV A=%.2fA",
                     site_id, dev_id,
                     fields.get("soc", 0), rwh,
                     fields.get("battery_voltage", 0),
                     fields.get("battery_current", 0))

    bms.on_data_callback = _on_data

    while True:
        # Wait until the passive scanner actually sees the BMS advertising.
        # This guarantees BlueZ has a device object for the MAC before we call
        # Device.Connect() — the same approach BLE apps use: scan first, then connect.
        if mac:
            log.info("[%s/%s] waiting for BMS to appear in BLE scan...", site_id, dev_id)
            seen = await _wait_for_bms_in_scan(mac, timeout=300.0)
            if seen:
                log.info("[%s/%s] BMS detected, connecting now", site_id, dev_id)
            else:
                log.warning("[%s/%s] BMS not seen after 5 min — may be off or connected "
                            "to another device; will keep waiting", site_id, dev_id)
                continue  # loop back and wait again rather than failing immediately

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

            # Auto-probe discovered the MAC — persist it to config immediately.
            if not mac and bms.address:
                mac = bms.address
                _persist_mac(SITES_FILE, site_id, dev_id, mac)

            # Persist newly discovered UUIDs so probe_all_litime is skipped on next restart.
            if not info.get("write_uuid") and getattr(bms, "_write_uuid", None):
                _persist_uuids(SITES_FILE, site_id, dev_id, bms._write_uuid, bms._notify_uuid or "")
                info["write_uuid"]  = bms._write_uuid
                info["notify_uuid"] = bms._notify_uuid

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


async def run_production(device_map: Dict[str, Any], writer: InfluxWriter):
    """Run scanner, BMS pollers, and management API server concurrently."""
    from api_server import run_api_server

    if not device_map:
        log.warning("No devices configured — nothing to do")
        return

    controller = BridgeController(device_map, writer)
    controller.start()
    api_task   = asyncio.create_task(run_api_server(controller))

    loop = asyncio.get_running_loop()

    def _shutdown():
        log.info("Shutdown signal — cancelling tasks for clean BMS disconnect")
        api_task.cancel()
        for t in list(controller._bms_tasks.values()):
            t.cancel()
        if controller._scanner_task and not controller._scanner_task.done():
            controller._scanner_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT,  _shutdown)

    log.info("Production mode: %d Victron device(s), %d BMS poller(s)",
             sum(1 for v in device_map.values() if v.get("type") not in _BMS_TYPES),
             sum(1 for v in device_map.values() if v.get("type") in _BMS_TYPES))

    try:
        # api_task runs forever; BMS/scanner tasks are managed by the controller.
        await api_task
    except asyncio.CancelledError:
        pass
    finally:
        # Cancel all remaining tasks (including those spawned during reload).
        remaining = list(controller._bms_tasks.values())
        if controller._scanner_task:
            remaining.append(controller._scanner_task)
        for t in remaining:
            if not t.done():
                t.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)


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
