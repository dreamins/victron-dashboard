"""Site configuration: loading, device classification, and safe file I/O.

All public functions take an explicit ``sites_file`` path so they can be
called from tests with a temporary file — no module-level globals here.
"""
import json
import os
import pathlib
import shutil
import tempfile
from typing import Any, Dict, List, Optional

# ── Field metadata ────────────────────────────────────────────────────────────

VALID_FIELDS: frozenset = frozenset({
    "pv_power", "pv_voltage", "battery_voltage", "charge_current",
    "load_current", "load_power", "load_state", "charge_state",
    "yield_today", "yield_total", "error_code", "temperature",
    "battery_current", "charger_error",
    "soc", "soh", "cycles", "cell_min", "cell_max", "cell_avg",
    "temperature_mosfet",
})

FIELD_UNITS: Dict[str, str] = {
    "pv_power": "W",      "load_power": "W",
    "pv_voltage": "V",    "battery_voltage": "V",
    "charge_current": "A","load_current": "A", "battery_current": "A",
    "yield_today": "Wh",  "yield_total": "kWh",
    "temperature": "°C",  "temperature_mosfet": "°C",
    "soc": "%",
}

YIELD_FIELDS: frozenset = frozenset({"yield_today", "yield_total"})

# ── Device type classification ────────────────────────────────────────────────

# Types that write to the 'battery' InfluxDB measurement (not 'solar').
BMS_MEASUREMENT_TYPES: frozenset = frozenset({"litime_bms"})

# Types that provide temperature readings.
TEMP_PROVIDER_TYPES: frozenset = frozenset({"victron_battery_sense", "litime_bms"})


# ── Public API ────────────────────────────────────────────────────────────────

def load_sites(sites_file: str) -> List[Dict[str, Any]]:
    """Return sanitised site list (MAC and BLE keys stripped)."""
    if not sites_file or not pathlib.Path(sites_file).exists():
        return []
    with open(sites_file) as f:
        data = json.load(f)
    result = []
    for s in data.get("sites", []):
        temp_providers: List[Dict[str, str]] = []
        for d in s.get("devices", []):
            # Explicit flag wins; fall back to type-based inference.
            is_tp = d.get("temperature_provider", d.get("type") in TEMP_PROVIDER_TYPES)
            if is_tp:
                temp_providers.append({"id": d["id"], "label": d.get("label", d["id"])})
        result.append({
            "id":              s["id"],
            "label":           s.get("label", s["id"]),
            "tz_offset_hours": s.get("tz_offset_hours", 0),
            "bridge":          s.get("bridge", "esp32"),
            "ui":              s.get("ui", {}),
            "device_types":    list({d.get("type", "unknown") for d in s.get("devices", [])}),
            "temp_providers":  temp_providers,
        })
    return result


def load_sites_raw(sites_file: str) -> dict:
    """Return full sites.json contents including MAC and BLE keys."""
    if not sites_file or not pathlib.Path(sites_file).exists():
        return {"sites": []}
    with open(sites_file) as f:
        return json.load(f)


def device_measurement(sites_file: str,
                       site_id: Optional[str],
                       device_id: str) -> str:
    """Return 'battery' for BMS devices, 'solar' for everything else."""
    data = load_sites_raw(sites_file)
    for s in data.get("sites", []):
        if site_id and s["id"] != site_id:
            continue
        for dev in s.get("devices", []):
            if dev["id"] == device_id:
                return "battery" if dev.get("type") in BMS_MEASUREMENT_TYPES else "solar"
    return "solar"


def write_sites_atomic(sites_file: str, data: dict) -> None:
    """Safely overwrite sites.json.

    os.replace() fails on Docker bind mounts (EBUSY on the mount point).
    shutil.copy2 writes to the existing inode, which works.
    """
    path = pathlib.Path(sites_file)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        shutil.copy2(tmp, str(path))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
