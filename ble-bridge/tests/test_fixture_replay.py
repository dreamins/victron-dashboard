"""
Integration test: fixture replay writes to InfluxDB with correct site/device tags.

Runs inside the ble-bridge container in the test stack (has InfluxDB access).
Environment variables INFLUX_URL, INFLUX_TOKEN, INFLUX_BUCKET must be set.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ble_bridge import load_device_map, run_fixture_mode, InfluxWriter

INFLUX_URL    = os.environ.get("INFLUX_URL", "")
INFLUX_TOKEN  = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG    = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "")

SITES_FILE    = "/app/sites.json"
FIXTURE_FILE  = "/app/fixtures/ble_packets.jsonl"

pytestmark = pytest.mark.skipif(
    not INFLUX_URL or not INFLUX_TOKEN or not INFLUX_BUCKET,
    reason="InfluxDB env vars not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def write_fixture_data():
    """Run fixture replay once for the module; return point count."""
    device_map = load_device_map(SITES_FILE)
    writer     = InfluxWriter()
    count      = run_fixture_mode(FIXTURE_FILE, device_map, writer)
    time.sleep(1)  # let InfluxDB flush
    yield count


def _query_count(site: str) -> int:
    from influxdb_client import InfluxDBClient
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "solar" and r.site == "{site}")
  |> count()
  |> sum()
"""
    tables = client.query_api().query(q)
    return sum(r.get_value() for t in tables for r in t.records)


def test_fixture_writes_points(write_fixture_data):
    assert write_fixture_data == 4, f"Expected 4 points written, got {write_fixture_data}"


def test_garage_site_tag_present(write_fixture_data):
    count = _query_count("garage")
    assert count > 0, "No points with site=garage found in InfluxDB"


def test_both_devices_written(write_fixture_data):
    from influxdb_client import InfluxDBClient
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "solar" and r.site == "garage")
  |> keep(columns: ["device"])
  |> distinct(column: "device")
"""
    tables = client.query_api().query(q)
    devices = {r.get_value() for t in tables for r in t.records}
    assert "garage_mppt1" in devices
    assert "garage_mppt2" in devices


def test_pv_power_field_written(write_fixture_data):
    from influxdb_client import InfluxDBClient
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "solar" and r.site == "garage" and r._field == "pv_power")
  |> last()
"""
    tables = client.query_api().query(q)
    values = [r.get_value() for t in tables for r in t.records]
    assert len(values) > 0, "No pv_power field found"
    assert all(v >= 0 for v in values)
