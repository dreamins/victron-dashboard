#!/usr/bin/env python3
"""BLE decoder: subscribes to Victron MQTT topics, decodes advertisements, writes to InfluxDB."""
import collections
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# --- Configuration ---
MQTT_BROKER   = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT     = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "decoder")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
INFLUX_URL    = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG    = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "victron")
BUFFER_SIZE   = int(os.environ.get("BUFFER_SIZE", "500"))
RETRY_DELAYS  = [1, 2, 4, 8, 16]

# SITES_FILE takes priority; DEVICES_FILE is the legacy fallback.
SITES_FILE   = os.environ.get("SITES_FILE", "")
DEVICES_FILE = os.environ.get("DEVICES_FILE", "/app/devices.json")
DEFAULT_SITE = os.environ.get("DEFAULT_SITE", "home")

FIELD_GETTERS = [
    ("pv_power",        "get_solar_power"),
    ("battery_voltage", "get_battery_voltage"),
    ("charge_current",  "get_battery_charging_current"),
    ("yield_today",     "get_yield_today"),
    ("load_power",      "get_external_device_load"),
    ("charge_state",    "get_charge_state"),
    ("charger_error",   "get_charger_error"),
    ("battery_voltage", "get_voltage"),           # BatterySenseData
    ("temperature",     "get_temperature"),
    ("pv_voltage",      "get_pv_voltage"),
    ("yield_total",     "get_yield_total"),
    ("load_current",    "get_load_current"),
    ("load_state",      "get_load_state"),
    ("alarm",           "get_alarm"),
]


@dataclass(frozen=True)
class VictronPacket:
    device_id: str
    label: str
    site_id: str
    timestamp: datetime
    fields: Dict[str, float]

    def to_point(self) -> Point:
        p = (Point("solar")
             .tag("device", self.device_id)
             .tag("label", self.label)
             .tag("site", self.site_id)
             .time(self.timestamp))
        for k, v in self.fields.items():
            p = p.field(k, v)
        return p


def _load_config(sites_file: str, devices_file: str) -> Dict[str, Any]:
    """Load device config. Returns MAC→device dict with site_id injected.

    Supports two file formats:
      - sites.json: {"sites": [{"id": "home", "devices": [...]}]}
      - devices.json (legacy): {"devices": [...]}  → all assigned DEFAULT_SITE
    """
    path = sites_file if sites_file and os.path.exists(sites_file) else devices_file
    with open(path) as f:
        data = json.load(f)

    result: Dict[str, Any] = {}
    if "sites" in data:
        for site in data["sites"]:
            site_id = site["id"]
            for d in site.get("devices", []):
                result[d["mac"].upper()] = {**d, "site_id": site_id}
        log.info("Loaded sites config from %s: %d sites, %d devices",
                 path, len(data["sites"]), len(result))
    else:
        # Legacy devices.json — assign everything to DEFAULT_SITE
        for d in data.get("devices", []):
            result[d["mac"].upper()] = {**d, "site_id": DEFAULT_SITE}
        log.info("Loaded legacy devices config from %s: %d devices (site=%s)",
                 path, len(result), DEFAULT_SITE)
    return result


class VictronDecoder:
    def __init__(self, devices_config: Dict[str, Any]):
        self.devices = devices_config

    def decode(self, payload: Dict[str, Any]) -> Optional[VictronPacket]:
        mac = payload.get("mac", "").upper()
        ts = datetime.now(timezone.utc)

        if "raw" in payload:
            return self._handle_test_packet(mac, payload["raw"], ts)
        if "data" not in payload:
            return None

        device = self.devices.get(mac)
        if not device or not device.get("key"):
            log.debug("No key for %s, skipping", mac)
            return None
        return self._handle_production_packet(device, payload["data"], ts)

    def _handle_test_packet(self, mac: str, raw_fields: Dict[str, Any], ts: datetime) -> Optional[VictronPacket]:
        device = self.devices.get(mac, {})
        dev_id  = device.get("id", mac.replace(":", "").lower())
        label   = device.get("label", mac)
        site_id = device.get("site_id", DEFAULT_SITE)
        fields  = {k: float(v) for k, v in raw_fields.items() if isinstance(v, (int, float))}
        if not fields:
            return None
        return VictronPacket(dev_id, label, site_id, ts, self._sanitize_fields(fields))

    def _handle_production_packet(self, device: Dict[str, Any], hex_data: str, ts: datetime) -> Optional[VictronPacket]:
        try:
            from victron_ble.devices import detect_device_type
            raw_bytes    = bytes.fromhex(hex_data)
            device_class = detect_device_type(raw_bytes)
            if not device_class:
                return None
            parsed = device_class(device["key"]).parse(raw_bytes)
            fields = self._extract_fields(parsed)
            if not fields:
                return None
            return VictronPacket(device["id"], device["label"], device["site_id"], ts, self._sanitize_fields(fields))
        except Exception as e:
            log.error("[%s] Decode error: %s", device.get("id"), e)
            return None

    def _sanitize_fields(self, fields: Dict[str, float]) -> Dict[str, float]:
        # Fix phantom pv_power spikes when device is sleeping (charge_state=0)
        if fields.get("charge_state", -1) == 0 and "pv_power" in fields:
            fields["pv_power"] = 0.0
        return fields

    def _extract_fields(self, parsed) -> Dict[str, float]:
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


class ResilientInfluxClient:
    def __init__(self):
        self._buffer: collections.deque = collections.deque(maxlen=BUFFER_SIZE)
        self._cv = threading.Condition()
        self.client   = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=3_000)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        threading.Thread(target=self._writer_loop, daemon=True).start()

    def write(self, packet: VictronPacket):
        log.debug("[%s/%s] %s", packet.site_id, packet.device_id,
                  " ".join(f"{k}={v}" for k, v in packet.fields.items()))
        with self._cv:
            self._buffer.append(packet)
            # Removed notify() to allow strictly timed batching (every 5s)

    def _writer_loop(self):
        while True:
            with self._cv:
                while not self._buffer:
                    self._cv.wait(timeout=5.0)
                if not self._buffer:
                    continue
                # Drain the entire buffer into a batch
                batch = []
                while self._buffer:
                    batch.append(self._buffer.popleft())
            
            if batch:
                self._write_batch_with_retry(batch)

    def _write_batch_with_retry(self, batch: List[VictronPacket]):
        records = [p.to_point() for p in batch]
        for delay in RETRY_DELAYS:
            try:
                self.write_api.write(bucket=INFLUX_BUCKET, record=records)
                return
            except Exception as e:
                log.warning("influx batch write failed (%d points): %s; retry in %ds", 
                            len(batch), e, delay)
                time.sleep(delay)
        try:
            self.write_api.write(bucket=INFLUX_BUCKET, record=records)
        except Exception as e:
            log.error("influx batch write failed after all retries, dropping %d points: %s", 
                      len(batch), e)


class DecoderService:
    def __init__(self):
        devices      = _load_config(SITES_FILE, DEVICES_FILE)
        self.decoder = VictronDecoder(devices)
        self.influx  = ResilientInfluxClient()

        self.mqtt_client = mqtt.Client()
        self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connection failed: rc=%s", rc)
            return
        log.info("Connected to MQTT %s", MQTT_BROKER)
        # Multi-site topic (new ESP32 / ble-bridge): victron/{site_id}/raw
        client.subscribe("victron/+/raw")
        # Legacy single-site topic (ESP32 not yet reflashed): victron/raw
        client.subscribe("victron/raw")
        # Bridge status — both old and new topic formats
        client.subscribe("victron/bridge/status")
        client.subscribe("victron/+/bridge/status")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            if topic == "victron/bridge/status" or topic.endswith("/bridge/status"):
                log.info("bridge [%s]: %s", topic, msg.payload.decode())
                return
            payload = json.loads(msg.payload)
            packet  = self.decoder.decode(payload)
            if packet:
                self.influx.write(packet)
        except Exception as e:
            log.error("Message processing error on %s: %s", msg.topic, e)

    def start(self):
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        self.mqtt_client.loop_forever()


if __name__ == "__main__":
    DecoderService().start()
