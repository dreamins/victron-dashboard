"""InfluxDB writer and point-builder helpers for ble-bridge."""
import logging
import os
from datetime import datetime
from typing import Dict, Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

log = logging.getLogger(__name__)


def _make_point(device_id: str, label: str, site_id: str,
                ts: datetime, fields: Dict[str, float]) -> Optional[Point]:
    if not fields:
        return None
    p = (Point("solar")
         .tag("device", device_id)
         .tag("label", label)
         .tag("site", site_id)
         .time(ts))
    for k, v in fields.items():
        p = p.field(k, v)
    return p


def _make_battery_point(device_id: str, label: str, site_id: str,
                        ts: datetime, fields: Dict[str, float]) -> Optional[Point]:
    if not fields:
        return None
    p = (Point("battery")
         .tag("device", device_id)
         .tag("label", label)
         .tag("site", site_id)
         .time(ts))
    for k, v in fields.items():
        p = p.field(k, v)
    return p


class InfluxWriter:
    def __init__(self, url: str = "", token: str = "",
                 org: str = "home", bucket: str = ""):
        url    = url    or os.environ.get("INFLUX_URL", "")
        token  = token  or os.environ.get("INFLUX_TOKEN", "")
        org    = org    or os.environ.get("INFLUX_ORG", "home")
        bucket = bucket or os.environ.get("INFLUX_BUCKET", "")
        self._bucket    = bucket
        self._client    = InfluxDBClient(url=url, token=token, org=org)
        self._write_api = self._client.write_api(write_options=SYNCHRONOUS)

    def write(self, point: Point) -> None:
        try:
            self._write_api.write(bucket=self._bucket, record=point)
        except Exception as e:
            log.error("InfluxDB write failed: %s", e)
