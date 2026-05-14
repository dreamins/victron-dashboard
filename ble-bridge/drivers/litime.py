"""LiTime BMS active BLE driver: auto-probe, connect, poll c_13 every 5s, parse 105-byte response.

Discovery is pure probe — no MAC, no service UUID assumed.  Every nearby BLE device is
connected and every writable+notifiable characteristic is tried.  The first device/char
pair that returns a response containing the c_13 anchor bytes is identified as the BMS.
"""
import asyncio
import logging
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

# c_13 response anchor at frame bytes [3:7]: type(01) + tag-with-response-bit(93) + magic(55 AA)
_C13_ANCHOR = bytes([0x01, 0x93, 0x55, 0xAA])
FRAME_LEN   = 105


def build_frame(tag: int) -> bytes:
    """Build a LiTime request frame for the given command tag."""
    checksum = (0x04 + tag) & 0xFF
    return bytes([0x00, 0x00, 0x04, 0x01, tag, 0x55, 0xAA, checksum])


def _checksum(frame: bytes) -> int:
    """Additive checksum over frame[2..103] per LiTime spec §2.3."""
    return sum(frame[2:104]) & 0xFF


def parse_litime_frame(data: bytes) -> Optional[Dict[str, float]]:
    """Parse a complete 105-byte c_13 response into a field dict.

    Returns None if data is too short or the checksum is wrong.
    """
    if len(data) < FRAME_LEN:
        return None
    frame = data[:FRAME_LEN]
    if _checksum(frame) != frame[104]:
        return None

    def u16(s): return int.from_bytes(frame[s:s+2], 'little')
    def i32(s): return int.from_bytes(frame[s:s+4], 'little', signed=True)
    def i8(s):  return int.from_bytes(frame[s:s+1], 'little', signed=True)

    voltage = u16(12) / 1000.0
    # Spec: positive current = discharge; invert so positive = charging.
    current = -(i32(48) / 1000.0)
    soc     = float(u16(90))
    soh     = float(u16(92))
    cycles  = float(u16(96))
    temp    = float(i8(52))   # cell temperature sensor

    cells  = [u16(16 + i * 2) / 1000.0 for i in range(16)]
    active = [v for v in cells if v > 0.0]
    cell_min = min(active) if active else 0.0
    cell_max = max(active) if active else 0.0
    cell_avg = sum(active) / len(active) if active else 0.0

    return {
        "battery_voltage": voltage,
        "battery_current": current,
        "soc":             soc,
        "soh":             soh,
        "cycles":          cycles,
        "temperature":     temp,
        "cell_min":        cell_min,
        "cell_max":        cell_max,
        "cell_avg":        cell_avg,
    }


# ── BLE probe ─────────────────────────────────────────────────────────────────

async def _try_characteristic_pair(client, write_uuid: str, notify_uuid: str,
                                    timeout: float) -> bool:
    """Send c_13 on write_uuid and wait up to timeout seconds for the c_13 anchor
    to appear in notifications on notify_uuid.  Returns True if found."""
    received = bytearray()
    found    = asyncio.Event()

    def _handler(sender, data: bytes):
        received.extend(data)
        if _C13_ANCHOR in received:
            found.set()

    try:
        await client.start_notify(notify_uuid, _handler)
        try:
            # write-without-response is more permissive during probing
            await client.write_gatt_char(write_uuid, build_frame(0x13), response=False)
            await asyncio.wait_for(found.wait(), timeout=timeout)
            return True
        except (asyncio.TimeoutError, Exception):
            return False
        finally:
            try:
                await client.stop_notify(notify_uuid)
            except Exception:
                pass
    except Exception:
        return False


async def _probe_device(address: str,
                        probe_timeout: float) -> Optional[Tuple[str, str, str]]:
    """Connect to one BLE address and probe all writable+notifiable char pairs.

    Returns (address, write_uuid, notify_uuid) on the first pair that responds
    to the c_13 probe, or None if the device is not a LiTime BMS.
    """
    from bleak import BleakClient
    try:
        async with BleakClient(address, timeout=10.0) as client:
            if not client.is_connected:
                return None
            for service in client.services:
                notifiable = [c.uuid for c in service.characteristics
                              if "notify" in c.properties]
                writable   = [c.uuid for c in service.characteristics
                              if "write" in c.properties
                              or "write-without-response" in c.properties]
                for w_uuid in writable:
                    for n_uuid in notifiable:
                        if await _try_characteristic_pair(client, w_uuid, n_uuid,
                                                          probe_timeout):
                            log.info("Probe identified LiTime BMS: %s  write=%s  notify=%s",
                                     address, w_uuid, n_uuid)
                            return (address, w_uuid, n_uuid)
    except Exception as exc:
        log.debug("Probe %s: %s", address, exc)
    return None


async def probe_all_litime(scan_timeout: float = 10.0,
                           probe_timeout: float = 2.5
                           ) -> list:
    """Scan and probe every nearby BLE device; return ALL that respond to c_13.

    Returns a list of (address, write_uuid, notify_uuid) tuples — one per
    LiTime BMS found.  Returns an empty list if none are found.
    """
    from bleak import BleakScanner
    log.info("Probing for LiTime BMS devices (scan=%.0fs, probe=%.1fs/device)...",
             scan_timeout, probe_timeout)
    devices = await BleakScanner.discover(timeout=scan_timeout)
    log.info("Found %d BLE device(s), probing each...", len(devices))
    results = []
    for device in devices:
        log.debug("Probing %s (%s)", device.address, device.name or "?")
        result = await _probe_device(device.address, probe_timeout)
        if result:
            results.append(result)
    log.info("LiTime probe complete: %d BMS device(s) found", len(results))
    return results


async def probe_for_litime(scan_timeout: float = 10.0,
                            probe_timeout: float = 2.5
                            ) -> Optional[Tuple[str, str, str]]:
    """Return the first LiTime BMS found, or None.  Convenience wrapper around probe_all_litime."""
    results = await probe_all_litime(scan_timeout, probe_timeout)
    return results[0] if results else None


# ── BMS client ────────────────────────────────────────────────────────────────

class LiTimeBMS:
    """Active BLE client for the LiTime BMS.

    Pass address="" to trigger auto-discovery on the first connect() call.
    The discovered address and characteristic UUIDs are cached for reconnects.
    """

    def __init__(self, address: str = ""):
        self.address          = address
        self.on_data_callback = None   # callable(dict[str, float]) or None
        self.is_connected     = False
        self._client          = None
        self._buffer          = bytearray()
        self._lock            = asyncio.Lock()
        # Set during probe (or kept as None to re-probe on connection failure)
        self._write_uuid: Optional[str]  = None
        self._notify_uuid: Optional[str] = None

    def _on_disconnect(self, client):
        self.is_connected = False
        log.warning("LiTime %s disconnected unexpectedly", self.address)

    async def connect(self):
        from bleak import BleakClient

        # Auto-discover if we have no address yet (or lost it and need re-probe)
        if not self.address:
            result = await probe_for_litime()
            if result is None:
                raise RuntimeError("No LiTime BMS found during BLE probe")
            self.address, self._write_uuid, self._notify_uuid = result

        self._client = BleakClient(
            self.address,
            timeout=20.0,
            disconnected_callback=self._on_disconnect,
        )
        await self._client.connect()
        self.is_connected = True
        self._buffer.clear()

        notify_uuid = self._notify_uuid or _fallback_notify(self._client)
        await self._client.start_notify(notify_uuid, self._notification_handler)
        self._notify_uuid = notify_uuid
        log.info("LiTime %s connected", self.address)

    async def disconnect(self):
        if self._client:
            if self.is_connected:
                try:
                    if self._notify_uuid:
                        await self._client.stop_notify(self._notify_uuid)
                    await self._client.disconnect()
                except Exception:
                    pass
            self.is_connected = False
            self._client = None

    async def poll(self):
        """Send a c_13 data request."""
        if not (self._client and self.is_connected):
            return
        write_uuid = self._write_uuid or _fallback_write(self._client)
        async with self._lock:
            await self._client.write_gatt_char(write_uuid, build_frame(0x13), response=True)

    def _notification_handler(self, sender, data: bytes):
        self._buffer.extend(data)
        while True:
            idx = self._buffer.find(_C13_ANCHOR)
            if idx == -1:
                if len(self._buffer) > 300:
                    self._buffer = self._buffer[-10:]
                break

            frame_start = idx - 3
            if frame_start < 0:
                break

            # Verify the two zero-prefix bytes at frame start.
            if self._buffer[frame_start] != 0x00 or self._buffer[frame_start + 1] != 0x00:
                self._buffer = self._buffer[idx + 4:]
                continue

            if frame_start + FRAME_LEN > len(self._buffer):
                break

            frame = bytes(self._buffer[frame_start:frame_start + FRAME_LEN])
            self._buffer = self._buffer[frame_start + FRAME_LEN:]

            fields = parse_litime_frame(frame)
            if fields is None:
                log.warning("LiTime %s: bad checksum, frame discarded", self.address)
                continue

            if self.on_data_callback:
                try:
                    self.on_data_callback(fields)
                except Exception as exc:
                    log.error("LiTime callback error: %s", exc)


def _fallback_write(client) -> str:
    """Return first writable characteristic UUID — used when probe didn't run."""
    for svc in client.services:
        for c in svc.characteristics:
            if "write" in c.properties or "write-without-response" in c.properties:
                return c.uuid
    raise RuntimeError("No writable characteristic found on connected BMS")


def _fallback_notify(client) -> str:
    """Return first notifiable characteristic UUID — used when probe didn't run."""
    for svc in client.services:
        for c in svc.characteristics:
            if "notify" in c.properties:
                return c.uuid
    raise RuntimeError("No notifiable characteristic found on connected BMS")
