"""Unit tests for api/config.py — pure functions, no InfluxDB or BLE required."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from config import (
    BMS_MEASUREMENT_TYPES, TEMP_PROVIDER_TYPES, VALID_FIELDS, YIELD_FIELDS,
    device_measurement, load_sites, load_sites_raw, write_sites_atomic,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sites_file(tmp_path):
    data = {
        "sites": [
            {
                "id": "home",
                "label": "Home Solar",
                "bridge": "esp32",
                "devices": [
                    {"id": "mppt1",  "label": "MPPT 1",         "type": "victron_mppt",
                     "mac": "AA:BB:CC:DD:EE:FF", "key": "secret1"},
                    {"id": "bsense", "label": "Battery Sense",  "type": "victron_battery_sense",
                     "mac": "11:22:33:44:55:66"},
                ],
            },
            {
                "id": "garage",
                "label": "Garage",
                "bridge": "ble",
                "devices": [
                    {"id": "garage_mppt", "label": "Garage MPPT", "type": "victron_mppt",
                     "mac": "CC:DD:EE:FF:00:11", "key": "secret2"},
                    {"id": "garage_bms",  "label": "LiTime BMS",  "type": "litime_bms",
                     "mac": "77:88:99:AA:BB:CC"},
                ],
            },
        ]
    }
    p = tmp_path / "sites.json"
    p.write_text(json.dumps(data))
    return str(p)


# ── device_measurement ────────────────────────────────────────────────────────

def test_device_measurement_litime_returns_battery(sites_file):
    assert device_measurement(sites_file, "garage", "garage_bms") == "battery"


def test_device_measurement_mppt_returns_solar(sites_file):
    assert device_measurement(sites_file, "garage", "garage_mppt") == "solar"


def test_device_measurement_unknown_device_returns_solar(sites_file):
    assert device_measurement(sites_file, "garage", "nonexistent") == "solar"


def test_device_measurement_ignores_wrong_site(sites_file):
    # garage_bms is litime_bms, but site=home means we only scan home devices.
    assert device_measurement(sites_file, "home", "garage_bms") == "solar"


def test_device_measurement_no_site_filter_matches_across_sites(sites_file):
    # site_id=None disables the site filter — finds garage_bms in any site.
    assert device_measurement(sites_file, None, "garage_bms") == "battery"


def test_device_measurement_missing_file_returns_solar():
    assert device_measurement("/nonexistent/sites.json", "s", "d") == "solar"


# ── load_sites ────────────────────────────────────────────────────────────────

def test_load_sites_strips_mac_and_key(sites_file):
    sites = load_sites(sites_file)
    for site in sites:
        assert "mac" not in site
        assert "key" not in site
        assert "devices" not in site  # raw device list removed; temp_providers kept


def test_load_sites_returns_correct_ids(sites_file):
    sites = load_sites(sites_file)
    ids = {s["id"] for s in sites}
    assert ids == {"home", "garage"}


def test_load_sites_builds_temp_providers_from_type(sites_file):
    sites = load_sites(sites_file)
    home = next(s for s in sites if s["id"] == "home")
    tp_ids = {tp["id"] for tp in home["temp_providers"]}
    # victron_battery_sense is in TEMP_PROVIDER_TYPES
    assert "bsense" in tp_ids
    # victron_mppt is not a temp provider
    assert "mppt1" not in tp_ids


def test_load_sites_bms_is_temp_provider(sites_file):
    sites = load_sites(sites_file)
    garage = next(s for s in sites if s["id"] == "garage")
    tp_ids = {tp["id"] for tp in garage["temp_providers"]}
    assert "garage_bms" in tp_ids


def test_load_sites_explicit_flag_true_overrides_non_provider_type(tmp_path):
    data = {"sites": [{"id": "s1", "devices": [
        {"id": "dev1", "type": "victron_mppt", "temperature_provider": True},
    ]}]}
    p = tmp_path / "sites.json"
    p.write_text(json.dumps(data))
    sites = load_sites(str(p))
    tp_ids = {tp["id"] for tp in sites[0]["temp_providers"]}
    assert "dev1" in tp_ids


def test_load_sites_explicit_flag_false_suppresses_provider_type(tmp_path):
    data = {"sites": [{"id": "s1", "devices": [
        {"id": "dev2", "type": "victron_battery_sense", "temperature_provider": False},
    ]}]}
    p = tmp_path / "sites.json"
    p.write_text(json.dumps(data))
    sites = load_sites(str(p))
    tp_ids = {tp["id"] for tp in sites[0]["temp_providers"]}
    assert "dev2" not in tp_ids


def test_load_sites_missing_file_returns_empty():
    assert load_sites("/nonexistent/path/sites.json") == []


def test_load_sites_empty_sites_key_returns_empty(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text('{"sites": []}')
    assert load_sites(str(p)) == []


def test_load_sites_includes_bridge_and_ui(sites_file):
    sites = load_sites(sites_file)
    home = next(s for s in sites if s["id"] == "home")
    assert home["bridge"] == "esp32"
    assert "ui" in home


def test_load_sites_device_types_collected(sites_file):
    sites = load_sites(sites_file)
    garage = next(s for s in sites if s["id"] == "garage")
    assert "litime_bms" in garage["device_types"]
    assert "victron_mppt" in garage["device_types"]


# ── load_sites_raw ────────────────────────────────────────────────────────────

def test_load_sites_raw_preserves_mac(sites_file):
    raw = load_sites_raw(sites_file)
    macs = {d["mac"] for s in raw["sites"] for d in s["devices"]}
    assert "AA:BB:CC:DD:EE:FF" in macs


def test_load_sites_raw_preserves_key(sites_file):
    raw = load_sites_raw(sites_file)
    keys = {d.get("key") for s in raw["sites"] for d in s["devices"] if "key" in d}
    assert "secret1" in keys


def test_load_sites_raw_missing_file_returns_empty_dict():
    result = load_sites_raw("/nonexistent/sites.json")
    assert result == {"sites": []}


# ── write_sites_atomic ────────────────────────────────────────────────────────

def test_write_sites_atomic_overwrites_correctly(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text('{"sites": []}')
    data = {"sites": [{"id": "x"}]}
    write_sites_atomic(str(p), data)
    assert json.loads(p.read_text()) == data


def test_write_sites_atomic_no_tmp_file_left(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text('{}')
    write_sites_atomic(str(p), {"sites": []})
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_sites_atomic_leaves_original_on_failure(tmp_path):
    p = tmp_path / "sites.json"
    original = '{"sites": []}'
    p.write_text(original)

    class _Unserializable:
        pass

    with pytest.raises(Exception):
        write_sites_atomic(str(p), {"sites": [_Unserializable()]})

    assert p.read_text() == original
    assert list(tmp_path.glob("*.tmp")) == []


# ── Field metadata ────────────────────────────────────────────────────────────

def test_valid_fields_includes_solar_fields():
    assert "pv_power" in VALID_FIELDS
    assert "battery_voltage" in VALID_FIELDS
    assert "charge_current" in VALID_FIELDS


def test_valid_fields_includes_bms_fields():
    assert "soc" in VALID_FIELDS
    assert "battery_current" in VALID_FIELDS
    assert "cell_min" in VALID_FIELDS


def test_yield_fields_use_max_not_mean():
    assert "yield_today" in YIELD_FIELDS
    assert "yield_total" in YIELD_FIELDS
    assert "pv_power" not in YIELD_FIELDS


def test_bms_measurement_types():
    assert "litime_bms" in BMS_MEASUREMENT_TYPES


def test_temp_provider_types_includes_sense_and_bms():
    assert "victron_battery_sense" in TEMP_PROVIDER_TYPES
    assert "litime_bms" in TEMP_PROVIDER_TYPES
    assert "victron_mppt" not in TEMP_PROVIDER_TYPES
