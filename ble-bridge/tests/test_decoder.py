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
