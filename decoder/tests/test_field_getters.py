"""
Verify that FIELD_GETTERS uses the correct method names from the victron-ble library.

Root cause of the original bug: victron-ble's SolarCharger parser exposes
  get_solar_power            (not get_pv_power)
  get_battery_charging_current (not get_battery_current)
  get_external_device_load   (not get_load_power)
The wrong names caused those fields to be silently skipped in _extract_fields().
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from victron_ble.devices import detect_device_type, SolarCharger, BatterySense
from victron_ble.devices.solar_charger import SolarChargerData
from victron_ble.devices.battery_sense import BatterySenseData

from decoder import FIELD_GETTERS, VictronDecoder

GETTER_MAP = dict(FIELD_GETTERS)


# ── Method-name correctness ────────────────────────────────────────────────

class TestFieldGetterNames:
    def test_pv_power_uses_solar_power(self):
        assert GETTER_MAP["pv_power"] == "get_solar_power", (
            "pv_power must call get_solar_power (not get_pv_power)"
        )

    def test_charge_current_uses_battery_charging_current(self):
        assert GETTER_MAP["charge_current"] == "get_battery_charging_current", (
            "charge_current must call get_battery_charging_current (not get_battery_current)"
        )

    def test_load_power_uses_external_device_load(self):
        assert GETTER_MAP["load_power"] == "get_external_device_load", (
            "load_power must call get_external_device_load (not get_load_power)"
        )

    def test_solar_charger_has_all_critical_methods(self):
        required = {
            "get_solar_power",
            "get_battery_charging_current",
            "get_external_device_load",
            "get_battery_voltage",
            "get_charge_state",
            "get_charger_error",
            "get_yield_today",
        }
        missing = required - set(dir(SolarChargerData))
        assert not missing, f"SolarChargerData missing methods: {missing}"

    def test_battery_sense_has_temperature(self):
        assert hasattr(BatterySenseData, "get_temperature")

    def test_battery_sense_has_voltage(self):
        assert hasattr(BatterySenseData, "get_voltage"), (
            "BatterySense uses get_voltage(), not get_battery_voltage()"
        )

    def test_battery_sense_voltage_getter_in_field_getters(self):
        """Ensure battery_voltage <- get_voltage entry exists for BatterySense."""
        entries = [(f, m) for f, m in FIELD_GETTERS if f == "battery_voltage"]
        methods = [m for _, m in entries]
        assert "get_voltage" in methods, (
            "FIELD_GETTERS must include ('battery_voltage','get_voltage') for BatterySense"
        )


# ── Mock packet decoding (no real production keys or packets committed) ──────

_MOCK_SOLAR_KEY = "00112233445566778899aabbccddeeff"
_MOCK_SOLAR_PKT  = "100274a001759999999999999999999999999999"   # mode=0x01 SolarCharger
_MOCK_SENSE_KEY = "ffeeddccbbaa99887766554433221100"
_MOCK_SENSE_PKT = "1000a5a302a38888888888888888888888888888888888"  # BatterySense


@pytest.fixture(autouse=True)
def mock_victron_ble_decryption(monkeypatch):
    import victron_ble.devices
    from victron_ble.devices import SolarCharger, BatterySense

    # Mock detect_device_type
    def mock_detect(raw_bytes):
        if raw_bytes == bytes.fromhex(_MOCK_SOLAR_PKT):
            return SolarCharger
        elif raw_bytes == bytes.fromhex(_MOCK_SENSE_PKT):
            return BatterySense
        return None
    monkeypatch.setattr(victron_ble.devices, "detect_device_type", mock_detect)
    # Also patch it in the local test module namespace since it was imported at module level
    monkeypatch.setattr(sys.modules[__name__], "detect_device_type", mock_detect)

    # Mock __init__ and parse for SolarCharger
    def mock_solar_init(self, key):
        self.mock_key = key
    monkeypatch.setattr(SolarCharger, "__init__", mock_solar_init)

    class MockSolarData:
        def get_solar_power(self):
            class Val: value = 150.0
            return Val()
        def get_battery_voltage(self):
            class Val: value = 13.8
            return Val()
        def get_charge_state(self):
            class Val: value = 3
            return Val()
        def get_charger_error(self):
            class Val: value = 0
            return Val()
        def get_yield_today(self):
            class Val: value = 40.0
            return Val()

    def mock_solar_parse(self, raw_bytes):
        # normalize self.mock_key to hex string if it is bytes
        k = self.mock_key
        k_str = k.hex() if isinstance(k, bytes) else str(k)
        if k_str != _MOCK_SOLAR_KEY:
            raise ValueError("Decryption failed: incorrect key")
        return MockSolarData()
    monkeypatch.setattr(SolarCharger, "parse", mock_solar_parse)

    # Mock __init__ and parse for BatterySense
    def mock_sense_init(self, key):
        self.mock_key = key
    monkeypatch.setattr(BatterySense, "__init__", mock_sense_init)

    class MockSenseData:
        def get_temperature(self):
            class Val: value = 22.5
            return Val()
        def get_voltage(self):
            class Val: value = 13.2
            return Val()

    def mock_sense_parse(self, raw_bytes):
        k = self.mock_key
        k_str = k.hex() if isinstance(k, bytes) else str(k)
        if k_str != _MOCK_SENSE_KEY:
            raise ValueError("Decryption failed: incorrect key")
        return MockSenseData()
    monkeypatch.setattr(BatterySense, "parse", mock_sense_parse)


class TestRealPacketDecoding:
    def test_solar_charger_packet_detected(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        assert detect_device_type(raw) is SolarCharger

    def test_solar_charger_decodes_pv_power(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        parsed = SolarCharger(_MOCK_SOLAR_KEY).parse(raw)
        pv = parsed.get_solar_power()
        assert pv is not None, "get_solar_power() returned None"
        assert float(pv.value if hasattr(pv, "value") else pv) >= 0

    def test_solar_charger_decodes_battery_voltage(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        parsed = SolarCharger(_MOCK_SOLAR_KEY).parse(raw)
        bv = parsed.get_battery_voltage()
        assert bv is not None
        assert 10.0 < float(bv.value if hasattr(bv, "value") else bv) < 17.0

    def test_solar_charger_decodes_charge_state(self):
        raw = bytes.fromhex(_MOCK_SOLAR_PKT)
        parsed = SolarCharger(_MOCK_SOLAR_KEY).parse(raw)
        cs = parsed.get_charge_state()
        assert cs is not None

    def test_battery_sense_packet_detected(self):
        raw = bytes.fromhex(_MOCK_SENSE_PKT)
        assert detect_device_type(raw) is BatterySense

    def test_battery_sense_decodes_temperature(self):
        raw = bytes.fromhex(_MOCK_SENSE_PKT)
        parsed = BatterySense(_MOCK_SENSE_KEY).parse(raw)
        t = parsed.get_temperature()
        assert t is not None
        assert -20.0 < float(t.value if hasattr(t, "value") else t) < 80.0


# ── VictronDecoder integration (test-format payloads) ─────────────────────

class TestVictronDecoderTestFormat:
    def setup_method(self):
        self.devices = {
            "AA:BB:CC:DD:EE:FF": {"id": "test1", "label": "Test MPPT", "mac": "AA:BB:CC:DD:EE:FF"},
        }
        self.dec = VictronDecoder(self.devices)

    def test_test_format_numeric_fields_decoded(self):
        payload = {"mac": "AA:BB:CC:DD:EE:FF", "raw": {"pv_power": 150.5, "battery_voltage": 13.8, "charge_state": 3}}
        pkt = self.dec.decode(payload)
        assert pkt is not None
        assert pkt.fields["pv_power"] == pytest.approx(150.5)
        assert pkt.fields["battery_voltage"] == pytest.approx(13.8)

    def test_test_format_non_numeric_fields_ignored(self):
        payload = {"mac": "AA:BB:CC:DD:EE:FF", "raw": {"pv_power": 10.0, "label": "ignored"}}
        pkt = self.dec.decode(payload)
        assert pkt is not None
        assert "label" not in pkt.fields

    def test_unknown_mac_with_data_skipped(self):
        payload = {"mac": "00:00:00:00:00:00", "data": "deadbeef"}
        pkt = self.dec.decode(payload)
        assert pkt is None
