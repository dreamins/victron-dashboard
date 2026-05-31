"""Tests for run_bms_poller and BridgeController._scanner_watchdog in ble_bridge.py.

Runs with standard pytest (no pytest-asyncio needed) by calling asyncio.run()
directly in each test function.

Covers:
  - Normal connect → poll → data write cycle
  - Regression: poller must NOT tight-loop when scanner is None
    (old code had `continue` that caused infinite busy-spin with no sleep or connect)
  - Backoff increments on connection failure
  - Backoff resets on successful connect
  - Scanner watchdog power-cycles BT adapter after 5 consecutive failures
  - Scanner watchdog sends SIGTERM after 10 consecutive failures
  - Scanner watchdog resets failure counter when scanner runs successfully
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ble_bridge
from ble_bridge import run_bms_poller, _bt_watchdog_thread, BridgeController


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
    ble_bridge._poller_heartbeat.clear()


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


# ── Poll timeout ─────────────────────────────────────────────────────────────

def test_poller_poll_timeout_triggers_reconnect():
    """If poll() deadlocks (BlueZ notification hang), asyncio.wait_for raises
    TimeoutError which breaks the poll loop and triggers a reconnect cycle."""
    _reset()
    writer      = _MockWriter()
    sleep_calls = []

    class _HangingPollBMS(_MockBMS):
        async def poll(self):
            self.poll_calls += 1
            raise asyncio.TimeoutError("injected poll timeout")

    bms = _HangingPollBMS("AA:BB:CC:DD:EE:FF")

    async def _fake_sleep(t):
        sleep_calls.append(t)

    async def _go():
        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())
    assert bms.connect_calls == 2, (
        f"Poller should reconnect after poll timeout, got {bms.connect_calls} connects"
    )
    assert 5 in sleep_calls, f"Expected 5s backoff after poll timeout, got: {sleep_calls}"


def test_poller_disconnect_timeout_is_swallowed():
    """If disconnect() deadlocks, TimeoutError is swallowed in the finally block —
    the poller must not crash and must retry the connect cycle."""
    _reset()

    class _HangingDisconnectBMS(_MockBMS):
        async def disconnect(self):
            self.disconnect_calls += 1
            self._connected = False
            raise asyncio.TimeoutError("injected disconnect timeout")

    bms    = _HangingDisconnectBMS("AA:BB:CC:DD:EE:FF", disconnect_after_polls=1)
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())
    assert bms.connect_calls == 2, (
        f"Poller should retry after disconnect timeout, "
        f"but connect was only called {bms.connect_calls} time(s)."
    )


# ── Connect timeout ───────────────────────────────────────────────────────────

def test_poller_connect_timeout_triggers_retry():
    """asyncio.wait_for(bms.connect(), timeout=30) must not deadlock.

    If bms.connect() raises TimeoutError the poller catches it as a generic
    exception, applies backoff, and retries — exactly like any other failure.
    """
    _reset()
    writer      = _MockWriter()
    sleep_calls = []

    class _TimeoutBMS(_MockBMS):
        async def connect(self):
            self.connect_calls += 1
            raise asyncio.TimeoutError("injected connect timeout")

    bms = _TimeoutBMS("AA:BB:CC:DD:EE:FF")

    async def _fake_sleep(t):
        sleep_calls.append(t)

    async def _go():
        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())
    assert bms.connect_calls == 2, "Poller should retry after TimeoutError"
    assert 5 in sleep_calls, f"Expected 5s backoff after timeout, got: {sleep_calls}"


# ── Scanner watchdog recovery ladder ─────────────────────────────────────────

def test_watchdog_restarts_bluetooth_and_sigterms_at_5():
    """After 5 consecutive scanner exits, watchdog restarts bluetooth.service
    via systemd D-Bus then sends SIGTERM so Docker restarts the container.

    Regression: the original fix only power-cycled the adapter (Powered=false/
    true) which does not reload Realtek USB firmware after a power glitch.
    The correct fix is to restart the BlueZ daemon and then let Docker restart
    the container with a fresh D-Bus connection.
    """
    _reset()
    real_sleep = asyncio.sleep  # save before patching

    async def _go():
        writer = _MockWriter()
        ctrl   = BridgeController({}, writer)

        bt_restarted = []
        sigtermed    = []

        async def _fast_sleep(t):
            await real_sleep(0)  # yield to event loop without waiting

        async def _failing_impl(*a, **kw):
            raise OSError("org.bluez.Error.NotReady")

        async def _mock_restart_bt():
            bt_restarted.append(True)
            return True

        def _mock_kill(pid, sig):
            sigtermed.append(sig)
            raise SystemExit(0)  # stop the watchdog loop

        # Start with an already-failing scanner task
        ctrl._scanner_task = asyncio.create_task(_failing_impl())

        # run_ble_scanner is async def so patch auto-creates AsyncMock.
        # side_effect on AsyncMock is called when the coroutine is awaited,
        # so setting it to an async function that raises works correctly.
        with patch("asyncio.sleep", side_effect=_fast_sleep), \
             patch("ble_bridge._restart_bluetooth_service", side_effect=_mock_restart_bt), \
             patch("ble_bridge.run_ble_scanner", new=AsyncMock(side_effect=_failing_impl)), \
             patch("os.kill", side_effect=_mock_kill), \
             patch("sys.exit"):
            try:
                await ctrl._scanner_watchdog()
            except SystemExit:
                pass

        assert len(bt_restarted) == 1, (
            f"Expected exactly one bluetooth restart at fail #5, got {len(bt_restarted)}"
        )
        assert len(sigtermed) >= 1, (
            f"Expected SIGTERM at fail #5, got {len(sigtermed)}"
        )

    asyncio.run(_go())


def test_watchdog_no_power_cycle_when_scanner_healthy():
    """Counter stays 0 (never reaches 5) when the scanner runs without exiting.

    This verifies the else-branch reset: _consec_fails = 0 fires every
    watchdog cycle where t.done() is False, preventing false escalation.
    """
    _reset()
    real_sleep = asyncio.sleep

    async def _go():
        writer       = _MockWriter()
        ctrl         = BridgeController({}, writer)
        power_cycled = []
        cycles       = [0]

        async def _fast_sleep(t):
            cycles[0] += 1
            if cycles[0] >= 20:  # 20 watchdog cycles is well past the threshold
                raise SystemExit(0)
            await real_sleep(0)

        async def _healthy_impl(*a, **kw):
            await real_sleep(3600)  # stays alive; cancelled on asyncio.run() teardown

        async def _mock_fsd(power_cycle=False):
            if power_cycle:
                power_cycled.append(True)

        ctrl._scanner_task = asyncio.create_task(_healthy_impl())

        with patch("asyncio.sleep", side_effect=_fast_sleep), \
             patch("ble_bridge._force_stop_discovery", side_effect=_mock_fsd), \
             patch("ble_bridge.run_ble_scanner", new=AsyncMock(side_effect=_healthy_impl)), \
             patch("os.kill"), \
             patch("sys.exit"):
            try:
                await ctrl._scanner_watchdog()
            except SystemExit:
                pass

        assert len(power_cycled) == 0, (
            f"Healthy scanner triggered unexpected power-cycle after {cycles[0]} cycles"
        )

    asyncio.run(_go())


# ── Disconnect exception regression ──────────────────────────────────────────

def test_poller_survives_disconnect_exception():
    """Regression: bms.disconnect() raising in the finally block must NOT kill
    the poller.  Before the fix, the exception escaped the finally, blew past
    the while-loop, and the BMS never retried — producing a permanent bridge-
    offline state with only a one-liner error in the logs."""
    _reset()

    class _FailingDisconnectBMS(_MockBMS):
        async def disconnect(self):
            self.disconnect_calls += 1
            self._connected = False
            raise OSError("BlueZ disconnect error (injected)")

    bms    = _FailingDisconnectBMS("AA:BB:CC:DD:EE:FF", disconnect_after_polls=1)
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=2)

    _run(_go())
    assert bms.connect_calls == 2, (
        f"Poller should have retried after disconnect exception, "
        f"but connect was only called {bms.connect_calls} time(s)."
    )


# ── Watchdog ──────────────────────────────────────────────────────────────────

def test_poller_updates_heartbeat_on_success():
    """_poller_heartbeat[dev_id] must be updated after a successful poll."""
    _reset()
    bms    = _MockBMS("AA:BB:CC:DD:EE:FF")
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    _run(_go())
    assert BMS_INFO["device_id"] in ble_bridge._poller_heartbeat, (
        "_poller_heartbeat was not updated by poller"
    )
    assert ble_bridge._poller_heartbeat[BMS_INFO["device_id"]] > 0


def test_poller_updates_heartbeat_on_error():
    """_poller_heartbeat must also update on connection failure so the watchdog
    doesn't trigger a hard reset for ordinary 'device not found' errors."""
    _reset()
    bms    = _MockBMS("AA:BB:CC:DD:EE:FF", fail_first_n=999)
    writer = _MockWriter()

    async def _go():
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await run_bms_poller(BMS_INFO, writer,
                                 bms_factory=lambda m: bms,
                                 _max_cycles=1)

    before = time.monotonic()
    _run(_go())
    after  = time.monotonic()
    ts = ble_bridge._poller_heartbeat.get(BMS_INFO["device_id"], 0.0)
    assert before <= ts <= after + 1, (
        "Heartbeat timestamp not updated on connection error"
    )


def test_watchdog_resets_adapter_when_poller_frozen(monkeypatch):
    """Watchdog thread: if _poller_heartbeat is stale > _BT_DEAD_S,
    it must run hciconfig reset exactly once, then stop when heartbeat recovers."""
    _reset()
    # Inject a stale timestamp so the watchdog fires on the very first check.
    ble_bridge._poller_heartbeat["test_bms"] = (
        time.monotonic() - ble_bridge._BT_DEAD_S - 10
    )
    reset_calls = []
    sleep_count = [0]

    def _fake_run(cmd, **kw):
        reset_calls.append(cmd)
        class _R: returncode = 0
        return _R()

    def _fake_sleep(t):
        sleep_count[0] += 1
        if sleep_count[0] == 2:
            # After the reset iteration, refresh heartbeat so next check is clean.
            ble_bridge._poller_heartbeat["test_bms"] = time.monotonic()
        if sleep_count[0] >= 3:
            # Two checks ran — stop the infinite loop via exception.
            raise RuntimeError("watchdog-test-stop")

    monkeypatch.setattr(time, "sleep", _fake_sleep)
    monkeypatch.setattr("ble_bridge.subprocess.run", _fake_run)

    try:
        _bt_watchdog_thread()
    except RuntimeError as e:
        assert str(e) == "watchdog-test-stop"

    assert any("hciconfig" in str(c) for c in reset_calls), (
        f"Expected hciconfig reset call, got: {reset_calls}"
    )
    assert len(reset_calls) == 1, f"Expected exactly one reset, got: {reset_calls}"


# ── _scanner_start() InProgress retry ────────────────────────────────────────

def test_scanner_start_power_cycles_on_third_attempt():
    """_scanner_start() must call _force_stop_discovery(power_cycle=True) on
    the third InProgress attempt (index 2), not on the first two."""
    _reset()
    power_cycle_calls = []
    inprogress = Exception("[org.bluez.Error.InProgress] Operation already in progress")

    class _MockScanner:
        def __init__(self):
            self.start_calls = 0

        async def start(self):
            self.start_calls += 1
            raise inprogress

    scanner = _MockScanner()
    ble_bridge._victron_scanner = scanner

    async def _fake_force_stop(power_cycle=False):
        power_cycle_calls.append(power_cycle)
        await asyncio.sleep(0)

    async def _go():
        with patch("ble_bridge._force_stop_discovery", side_effect=_fake_force_stop):
            await ble_bridge._scanner_start()

    _run(_go())
    ble_bridge._victron_scanner = None

    assert scanner.start_calls == 4, f"Expected 4 start attempts, got {scanner.start_calls}"
    assert power_cycle_calls == [False, False, True], (
        f"Expected [False, False, True] for power_cycle calls, got {power_cycle_calls}"
    )


def test_scanner_watchdog_restarts_when_not_discovering():
    """BridgeController._scanner_watchdog cancels the scanner task when
    _is_discovering() returns False, allowing the watchdog to restart it."""
    from ble_bridge import BridgeController

    async def _go():
        ctrl = BridgeController({}, MagicMock())
        ctrl._scanner_task = asyncio.create_task(asyncio.sleep(9999))

        sleep_count = [0]
        cancelled   = []

        async def _fake_is_discovering():
            return False

        async def _fake_sleep(t):
            sleep_count[0] += 1
            if sleep_count[0] >= 5:
                raise RuntimeError("watchdog-test-stop")

        original_cancel = ctrl._scanner_task.cancel

        def _tracking_cancel(*args, **kwargs):
            cancelled.append(True)
            return original_cancel(*args, **kwargs)

        ctrl._scanner_task.cancel = _tracking_cancel

        with patch("ble_bridge._is_discovering", side_effect=_fake_is_discovering), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            try:
                await ctrl._scanner_watchdog()
            except RuntimeError as e:
                if "watchdog-test-stop" not in str(e):
                    raise

        return cancelled

    cancelled = _run(_go())
    assert cancelled, "Watchdog should have cancelled the scanner task when Discovering=false"
