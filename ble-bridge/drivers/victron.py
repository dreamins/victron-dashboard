"""Victron BLE advertisement decoding for the Linux BLE bridge."""
from typing import Optional, Dict

# Same getter list as decoder/decoder.py — kept in sync.
FIELD_GETTERS = [
    ("pv_power",        "get_solar_power"),
    ("battery_voltage", "get_battery_voltage"),
    ("charge_current",  "get_battery_charging_current"),
    ("yield_today",     "get_yield_today"),
    ("load_power",      "get_external_device_load"),
    ("charge_state",    "get_charge_state"),
    ("charger_error",   "get_charger_error"),
    ("battery_voltage", "get_voltage"),         # BatterySense fallback
    ("battery_voltage", "get_output_voltage1"), # AcCharger fallback
    ("charge_current",  "get_output_current1"), # AcCharger fallback
    ("temperature",     "get_temperature"),
    ("pv_voltage",      "get_pv_voltage"),
    ("yield_total",     "get_yield_total"),
    ("load_current",    "get_load_current"),
    ("load_state",      "get_load_state"),
]


def decode_advertisement(raw_bytes: bytes, key: str) -> Optional[Dict[str, float]]:
    """Decode raw Victron BLE manufacturer-data bytes with the device key.

    raw_bytes: manufacturer_data payload AFTER the 2-byte company identifier
               (bleak strips it automatically, matching what the ESP32 publishes).
    key:       hex string from sites.json.
    Returns:   field dict, or None if unsupported/corrupt.
    """
    try:
        from victron_ble.devices import detect_device_type
        device_class = detect_device_type(raw_bytes)
        if not device_class:
            return None
        parsed = device_class(key).parse(raw_bytes)
        return _extract_fields(parsed)
    except Exception:
        return None


def _extract_fields(parsed) -> Dict[str, float]:
    fields: Dict[str, float] = {}
    for field_name, method_name in FIELD_GETTERS:
        getter = getattr(parsed, method_name, None)
        if not getter:
            continue
        try:
            val = getter()
            if val is None:
                continue
            fields[field_name] = float(val.value) if hasattr(val, "value") else float(val)
        except Exception:
            continue
    return fields
