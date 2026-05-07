#!/usr/bin/env python3
"""BLE decoder: subscribes to victron/raw MQTT, decodes Victron advertisements, writes to InfluxDB."""
import collections
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any

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
DEVICES_FILE  = os.environ.get("DEVICES_FILE", "/app/devices.json")
BUFFER_SIZE   = int(os.environ.get("BUFFER_SIZE", "500"))
RETRY_DELAYS  = [1, 2, 4, 8, 16]


FIELD_GETTERS = [
    # victron-ble SolarChargerData methods (verified against library source)
    ("pv_power",        "get_solar_power"),              # was get_pv_power — wrong name
    ("battery_voltage", "get_battery_voltage"),
    ("charge_current",  "get_battery_charging_current"), # was get_battery_current — wrong name
    ("yield_today",     "get_yield_today"),
    ("load_power",      "get_external_device_load"),     # was get_load_power — wrong name
    ("charge_state",    "get_charge_state"),
    ("charger_error",   "get_charger_error"),
    # BatterySenseData methods (Smart Battery Sense uses get_voltage, not get_battery_voltage)
    ("battery_voltage", "get_voltage"),
    ("temperature",     "get_temperature"),
    # Methods that may exist on other device classes; silently skipped if absent
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
    timestamp: datetime
    fields: Dict[str, float]

    def to_point(self) -> Point:
        p = Point("solar").tag("device", self.device_id).tag("label", self.label).time(self.timestamp)
        for k, v in self.fields.items():
            p = p.field(k, v)
        return p

class VictronDecoder:
    def __init__(self, devices_config: Dict[str, Dict[str, Any]]):
        self.devices = devices_config

    def decode(self, payload: Dict[str, Any]) -> Optional[VictronPacket]:
        mac = payload.get("mac", "").upper()
        ts = datetime.now(timezone.utc)
        
        # Handle pre-decoded test format
        if "raw" in payload:
            return self._handle_test_packet(mac, payload["raw"], ts)
        
        # Handle encrypted production format
        if "data" not in payload:
            return None
            
        device = self.devices.get(mac)
        if not device or not device.get("key"):
            log.debug("No key for %s, skipping", mac)
            return None

        return self._handle_production_packet(device, payload["data"], ts)

    def _handle_test_packet(self, mac: str, raw_fields: Dict[str, Any], ts: datetime) -> Optional[VictronPacket]:
        device = self.devices.get(mac, {})
        dev_id = device.get("id", mac.replace(":", "").lower())
        label = device.get("label", mac)
        fields = {k: float(v) for k, v in raw_fields.items() if isinstance(v, (int, float))}
        return VictronPacket(dev_id, label, ts, fields) if fields else None

    def _handle_production_packet(self, device: Dict[str, Any], hex_data: str, ts: datetime) -> Optional[VictronPacket]:
        try:
            from victron_ble.devices import detect_device_type
            raw_bytes = bytes.fromhex(hex_data)
            device_class = detect_device_type(raw_bytes)
            if not device_class:
                return None

            parsed = device_class(device["key"]).parse(raw_bytes)
            fields = self._extract_fields(parsed)
            if not fields:
                return None

            return VictronPacket(device["id"], device["label"], ts, fields)
        except Exception as e:
            log.error("[%s] Decode error: %s", device.get("id"), e)
            return None

    def _extract_fields(self, parsed) -> Dict[str, float]:
        fields = {}
        for field_name, method_name in FIELD_GETTERS:
            getter = getattr(parsed, method_name, None)
            if not getter: continue
            try:
                val = getter()
                if val is None: continue
                fields[field_name] = float(val.value) if hasattr(val, "value") else float(val)
            except Exception:
                continue
        return fields

class ResilientInfluxClient:
    def __init__(self):
        self._buffer: collections.deque = collections.deque(maxlen=BUFFER_SIZE)
        self._cv = threading.Condition()
        self.client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=3_000)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        threading.Thread(target=self._writer_loop, daemon=True).start()

    def write(self, packet: VictronPacket):
        log.info("[%s] Decoded: %s", packet.device_id,
                 " ".join(f"{k}={v}" for k, v in packet.fields.items()))
        with self._cv:
            self._buffer.append(packet)
            self._cv.notify()

    def _writer_loop(self):
        while True:
            with self._cv:
                while not self._buffer:
                    self._cv.wait(timeout=5.0)
                if not self._buffer:
                    continue
                packet = self._buffer.popleft()
            self._write_with_retry(packet)

    def _write_with_retry(self, packet: VictronPacket):
        for delay in RETRY_DELAYS:
            try:
                self.write_api.write(bucket=INFLUX_BUCKET, record=packet.to_point())
                return
            except Exception as e:
                log.warning("influx write attempt failed: %s; retry in %ds", e, delay)
                time.sleep(delay)
        try:
            self.write_api.write(bucket=INFLUX_BUCKET, record=packet.to_point())
        except Exception as e:
            log.error("influx write failed after all retries, dropping point: %s", e)

class DecoderService:
    def __init__(self):
        devices = self._load_devices(DEVICES_FILE)
        self.decoder = VictronDecoder(devices)
        self.influx = ResilientInfluxClient()
        
        self.mqtt = mqtt.Client()
        self.mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    def _load_devices(self, path: str) -> Dict[str, Any]:
        with open(path) as f:
            return {d["mac"].upper(): d for d in json.load(f).get("devices", [])}

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("Connected to MQTT %s", MQTT_BROKER)
            client.subscribe("victron/raw")
            client.subscribe("victron/bridge/status")
        else:
            log.error("MQTT Connection failed: %s", rc)

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic == "victron/bridge/status":
                log.info("bridge: %s", msg.payload.decode())
                return
            payload = json.loads(msg.payload)
            packet = self.decoder.decode(payload)
            if packet:
                self.influx.write(packet)
        except Exception as e:
            log.error("Message processing error: %s", e)

    def start(self):
        self.mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
        self.mqtt.loop_forever()

if __name__ == "__main__":
    DecoderService().start()
