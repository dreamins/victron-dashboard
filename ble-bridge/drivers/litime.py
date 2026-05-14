"""LiTime BMS active BLE driver: connect, poll c_13 every 5s, parse 105-byte response."""
import asyncio
import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)

CHAR_WRITE  = "0000ffe2-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000ffe1-0000-1000-8000-00805f9b34fb"

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


class LiTimeBMS:
    """Active BLE client for the LiTime BMS. Connect, then poll() every 5 s."""

    def __init__(self, address: str):
        self.address          = address
        self.on_data_callback = None   # callable(dict[str, float]) or None
        self.is_connected     = False
        self._client          = None
        self._buffer          = bytearray()
        self._lock            = asyncio.Lock()

    def _on_disconnect(self, client):
        self.is_connected = False
        log.warning("LiTime %s disconnected unexpectedly", self.address)

    async def connect(self):
        from bleak import BleakClient
        self._client = BleakClient(
            self.address,
            timeout=20.0,
            disconnected_callback=self._on_disconnect,
        )
        await self._client.connect()
        self.is_connected = True
        self._buffer.clear()
        await self._client.start_notify(CHAR_NOTIFY, self._notification_handler)
        log.info("LiTime %s connected", self.address)

    async def disconnect(self):
        if self._client:
            if self.is_connected:
                try:
                    await self._client.stop_notify(CHAR_NOTIFY)
                    await self._client.disconnect()
                except Exception:
                    pass
            self.is_connected = False
            self._client = None

    async def poll(self):
        """Send a c_13 data request."""
        if not (self._client and self.is_connected):
            return
        async with self._lock:
            await self._client.write_gatt_char(CHAR_WRITE, build_frame(0x13), response=True)

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
                except Exception as e:
                    log.error("LiTime callback error: %s", e)
