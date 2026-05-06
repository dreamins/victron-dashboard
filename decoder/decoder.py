#!/usr/bin/env python3
"""BLE decoder: subscribes to victron/raw MQTT, decodes Victron advertisements, writes to InfluxDB."""
import collections
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

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

# Exponential backoff delays between InfluxDB write attempts (seconds)
RETRY_DELAYS = [1, 2, 4, 8, 16]

# (field_name, getter_method_name) pairs tried in order; first non-None wins per field
FIELD_GETTERS = [
    ("pv_power",        "get_pv_power"),
    ("pv_voltage",      "get_pv_voltage"),
    ("battery_voltage", "get_battery_voltage"),
    ("battery_current", "get_battery_current"),
    ("yield_today",     "get_yield_today"),
    ("yield_total",     "get_yield_total"),
    ("load_current",    "get_load_current"),
    ("load_power",      "get_load_power"),
    ("load_state",      "get_load_state"),
    ("charge_state",    "get_charge_state"),
    ("charger_error",   "get_charger_error"),
    ("temperature",     "get_temperature"),
    ("alarm",           "get_alarm"),
]


def load_devices(path: str) -> dict:
    """Return {MAC_UPPER: device_dict} from devices.json."""
    with open(path) as f:
        cfg = json.load(f)
    return {d["mac"].upper(): d for d in cfg.get("devices", [])}


def extract_fields(parsed) -> dict:
    """Extract all available fields from a victron-ble parsed device data object."""
    fields = {}
    for field_name, method_name in FIELD_GETTERS:
        if field_name in fields:
            continue
        getter = getattr(parsed, method_name, None)
        if getter is None:
            continue
        try:
            val = getter()
            if val is None:
                continue
            # Enums → int value; bools → int; floats/ints as-is
            if isinstance(val, bool):
                fields[field_name] = int(val)
            elif hasattr(val, "value"):
                fields[field_name] = float(val.value)
            else:
                fields[field_name] = float(val)
        except Exception:
            pass
    return fields


class Decoder:
    def __init__(self):
        self.devices = load_devices(DEVICES_FILE)
        log.info("Loaded %d device(s) from %s", len(self.devices), DEVICES_FILE)

        # Thread-safe write buffer: deque auto-evicts oldest when maxlen is reached
        self._buffer: collections.deque = collections.deque(maxlen=BUFFER_SIZE)
        self._cv = threading.Condition()

        self.influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        self.write_api = self.influx.write_api(write_options=SYNCHRONOUS)

        writer = threading.Thread(target=self._writer_loop, daemon=True)
        writer.start()

        self.mqtt_client = mqtt.Client()
        self.mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed rc=%d", rc)
            return
        log.info("MQTT connected to %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe("victron/raw")
        client.subscribe("victron/bridge/status")

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic == "victron/bridge/status":
                log.info("bridge: %s", msg.payload.decode())
                return
            payload = json.loads(msg.payload)
            point = self._decode(payload)
            if point is not None:
                with self._cv:
                    self._buffer.append(point)
                    self._cv.notify()
        except Exception as e:
            log.error("message handling error: %s", e)

    def _decode(self, payload: dict):
        mac = payload.get("mac", "").upper()
        ts  = datetime.now(timezone.utc)

        # Pre-decoded test mode: {"mac": "...", "raw": {field: value, ...}}
        if "raw" in payload:
            device = self.devices.get(mac, {})
            device_id = device.get("id", mac.replace(":", "").lower())
            label     = device.get("label", mac)
            fields = {
                k: float(v)
                for k, v in payload["raw"].items()
                if isinstance(v, (int, float))
            }
            if not fields:
                return None
            log.info("[%s] pre-decoded: %s", device_id,
                     " ".join(f"{k}={v}" for k, v in fields.items()))
            return self._make_point(device_id, label, fields, ts)

        # Encrypted production mode: {"mac": "...", "data": "hexstring"}
        if "data" not in payload:
            log.warning("ignoring message without 'data' or 'raw' key")
            return None

        device = self.devices.get(mac)
        if device is None:
            log.info("no key configured for %s — add to devices.json for decryption", mac)
            return None
        if not device.get("key"):
            log.info("no key configured for %s — add encryption key to devices.json", mac)
            return None

        try:
            from victron_ble.devices import detect_device_type

            raw_bytes = bytes.fromhex(payload["data"])
            device_class = detect_device_type(raw_bytes)
            if device_class is None:
                log.warning("[%s] unrecognised advertisement record type", device["id"])
                return None

            parsed = device_class(device["key"]).parse(raw_bytes)
            fields = extract_fields(parsed)
            if not fields:
                log.warning("[%s] no fields in decoded payload", device["id"])
                return None

            log.info("[%s] decoded and timestamped server-side: %s",
                     device["id"],
                     " ".join(f"{k}={v}" for k, v in fields.items()))
            return self._make_point(device["id"], device["label"], fields, ts)

        except Exception as e:
            log.error("[%s] decode error: %s", device.get("id", mac), e)
            return None

    @staticmethod
    def _make_point(device_id: str, label: str, fields: dict, ts: datetime) -> Point:
        p = Point("solar").tag("device", device_id).tag("label", label).time(ts)
        for k, v in fields.items():
            p = p.field(k, v)
        return p

    def _writer_loop(self):
        """Drain the buffer, retrying failed writes with exponential backoff."""
        while True:
            with self._cv:
                while not self._buffer:
                    self._cv.wait(timeout=5.0)
                if not self._buffer:
                    continue
                point = self._buffer.popleft()
            self._write_with_retry(point)

    def _write_with_retry(self, point: Point):
        for i, delay in enumerate(RETRY_DELAYS):
            try:
                self.write_api.write(bucket=INFLUX_BUCKET, record=point)
                return
            except Exception as e:
                log.warning("influx write attempt %d/%d failed: %s; retry in %ds",
                            i + 1, len(RETRY_DELAYS) + 1, e, delay)
                time.sleep(delay)
        try:
            self.write_api.write(bucket=INFLUX_BUCKET, record=point)
        except Exception as e:
            log.error("influx write failed after all retries, dropping point: %s", e)

    def run(self):
        log.info("Starting decoder — connecting to MQTT %s:%d", MQTT_BROKER, MQTT_PORT)
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        self.mqtt_client.loop_forever()


if __name__ == "__main__":
    Decoder().run()
