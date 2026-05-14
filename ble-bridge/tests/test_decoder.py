"""
Unit tests for ble-bridge/drivers/victron.py.

Uses the same real packet captures as decoder/tests/test_field_getters.py
to verify the decode_advertisement() function in the BLE bridge driver.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from drivers.victron import decode_advertisement, FIELD_GETTERS

# Real production packets (same as decoder/tests/test_field_getters.py)
_MOCK_SOLAR_KEY       = "00112233445566778899aabbccddeeff"
_MOCK_SOLAR_PKT = "100274a00175ae8611181ecabb68d8ccfbf5085c"

_MOCK_SENSE_KEY      = "ffeeddccbbaa99887766554433221100"
_MOCK_SENSE_PKT   = "1000a5a302a3aeab8bada0fa6e193392139606d9445774"


class TestFieldGetterList:
    def test_pv_power_getter(self):
        assert ("pv_power", "get_solar_power") in FIELD_GETTERS

    def test_charge_current_getter(self):
        assert ("charge_current", "get_battery_charging_current") in FIELD_GETTERS

    def test_battery_voltage_fallback_for_sense(self):
        methods = [m for f, m in FIELD_GETTERS if f == "battery_voltage"]
        assert "get_voltage" in methods

    def test_temperature_getter(self):
        assert ("temperature", "get_temperature") in FIELD_GETTERS


class TestDecodeAdvertisement:
    def test_solar_charger_returns_dict(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        result = decode_advertisement(raw, _MOCK_SOLAR_KEY)
        assert result is not None
        assert isinstance(result, dict)

    def test_solar_charger_has_pv_power(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        result = decode_advertisement(raw, _MOCK_SOLAR_KEY)
        assert "pv_power" in result
        assert result["pv_power"] >= 0

    def test_solar_charger_has_battery_voltage(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        result = decode_advertisement(raw, _MOCK_SOLAR_KEY)
        assert "battery_voltage" in result
        assert 10.0 < result["battery_voltage"] < 17.0

    def test_solar_charger_has_charge_state(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        result = decode_advertisement(raw, _MOCK_SOLAR_KEY)
        assert "charge_state" in result

    def test_battery_sense_has_temperature(self):
        raw = bytes.fromhex(_MOCK_SENSE_PKT)
        result = decode_advertisement(raw, _MOCK_SENSE_KEY)
        assert result is not None
        assert "temperature" in result
        assert -20.0 < result["temperature"] < 80.0

    def test_battery_sense_has_voltage(self):
        raw = bytes.fromhex(_MOCK_SENSE_PKT)
        result = decode_advertisement(raw, _MOCK_SENSE_KEY)
        assert "battery_voltage" in result

    def test_wrong_key_returns_none_or_empty(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        result = decode_advertisement(raw, "00" * 16)
        # Either None or empty dict — both are acceptable failure modes
        assert not result

    def test_garbage_bytes_returns_none(self):
        result = decode_advertisement(b"\x00" * 20, _MOCK_SOLAR_KEY)
        assert not result


class TestBmsScanWait:
    """_wait_for_bms_in_scan resolves immediately when scanner fires the event."""

    def test_fires_when_event_set(self):
        import asyncio
        import ble_bridge

        async def _run():
            mac = "AA:BB:CC:DD:EE:FF"
            ble_bridge._victron_scanner = object()

            async def _trigger():
                await asyncio.sleep(0.05)
                # Fire the event that _wait_for_bms_in_scan registered
                if mac in ble_bridge._bms_seen_events:
                    ble_bridge._bms_seen_events[mac].set()

            asyncio.create_task(_trigger())
            seen = await ble_bridge._wait_for_bms_in_scan(mac, timeout=2.0)
            ble_bridge._victron_scanner = None
            return seen

        assert asyncio.run(_run()) is True

    def test_times_out_when_never_seen(self):
        import asyncio
        import ble_bridge

        async def _run():
            ble_bridge._victron_scanner = object()
            seen = await ble_bridge._wait_for_bms_in_scan("00:11:22:33:44:55", timeout=0.1)
            ble_bridge._victron_scanner = None
            return seen

        assert asyncio.run(_run()) is False

    def test_returns_false_without_scanner(self):
        import asyncio
        import ble_bridge

        async def _run():
            ble_bridge._victron_scanner = None
            return await ble_bridge._wait_for_bms_in_scan("AA:BB:CC:DD:EE:FF", timeout=5.0)

        assert asyncio.run(_run()) is False


class TestPersistMac:
    def test_writes_mac_to_sites_json(self, tmp_path):
        import json
        from ble_bridge import _persist_mac

        sites = tmp_path / "sites.json"
        sites.write_text(json.dumps({"sites": [{"id": "garage", "devices": [
            {"id": "litime_main", "label": "LiTime Battery", "type": "litime_bms"}
        ]}]}))

        _persist_mac(str(sites), "garage", "litime_main", "AA:BB:CC:DD:EE:75")

        data = json.loads(sites.read_text())
        dev = data["sites"][0]["devices"][0]
        assert dev["mac"] == "AA:BB:CC:DD:EE:75"

    def test_missing_file_does_not_raise(self, tmp_path):
        from ble_bridge import _persist_mac
        # Should log an error but not crash
        _persist_mac(str(tmp_path / "nonexistent.json"), "garage", "litime_main", "AA:BB:CC:DD:EE:FF")

    def test_wrong_device_id_leaves_file_unchanged(self, tmp_path):
        import json
        from ble_bridge import _persist_mac

        original = {"sites": [{"id": "garage", "devices": [
            {"id": "litime_main", "label": "LiTime Battery", "type": "litime_bms"}
        ]}]}
        sites = tmp_path / "sites.json"
        sites.write_text(json.dumps(original))

        _persist_mac(str(sites), "garage", "wrong_id", "AA:BB:CC:DD:EE:FF")

        data = json.loads(sites.read_text())
        assert "mac" not in data["sites"][0]["devices"][0]


class TestFixtureReplay:
    def test_fixture_file_parsed(self, tmp_path):
        import json
        from ble_bridge import load_device_map, run_fixture_mode

        sites = tmp_path / "sites.json"
        sites.write_text(json.dumps({"sites": [{"id": "garage", "devices": [
            {"id": "g1", "label": "G1", "mac": "AA:BB:CC:DD:EE:11", "key": "aabb"},
            {"id": "g2", "label": "G2", "mac": "AA:BB:CC:DD:EE:22", "key": "ccdd"},
        ]}]}))

        packets = tmp_path / "packets.jsonl"
        packets.write_text(
            '{"mac": "AA:BB:CC:DD:EE:11", "raw": {"pv_power": 100.0, "battery_voltage": 13.2}}\n'
            '{"mac": "AA:BB:CC:DD:EE:22", "raw": {"pv_power": 80.0, "battery_voltage": 13.1}}\n'
        )

        written = []

        class MockWriter:
            def write(self, point):
                written.append(point)

        dmap = load_device_map(str(sites))
        count = run_fixture_mode(str(packets), dmap, MockWriter())
        assert count == 2
        assert len(written) == 2

    def test_unknown_mac_skipped_gracefully(self, tmp_path):
        import json
        from ble_bridge import load_device_map, run_fixture_mode

        sites = tmp_path / "sites.json"
        sites.write_text(json.dumps({"sites": [{"id": "garage", "devices": []}]}))

        packets = tmp_path / "packets.jsonl"
        packets.write_text('{"mac": "FF:FF:FF:FF:FF:FF", "raw": {"pv_power": 50.0}}\n')

        written = []

        class MockWriter:
            def write(self, point):
                written.append(point)

        dmap = load_device_map(str(sites))
        count = run_fixture_mode(str(packets), dmap, MockWriter())
        assert count == 1  # still written — device_id falls back to mac hex
        assert len(written) == 1


class TestLoadDeviceMap:
    def test_loads_garage_site(self, tmp_path):
        import json
        from ble_bridge import load_device_map
        fixture = tmp_path / "sites.json"
        fixture.write_text(json.dumps({
            "sites": [{
                "id": "garage",
                "devices": [{
                    "id": "g1", "label": "G1",
                    "mac": "AA:BB:CC:DD:EE:11", "key": "aabb",
                    "type": "victron_mppt"
                }]
            }]
        }))
        dmap = load_device_map(str(fixture))
        assert "AA:BB:CC:DD:EE:11" in dmap
        assert dmap["AA:BB:CC:DD:EE:11"]["site_id"] == "garage"
        assert dmap["AA:BB:CC:DD:EE:11"]["device_id"] == "g1"

    def test_missing_file_returns_empty(self, tmp_path):
        from ble_bridge import load_device_map
        result = load_device_map(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_mac_stored_in_entry(self, tmp_path):
        import json
        from ble_bridge import load_device_map
        fixture = tmp_path / "sites.json"
        fixture.write_text(json.dumps({
            "sites": [{"id": "garage", "devices": [
                {"id": "g1", "label": "G1", "mac": "AA:BB:CC:DD:EE:11",
                 "key": "aabb", "type": "victron_mppt"}
            ]}]
        }))
        dmap = load_device_map(str(fixture))
        assert dmap["AA:BB:CC:DD:EE:11"]["mac"] == "AA:BB:CC:DD:EE:11"

    def test_litime_bms_no_mac_included(self, tmp_path):
        """litime_bms without a mac must still appear in device map (auto-probe path)."""
        import json
        from ble_bridge import load_device_map
        fixture = tmp_path / "sites.json"
        fixture.write_text(json.dumps({
            "sites": [{"id": "garage", "devices": [
                {"id": "litime_main", "label": "LiTime Battery", "type": "litime_bms"}
            ]}]
        }))
        dmap = load_device_map(str(fixture))
        entries = [v for v in dmap.values() if v["device_id"] == "litime_main"]
        assert len(entries) == 1
        assert entries[0]["mac"] == ""
        assert entries[0]["type"] == "litime_bms"

    def test_victron_no_mac_excluded(self, tmp_path):
        """Victron devices without a mac must be skipped — they require MAC for BLE matching."""
        import json
        from ble_bridge import load_device_map
        fixture = tmp_path / "sites.json"
        fixture.write_text(json.dumps({
            "sites": [{"id": "garage", "devices": [
                {"id": "mppt_bad", "label": "MPPT", "type": "victron_mppt"}
            ]}]
        }))
        dmap = load_device_map(str(fixture))
        entries = [v for v in dmap.values() if v["device_id"] == "mppt_bad"]
        assert len(entries) == 0

    def test_two_litime_bms_without_mac(self, tmp_path):
        """Two BMS devices without MACs must both appear using distinct synthetic keys."""
        import json
        from ble_bridge import load_device_map
        fixture = tmp_path / "sites.json"
        fixture.write_text(json.dumps({
            "sites": [{"id": "garage", "devices": [
                {"id": "bms_a", "label": "Battery A", "type": "litime_bms"},
                {"id": "bms_b", "label": "Battery B", "type": "litime_bms"},
            ]}]
        }))
        dmap = load_device_map(str(fixture))
        ids = {v["device_id"] for v in dmap.values()}
        assert "bms_a" in ids
        assert "bms_b" in ids
        # Each gets its own synthetic key (no MAC collision)
        assert len(dmap) == 2


# ─── LiTime parser ────────────────────────────────────────────────────────────

import struct as _struct


def _make_litime_frame(voltage_mv=13200, current_ma=-2500, soc=85, soh=99,
                       cycles=15, temp=22, cell_mv=3300, bad_checksum=False):
    """Build a synthetic 105-byte c_13 response frame for testing."""
    frame = bytearray(105)
    frame[0] = 0x00
    frame[1] = 0x00
    # c_13 anchor at [3:7]
    frame[3] = 0x01
    frame[4] = 0x93
    frame[5] = 0x55
    frame[6] = 0xAA
    _struct.pack_into('<H', frame, 12, voltage_mv)
    for i in range(16):
        _struct.pack_into('<H', frame, 16 + i * 2, cell_mv)
    _struct.pack_into('<i', frame, 48, current_ma)
    frame[52] = temp & 0xFF
    _struct.pack_into('<H', frame, 90, soc)
    _struct.pack_into('<H', frame, 92, soh)
    _struct.pack_into('<H', frame, 96, cycles)
    frame[104] = sum(frame[2:104]) & 0xFF
    if bad_checksum:
        frame[104] ^= 0xFF
    return bytes(frame)


class TestLiTimeParser:
    def test_valid_frame_returns_dict(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame())
        assert result is not None
        assert isinstance(result, dict)

    def test_voltage_correct(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(voltage_mv=13200))
        assert abs(result["battery_voltage"] - 13.200) < 0.001

    def test_current_inverted_positive_discharge(self):
        # BMS reports positive = discharge; bridge inverts so positive = charging
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(current_ma=2500))   # 2.5A discharge
        assert result["battery_current"] == pytest.approx(-2.5, abs=0.001)

    def test_current_charging_is_positive(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(current_ma=-2500))  # 2.5A charging
        assert result["battery_current"] == pytest.approx(2.5, abs=0.001)

    def test_soc_correct(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(soc=85))
        assert result["soc"] == 85.0

    def test_soh_correct(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(soh=99))
        assert result["soh"] == 99.0

    def test_cycles_correct(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(cycles=15))
        assert result["cycles"] == 15.0

    def test_temperature_correct(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(temp=22))
        assert result["temperature"] == 22.0

    def test_temperature_negative(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(temp=-5))
        assert result["temperature"] == -5.0

    def test_cell_stats_uniform(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(cell_mv=3300))
        assert result["cell_min"] == pytest.approx(3.3, abs=0.001)
        assert result["cell_max"] == pytest.approx(3.3, abs=0.001)
        assert result["cell_avg"] == pytest.approx(3.3, abs=0.001)

    def test_bad_checksum_returns_none(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(_make_litime_frame(bad_checksum=True))
        assert result is None

    def test_short_frame_returns_none(self):
        from drivers.litime import parse_litime_frame
        result = parse_litime_frame(b"\x00" * 50)
        assert result is None

    def test_build_frame_c13_structure(self):
        from drivers.litime import build_frame
        frame = build_frame(0x13)
        assert len(frame) == 8
        assert frame[0] == 0x00
        assert frame[1] == 0x00
        assert frame[3] == 0x01
        assert frame[4] == 0x13
        assert frame[5] == 0x55
        assert frame[6] == 0xAA
        assert frame[7] == (0x04 + 0x13) & 0xFF

    def test_notification_handler_split_frame(self):
        """Frame delivered in two BLE notification chunks must be reassembled."""
        from drivers.litime import LiTimeBMS
        bms = LiTimeBMS("TEST:MAC")
        received = []
        bms.on_data_callback = received.append

        full = _make_litime_frame()
        mid = len(full) // 2
        bms._notification_handler(None, full[:mid])
        assert len(received) == 0, "Should not parse incomplete frame"
        bms._notification_handler(None, full[mid:])
        assert len(received) == 1, "Should parse after full frame received"
        assert "soc" in received[0]

    def test_notification_handler_two_consecutive_frames(self):
        """Two back-to-back frames in one notification should both be parsed."""
        from drivers.litime import LiTimeBMS
        bms = LiTimeBMS("TEST:MAC")
        received = []
        bms.on_data_callback = received.append

        frame1 = _make_litime_frame(soc=80)
        frame2 = _make_litime_frame(soc=81)
        bms._notification_handler(None, frame1 + frame2)
        assert len(received) == 2
