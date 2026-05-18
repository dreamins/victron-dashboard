"""Tests for run_bms_poller in ble_bridge.py — all BLE interactions mocked.

Runs with standard pytest (no pytest-asyncio needed) by calling asyncio.run()
directly in each test function.

Covers:
  - Normal connect → poll → data write cycle
  - Regression: poller must NOT tight-loop when scanner is None
    (old code had `continue` that caused infinite busy-spin with no sleep or connect)
  - Backoff increments on connection failure
  - Backoff resets on successful connect
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ble_bridge
from ble_bridge import run_bms_poller


BMS_INFO = {
    "mac":         "AA:BB:CC:DD:EE:FF",
    "site_id":     "test_site",
    "device_id":   "test_bms",
    "label":       "Test BMS",
    "write_uuid":  "aaaa-1111",
    "notify_uuid": "bbbb-2222",
}


class _MockBMS:
    """LiTimeBMS stand-in. Fails connect the first `fail_first_n` calls."""

    def __init__(self, mac, fail_first_n=0, disconnect_after_polls=1):
        self.address           = mac
        self._write_uuid       = None
        self._notify_uuid      = None
        self.on_data_callback  = None
        self._connected        = False
        self._fail_first_n     = fail_first_n
        self._disconnect_after = disconnect_after_polls
        self._poll_count       = 0
        self.connect_calls     = 0
        self.poll_calls        = 0
        self.disconnect_calls  = 0

    @property
    def is_connected(self):
        return self._connected

    async def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self._fail_first_n:
            raise OSError("BLE connect failed (injected)")
        self._connected  = True
        self._poll_count = 0

    async def poll(self):
        self.poll_calls  += 1
        self._poll_count += 1
        if self.on_data_callback:
            self.on_data_callback({
                "soc": 85.0,
                "battery_voltage": 13.2,
                "battery_current": -1.0,
            })
        if self._poll_count >= self._disconnect_after:
            self._connected = False

    async def disconnect(self):
        self.disconnect_calls += 1
        self._connected = False


class _MockWriter:
    def __init__(self):
        self.written = []

    def write(self, point):
        self.written.append(point)


def _reset():
    ble_bridge._victron_scanner = None
    ble_bridge._bms_seen_events.clear()
    ble_bridge._last_written.clear()


def _run(coro):
    """Run a coroutine in a fresh event loop (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# ── Normal path ───────────────────────────────────────────────────────────────

def test_poller_connects_and_writes_data():
    _reset()
    bms    = _MockBMS("AA:BB:CC:DD:EE:FF")
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    _run(_go())
    assert bms.connect_calls == 1
    assert bms.poll_calls >= 1
    assert len(writer.written) >= 1


def test_poller_reconnects_after_disconnect():
    _reset()
    bms    = _MockBMS("AA:BB:CC:DD:EE:FF", disconnect_after_polls=1)
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())
    assert bms.connect_calls == 2
    assert bms.poll_calls == 2


# ── Tight-loop regression ─────────────────────────────────────────────────────

def test_poller_falls_through_when_scanner_none():
    """Regression: _victron_scanner is None → must sleep 30s then attempt connect.

    Before the fix: `continue` caused an infinite busy loop — _wait_for_bms_in_scan
    returned False immediately, then `continue` jumped back to it with no sleep and
    no connection attempt, leaving the battery card grey forever.
    """
    _reset()
    bms         = _MockBMS("AA:BB:CC:DD:EE:FF")
    writer      = _MockWriter()
    sleep_calls = []

    async def _fake_sleep(t):
        sleep_calls.append(t)

    async def _go():
        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    _run(_go())

    assert 30.0 in sleep_calls, (
        f"Expected 30s scanner-down sleep, got: {sleep_calls}. "
        "Possible tight-loop regression."
    )
    assert bms.connect_calls == 1


def test_poller_no_scanner_down_sleep_when_bms_seen():
    """When BMS is seen in scan, the 30s scanner-down sleep must NOT happen."""
    _reset()
    bms         = _MockBMS("AA:BB:CC:DD:EE:FF")
    writer      = _MockWriter()
    sleep_calls = []

    async def _fake_sleep(t):
        sleep_calls.append(t)

    async def _go():
        mock_scanner = MagicMock()
        mock_scanner.stop  = AsyncMock()
        mock_scanner.start = AsyncMock()
        ble_bridge._victron_scanner = mock_scanner

        with patch("ble_bridge._wait_for_bms_in_scan",
                   new_callable=AsyncMock, return_value=True), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    _run(_go())
    assert 30.0 not in sleep_calls
    assert bms.connect_calls == 1


# ── Backoff ───────────────────────────────────────────────────────────────────

def test_poller_increments_backoff_on_consecutive_failures():
    """Two failures in a row: sleep 5s after first, 10s after second."""
    _reset()
    bms         = _MockBMS("AA:BB:CC:DD:EE:FF", fail_first_n=999)  # always fails
    writer      = _MockWriter()
    sleep_calls = []

    async def _fake_sleep(t):
        sleep_calls.append(t)

    async def _go():
        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())

    backoff_delays = [t for t in sleep_calls if t in (5, 10, 20, 40)]
    assert 5  in backoff_delays, f"Expected 5s after first failure, got: {sleep_calls}"
    assert 10 in backoff_delays, f"Expected 10s after second failure, got: {sleep_calls}"


def test_poller_resets_backoff_on_success():
    """After a failure then a success, backoff returns to 5s (not 10s)."""
    _reset()
    # fail_first_n=1: first connect fails, second and onwards succeed
    bms         = _MockBMS("AA:BB:CC:DD:EE:FF", fail_first_n=1)
    writer      = _MockWriter()
    sleep_calls = []

    async def _fake_sleep(t):
        sleep_calls.append(t)

    async def _go():
        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())

    assert 10 not in sleep_calls, (
        f"backoff_idx was not reset on success — got sleeps: {sleep_calls}"
    )


# ── UUIDs applied from info ───────────────────────────────────────────────────

def test_poller_sets_uuids_on_bms_from_info():
    _reset()
    bms    = _MockBMS("AA:BB:CC:DD:EE:FF")
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    _run(_go())
    assert bms._write_uuid  == BMS_INFO["write_uuid"]
    assert bms._notify_uuid == BMS_INFO["notify_uuid"]


def test_poller_writes_battery_point():
    _reset()
    bms    = _MockBMS("AA:BB:CC:DD:EE:FF")
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    _run(_go())
    assert len(writer.written) >= 1
