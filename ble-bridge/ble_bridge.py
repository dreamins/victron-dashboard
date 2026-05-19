#!/usr/bin/env python3
"""
Linux BLE bridge: passively scans Victron BLE advertisements and actively polls
LiTime BMS devices, writing all data to InfluxDB.

Production mode  — uses bleak + BlueZ to scan real BLE advertisements.
Test/fixture mode — reads BLE_FIXTURE_FILE (JSONL, pre-decoded Victron packets)
                    and exits after the last line; no real BLE hardware required.

Testability
-----------
``run_bms_poller`` and ``run_ble_scanner`` accept optional factory kwargs so
unit tests can inject mock BLE objects without real hardware:

    await run_bms_poller(info, writer, bms_factory=lambda mac: MockBMS(mac))
    await run_ble_scanner(device_map, writer, scanner_factory=lambda cb: MockScanner(cb))

See ble-bridge/tests/test_poller.py for usage examples.
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
from typing import Callable, Dict, Any, Optional

from bridge_config import (
    load_device_map, persist_mac, persist_uuids,
    BMS_TYPES, DEFAULT_WRITE_S,
)
from writer import InfluxWriter, _make_point, _make_battery_point, _make_heartbeat_point

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

SITES_FILE       = os.environ.get("SITES_FILE", "/app/sites.json")
BLE_FIXTURE_FILE = os.environ.get("BLE_FIXTURE_FILE", "")
BLE_ADAPTER      = os.environ.get("BLE_ADAPTER", "")

VICTRON_MFR_ID = 0x02E1
_BMS_BACKOFF   = [5, 10, 20, 40]  # seconds; capped at last entry


# ── Shared scanner reference ──────────────────────────────────────────────────
# BMS pollers stop/start it around connect() to avoid InProgress errors.
_victron_scanner = None

# MAC → monotonic timestamp of last InfluxDB write (per-device throttle).
_last_written: Dict[str, float] = {}

# MAC → Event: fires when passive scanner first sees the MAC advertising.
_bms_seen_events: Dict[str, asyncio.Event] = {}

# MAC → {name, rssi, seen_at}: all Victron-advertising devices seen by the scanner.
# Populated by run_ble_scanner callback; read by scan_victron() without stopping the scanner.
_victron_seen_cache: Dict[str, Dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _wait_for_bms_in_scan(mac: str, timeout: float = 300.0) -> bool:
    """Block until the running scanner spots mac, or timeout.

    Returns True if seen, False on timeout or if no scanner is running.
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


async def _scanner_stop() -> bool:
    """Stop the Victron scanner if running. Returns True if it was running."""
    if _victron_scanner is None:
        return False
    try:
        await _victron_scanner.stop()
        return True
    except Exception:
        return False


async def _scanner_start() -> None:
    if _victron_scanner is None:
        return
    try:
        await _victron_scanner.start()
    except Exception:
        pass


async def _read_one_bms_frame(address: str, write_uuid: str,
                               notify_uuid: str) -> Dict:
    """Connect to a BMS, collect one data frame, disconnect."""
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
        try:
            await bms.disconnect()
        except Exception:
            pass
    return result


# ── BridgeController ─────────────────────────────────────────────────────────

async def run_heartbeat(site_ids: list, writer: InfluxWriter) -> None:
    """Write a liveness heartbeat to InfluxDB every 30s.

    As long as this task runs, the API can distinguish 'bridge alive but devices
    asleep at night' from 'bridge process is dead'.
    """
    while True:
        ts = datetime.now(timezone.utc)
        for site_id in site_ids:
            pt = _make_heartbeat_point(site_id, ts)
            writer.write(pt)
        await asyncio.sleep(30.0)


class BridgeController:
    """Manages scanner and BMS poller tasks; supports reload and BMS scan."""

    def __init__(self, device_map: Dict[str, Any], writer: InfluxWriter):
        self.device_map: Dict[str, Any]    = device_map
        self.writer                        = writer
        self._scanner_task:   Optional[asyncio.Task] = None
        self._bms_tasks:      Dict[str, asyncio.Task] = {}
        self._watchdog_task:  Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    @staticmethod
    def _bms_key(info: Dict) -> str:
        return info.get("mac") or f"_{info['site_id']}_{info['device_id']}"

    @staticmethod
    def _victron_subset(dm: Dict) -> Dict:
        return {mac: info for mac, info in dm.items()
                if info.get("type") not in BMS_TYPES}

    @staticmethod
    def _bms_list(dm: Dict):
        return [info for info in dm.values() if info.get("type") in BMS_TYPES]

    def start(self) -> None:
        victron  = self._victron_subset(self.device_map)
        bms_devs = self._bms_list(self.device_map)

        ready = [i for i in bms_devs if i.get("mac")]
        uncfg = [i for i in bms_devs if not i.get("mac")]
        if len(uncfg) > 1:
            for info in uncfg:
                log.error("[%s/%s] BMS has no MAC — run scan-bms to identify it",
                          info["site_id"], info["device_id"])
            bms_devs = ready
        elif len(uncfg) == 1:
            bms_devs = ready + uncfg

        self._scanner_task  = asyncio.create_task(run_ble_scanner(victron, self.writer))
        self._watchdog_task = asyncio.create_task(self._scanner_watchdog())
        site_ids = list({info["site_id"] for info in self.device_map.values()})
        self._heartbeat_task = asyncio.create_task(run_heartbeat(site_ids, self.writer))
        for info in bms_devs:
            key = self._bms_key(info)
            self._bms_tasks[key] = asyncio.create_task(run_bms_poller(info, self.writer))

        log.info("BridgeController started: %d Victron, %d BMS",
                 len(victron), len(bms_devs))

    async def _scanner_watchdog(self) -> None:
        """Restart the scanner task if it exits unexpectedly (e.g. BlueZ crash).

        Without this, BMS pollers fall into a tight loop (_victron_scanner is
        None → _wait_for_bms_in_scan returns False immediately → loop) and the
        BMS goes offline indefinitely.
        """
        while True:
            await asyncio.sleep(30.0)
            t = self._scanner_task
            if t is not None and t.done():
                exc = t.exception() if not t.cancelled() else None
                log.error("Scanner task exited (exc=%s) — restarting in 10s", exc)
                await asyncio.sleep(10.0)
                victron = self._victron_subset(self.device_map)
                self._scanner_task = asyncio.create_task(
                    run_ble_scanner(victron, self.writer))
                log.info("Scanner task restarted by watchdog")

    async def reload(self) -> None:
        """Re-read sites.json and diff-apply changes without restarting."""
        new_map = load_device_map(SITES_FILE)

        old_bms_keys = {self._bms_key(v) for v in self.device_map.values()
                        if v.get("type") in BMS_TYPES}
        new_bms_keys = {self._bms_key(v) for v in new_map.values()
                        if v.get("type") in BMS_TYPES}

        for key in old_bms_keys - new_bms_keys:
            t = self._bms_tasks.pop(key, None)
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        new_victron = self._victron_subset(new_map)
        if self._scanner_task and not self._scanner_task.done():
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.sleep(1.0)
        self._scanner_task = asyncio.create_task(run_ble_scanner(new_victron, self.writer))

        new_bms_by_key = {self._bms_key(v): v for v in new_map.values()
                          if v.get("type") in BMS_TYPES}
        for key in new_bms_keys - old_bms_keys:
            info = new_bms_by_key[key]
            self._bms_tasks[key] = asyncio.create_task(run_bms_poller(info, self.writer))

        self.device_map = new_map
        log.info("Bridge reloaded: %d devices (%d Victron, %d BMS)",
                 len(new_map), len(new_victron), len(new_bms_keys))

    async def scan_bms(self) -> list:
        """Probe for new (unconfigured) BMS devices; leave existing pollers running.

        Only unconfigured BMS pollers (key starts with '_', no MAC yet) are
        cancelled — they haven't connected to anything so no disconnect delay
        is needed.  Configured BMS devices stay connected and won't appear in
        BLE advertising, so the probe naturally ignores them.
        """
        from drivers.litime import probe_all_litime

        was_running = await _scanner_stop()

        # Cancel only unconfigured pollers (key = "_site_device", no MAC).
        # Configured pollers keep running — their BMS stays connected and
        # invisible to the probe scan, which is exactly what we want.
        paused_tasks = []
        paused_keys  = []
        for key in list(self._bms_tasks.keys()):
            if not key.startswith("_"):
                continue  # configured (MAC-keyed) — leave running
            t = self._bms_tasks.pop(key, None)
            if t and not t.done():
                t.cancel()
                paused_tasks.append(t)
                paused_keys.append(key)
        if paused_tasks:
            log.info("scan-bms: paused %d unconfigured BMS poller(s)", len(paused_tasks))
            await asyncio.gather(*paused_tasks, return_exceptions=True)

        if was_running:
            await asyncio.sleep(0.5)

        results = []
        found: list = []
        try:
            try:
                found = await asyncio.wait_for(
                    probe_all_litime(scan_timeout=15.0, probe_timeout=5.0,
                                     adapter=BLE_ADAPTER),
                    timeout=70.0,
                )
                log.info("scan-bms: probe complete, %d device(s) found", len(found))
            except Exception as exc:
                log.warning("scan-bms: probe raised %s: %s — returning empty",
                            type(exc).__name__, exc)
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
            if found:
                await asyncio.sleep(2.0)
            for info in self._bms_list(self.device_map):
                key = self._bms_key(info)
                if key not in self._bms_tasks or self._bms_tasks[key].done():
                    self._bms_tasks[key] = asyncio.create_task(
                        run_bms_poller(info, self.writer))
            if paused_keys:
                log.info("scan-bms: restarted %d BMS poller(s)", len(paused_keys))
            if was_running:
                await _scanner_start()

        return results

    async def scan_victron(self) -> list:
        """Return nearby Victron devices not already in config.

        Reads from _victron_seen_cache populated by the running scanner callback —
        no scanner stop, no BleakScanner.discover(), no interference with the BMS
        connection.  Cache entries older than 60s are excluded.
        """
        now = time.monotonic()
        configured = {v["mac"].upper() for v in self.device_map.values()
                      if "mac" in v}
        results = []
        for mac, info in _victron_seen_cache.items():
            if now - info["seen_at"] > 60.0:
                continue
            if mac in configured:
                continue
            results.append({"mac": mac, "name": info["name"], "rssi": info["rssi"]})

        log.info("scan-victron: %d unconfigured Victron device(s) in cache", len(results))
        return sorted(results, key=lambda x: -(x["rssi"] or -999))


# ── Scanner ───────────────────────────────────────────────────────────────────

async def run_ble_scanner(device_map: Dict[str, Dict], writer: InfluxWriter,
                          scanner_factory: Optional[Callable] = None) -> None:
    """Scan BLE indefinitely, decode Victron advertisements.

    Parameters
    ----------
    scanner_factory:
        Optional ``callable(detection_callback, **kwargs) → scanner``.
        Defaults to bleak.BleakScanner.  Inject a mock in tests.
    """
    global _victron_scanner
    from drivers.victron import decode_advertisement

    seen: Dict[str, int] = {}

    def _callback(device, adv_data):
        mac = device.address.upper()
        if mac in _bms_seen_events:
            _bms_seen_events[mac].set()

        if VICTRON_MFR_ID not in adv_data.manufacturer_data:
            return

        _victron_seen_cache[mac] = {
            "name":    device.name or mac,
            "rssi":    adv_data.rssi,
            "seen_at": time.monotonic(),
        }

        info = device_map.get(mac)
        if not info:
            return

        interval = info.get("write_interval", DEFAULT_WRITE_S)
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
                log.info("[%s/%s] %d points (interval=%ds)",
                         info["site_id"], info["device_id"], n, interval)

    log.info("BLE scan started — %d Victron device(s) (adapter=%s)",
             len(device_map), BLE_ADAPTER or "default")

    if scanner_factory is not None:
        scanner = scanner_factory(_callback)
    else:
        from bleak import BleakScanner
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


# ── BMS poller ────────────────────────────────────────────────────────────────

async def run_bms_poller(info: Dict, writer: InfluxWriter,
                         bms_factory: Optional[Callable] = None,
                         _max_cycles: Optional[int] = None) -> None:
    """Connect to one LiTime BMS, poll every 5 s, reconnect on drop.

    Parameters
    ----------
    bms_factory:
        ``callable(mac: str) → BMS-like object`` with the same interface as
        ``drivers.litime.LiTimeBMS``.  Defaults to real LiTimeBMS.
        Inject a mock in tests.
    _max_cycles:
        Stop after this many connect/disconnect cycles.  For testing only;
        production leaves this None (runs forever).
    """
    mac         = info["mac"]
    site_id     = info["site_id"]
    dev_id      = info["device_id"]
    label       = info["label"]
    capacity_ah = info.get("capacity_ah")

    if bms_factory is None:
        from drivers.litime import LiTimeBMS
        bms_factory = lambda m: LiTimeBMS(m, adapter=BLE_ADAPTER)

    bms = bms_factory(mac)
    if info.get("write_uuid"):
        bms._write_uuid  = info["write_uuid"]
        bms._notify_uuid = info.get("notify_uuid")
    backoff_idx = 0
    cycles      = 0

    def _on_data(fields: Dict[str, float]) -> None:
        voltage = fields.get("battery_voltage", 0)
        if "remaining_charge_ah" in fields:
            fields["remaining_wh"] = round(fields["remaining_charge_ah"] * voltage, 0)
        elif capacity_ah is not None:
            soc = fields.get("soc", 0)
            fields["remaining_wh"] = round(soc / 100.0 * capacity_ah * voltage, 0)
        ts = datetime.now(timezone.utc)
        pt = _make_battery_point(dev_id, label, site_id, ts, fields)
        if pt:
            writer.write(pt)
            rwh = f" ~{fields['remaining_wh']:.0f}Wh" if "remaining_wh" in fields else ""
            log.info("[%s/%s] SOC=%.0f%%%s V=%.2fV A=%.2fA",
                     site_id, dev_id,
                     fields.get("soc", 0), rwh,
                     fields.get("battery_voltage", 0),
                     fields.get("battery_current", 0))

    bms.on_data_callback = _on_data

    while _max_cycles is None or cycles < _max_cycles:
        if mac:
            log.info("[%s/%s] waiting for BMS in scan...", site_id, dev_id)
            seen = await _wait_for_bms_in_scan(mac, timeout=300.0)
            if seen:
                log.info("[%s/%s] BMS detected, connecting", site_id, dev_id)
            else:
                # Do NOT continue-loop here — that creates an infinite tight loop
                # when _victron_scanner is None (scanner crashed), never connecting
                # the BMS and leaving the card grey forever.
                if _victron_scanner is None:
                    log.warning("[%s/%s] scanner down — sleeping 30s before blind connect",
                                site_id, dev_id)
                    await asyncio.sleep(30.0)
                else:
                    log.warning("[%s/%s] BMS not seen in 5 min — connecting anyway",
                                site_id, dev_id)
                # Fall through to connection attempt regardless.

        connected_ok    = False
        scan_was_running = False
        try:
            scan_was_running = await _scanner_stop()
            if scan_was_running:
                await asyncio.sleep(0.5)

            await asyncio.wait_for(bms.connect(), timeout=30.0)
            connected_ok = True
            backoff_idx  = 0

            if not mac and bms.address:
                mac = bms.address
                persist_mac(SITES_FILE, site_id, dev_id, mac)

            if not info.get("write_uuid") and getattr(bms, "_write_uuid", None):
                persist_uuids(SITES_FILE, site_id, dev_id,
                              bms._write_uuid, bms._notify_uuid or "")
                info["write_uuid"]  = bms._write_uuid
                info["notify_uuid"] = bms._notify_uuid

            await _scanner_start()
            scan_was_running = False

            while bms.is_connected:
                await bms.poll()
                await asyncio.sleep(5)
            log.info("[%s/%s] BMS disconnected", site_id, dev_id)
        except Exception as e:
            log.error("[%s/%s] BMS error: %s — retry in %ds",
                      site_id, dev_id, e, _BMS_BACKOFF[backoff_idx])
        finally:
            if scan_was_running:
                try:
                    await _scanner_start()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(bms.disconnect(), timeout=10.0)
            except Exception:
                pass

        cycles += 1
        delay = _BMS_BACKOFF[backoff_idx]
        await asyncio.sleep(delay)
        if not connected_ok:
            backoff_idx = min(backoff_idx + 1, len(_BMS_BACKOFF) - 1)


# ── Fixture mode ──────────────────────────────────────────────────────────────

def run_fixture_mode(fixture_file: str, device_map: Dict[str, Dict],
                     writer: InfluxWriter) -> int:
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
                mac     = payload.get("mac", "").upper()
                ts      = datetime.now(timezone.utc)
                dev     = device_map.get(mac, {})
                dev_id  = dev.get("device_id", mac.replace(":", "").lower())
                label   = dev.get("label", mac)
                site_id = dev.get("site_id", "test")
                fields  = {k: float(v) for k, v in payload.get("raw", {}).items()
                           if isinstance(v, (int, float))}
                pt = _make_point(dev_id, label, site_id, ts, fields)
                if pt:
                    writer.write(pt)
                    count += 1
                    log.info("[%s/%s] wrote %d fields", site_id, dev_id, len(fields))
            except Exception as e:
                log.error("Fixture parse error: %s", e)
    log.info("Fixture replay complete: %d points", count)
    return count


# ── Production entry point ────────────────────────────────────────────────────

async def run_production(device_map: Dict[str, Any], writer: InfluxWriter) -> None:
    from api_server import run_api_server

    if not device_map:
        log.warning("No devices configured")
        return

    controller = BridgeController(device_map, writer)
    controller.start()
    api_task   = asyncio.create_task(run_api_server(controller))
    loop       = asyncio.get_running_loop()

    def _shutdown():
        log.info("Shutdown — cancelling tasks")
        api_task.cancel()
        for t in list(controller._bms_tasks.values()):
            t.cancel()
        if controller._scanner_task and not controller._scanner_task.done():
            controller._scanner_task.cancel()
        if controller._watchdog_task and not controller._watchdog_task.done():
            controller._watchdog_task.cancel()
        if controller._heartbeat_task and not controller._heartbeat_task.done():
            controller._heartbeat_task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT,  _shutdown)

    log.info("Production: %d Victron, %d BMS",
             sum(1 for v in device_map.values() if v.get("type") not in BMS_TYPES),
             sum(1 for v in device_map.values() if v.get("type") in BMS_TYPES))

    try:
        await api_task
    except asyncio.CancelledError:
        pass
    finally:
        remaining = list(controller._bms_tasks.values())
        for t in [controller._scanner_task, controller._watchdog_task,
                  controller._heartbeat_task]:
            if t:
                remaining.append(t)
        for t in remaining:
            if not t.done():
                t.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)


def main():
    influx_url   = os.environ.get("INFLUX_URL", "")
    influx_token = os.environ.get("INFLUX_TOKEN", "")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "")
    if not influx_url or not influx_token or not influx_bucket:
        sys.exit("INFLUX_URL, INFLUX_TOKEN, INFLUX_BUCKET env vars are required")
    device_map = load_device_map(SITES_FILE)
    writer     = InfluxWriter()

    if BLE_FIXTURE_FILE:
        log.info("TEST MODE: replaying %s", BLE_FIXTURE_FILE)
        run_fixture_mode(BLE_FIXTURE_FILE, device_map, writer)
    else:
        log.info("PRODUCTION MODE")
        asyncio.run(run_production(device_map, writer))


if __name__ == "__main__":
    main()
