"""Device-map loading and MAC/UUID persistence for ble-bridge."""
import json
import logging
import pathlib
from typing import Any, Dict

log = logging.getLogger(__name__)

BMS_TYPES        = {"litime_bms"}
DEFAULT_WRITE_S  = 60


def load_device_map(sites_file: str) -> Dict[str, Dict[str, Any]]:
    """Load sites.json → {MAC_UPPER: device_info}.

    BLE-bridge sites only (bridge != esp32/mqtt).
    BMS devices without a MAC are included using a synthetic key.
    """
    path = pathlib.Path(sites_file)
    if not path.exists():
        log.warning("Sites file not found: %s", sites_file)
        return {}
    with open(path) as f:
        data = json.load(f)

    result: Dict[str, Dict[str, Any]] = {}
    for site in data.get("sites", []):
        site_id = site["id"]
        bridge  = site.get("bridge", "ble")
        if bridge in ("esp32", "mqtt"):
            log.debug("Skipping site %s (bridge=%s)", site_id, bridge)
            continue
        for dev in site.get("devices", []):
            mac      = dev.get("mac", "").upper()
            dev_type = dev.get("type", "unknown")
            if not mac and dev_type not in BMS_TYPES:
                log.warning("Skipping %s/%s: no MAC and not a BMS type",
                            site_id, dev.get("id", "?"))
                continue
            key = mac if mac else f"_{site_id}_{dev['id']}"
            result[key] = {
                "site_id":        site_id,
                "device_id":      dev["id"],
                "label":          dev.get("label", dev["id"]),
                "key":            dev.get("key", ""),
                "type":           dev_type,
                "mac":            mac,
                "write_interval": int(dev.get("write_interval_s", DEFAULT_WRITE_S)),
                "capacity_ah":    dev.get("capacity_ah"),
                "write_uuid":     dev.get("write_uuid"),
                "notify_uuid":    dev.get("notify_uuid"),
            }

    log.info("Loaded %d devices from %s", len(result), sites_file)
    return result


def persist_mac(sites_file: str, site_id: str, device_id: str, mac: str) -> None:
    """Write a discovered BMS MAC back to sites.json."""
    path = pathlib.Path(sites_file)
    try:
        with open(path) as f:
            data = json.load(f)
        for site in data.get("sites", []):
            if site["id"] != site_id:
                continue
            for dev in site.get("devices", []):
                if dev["id"] == device_id:
                    dev["mac"] = mac
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("[%s/%s] discovered MAC %s saved to %s",
                 site_id, device_id, mac, sites_file)
    except Exception as e:
        log.error("[%s/%s] failed to save discovered MAC: %s", site_id, device_id, e)


def persist_uuids(sites_file: str, site_id: str, device_id: str,
                  write_uuid: str, notify_uuid: str) -> None:
    """Write discovered BLE UUIDs back to sites.json."""
    path = pathlib.Path(sites_file)
    try:
        with open(path) as f:
            data = json.load(f)
        for site in data.get("sites", []):
            if site["id"] != site_id:
                continue
            for dev in site.get("devices", []):
                if dev["id"] == device_id:
                    dev["write_uuid"]  = write_uuid
                    dev["notify_uuid"] = notify_uuid
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("[%s/%s] UUIDs persisted to sites.json", site_id, device_id)
    except Exception as e:
        log.error("[%s/%s] failed to persist UUIDs: %s", site_id, device_id, e)
