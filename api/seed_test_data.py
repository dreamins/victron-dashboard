#!/usr/bin/env python3
"""Seed victron_test bucket with synthetic solar data for Phase 4 testing.

yield_today resets at UTC midnight and increases monotonically through the day.
Seeded data ends 90s before now so bridge_online/online show False in tests.
"""

import os
import argparse
import math
from datetime import datetime, timezone, timedelta
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL    = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN  = os.environ["INFLUX_TOKEN"]
INFLUX_ORG    = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "victron_test")
SITE          = os.environ.get("SEED_SITE", "test")

DEVICES = {
    "test_mppt1": {"label": "Test-MPPT1", "peak_wh": 1500.0, "peak_w": 250.0},
    "test_mppt2": {"label": "Test-MPPT2", "peak_wh": 1000.0, "peak_w": 180.0},
    "test_battery_sense": {"label": "Test-BatterySense"},
}


def _solar_fraction(hour: float) -> float:
    """0–1 solar output for given hour of UTC day (0–24)."""
    if hour < 6.0 or hour > 18.0:
        return 0.0
    return math.sin((hour - 6.0) / 12.0 * math.pi)


def _yield_today(hour: float, peak_wh: float) -> float:
    """Cumulative Wh from UTC midnight to current hour."""
    if hour <= 6.0:
        return 0.0
    if hour >= 18.0:
        return peak_wh
    # Integral of sin((h-6)/12*pi) dh from 6 to h, normalized to [0, peak_wh]
    # Indefinite integral: -12/pi * cos((h-6)/12*pi) + C
    # From 6 to h: 12/pi * (1 - cos((h-6)/12*pi))
    # Total (6 to 18): 12/pi * 2 = 24/pi
    fraction = 12.0 / math.pi * (1.0 - math.cos((hour - 6.0) / 12.0 * math.pi))
    total = 24.0 / math.pi
    return peak_wh * fraction / total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--interval-minutes", type=int, default=1)
    args = parser.parse_args()

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    now = datetime.now(timezone.utc)
    # End 90s ago so online/bridge_online show False in tests (thresholds: 15s, 30s)
    end_time = now - timedelta(seconds=90)
    start_time = end_time - timedelta(hours=args.hours)
    step = timedelta(minutes=args.interval_minutes)

    cumulative_kwh = {"test_mppt1": 0.0, "test_mppt2": 0.0}

    t = start_time
    batch: list[Point] = []
    written = 0

    while t <= end_time:
        hour = t.hour + t.minute / 60.0
        sf = _solar_fraction(hour)
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
        day_hour = (t - midnight).total_seconds() / 3600.0

        for dev_id in ("test_mppt1", "test_mppt2"):
            info = DEVICES[dev_id]
            pv_w = sf * info["peak_w"]
            yt = _yield_today(day_hour, info["peak_wh"])
            cumulative_kwh[dev_id] += pv_w * args.interval_minutes / 60000.0  # kWh
            batt_v = 12.5 + 1.9 * sf
            charge_state = 5 if sf > 0.1 else 0

            batch.append(
                Point("solar")
                .tag("device", dev_id)
                .tag("label", info["label"])
                .tag("site", SITE)
                .field("pv_power", float(pv_w))
                .field("pv_voltage", float(17.0 + sf * 2.0))
                .field("battery_voltage", float(batt_v))
                .field("charge_current", float(pv_w / max(batt_v, 0.1)))
                .field("yield_today", float(yt))
                .field("yield_total", float(cumulative_kwh[dev_id]))
                .field("charge_state", int(charge_state))
                .field("charger_error", int(0))
                .time(t, "s")
            )

        temp = 15.0 + 5.0 * math.sin(hour / 24.0 * 2.0 * math.pi)
        batch.append(
            Point("solar")
            .tag("device", "test_battery_sense")
            .tag("label", DEVICES["test_battery_sense"]["label"])
            .tag("site", SITE)
            .field("battery_voltage", float(12.5 + 1.9 * sf))
            .field("temperature", float(temp))
            .time(t, "s")
        )

        if len(batch) >= 1000:
            try:
                write_api.write(bucket=INFLUX_BUCKET, record=batch)
                written += len(batch)
            except Exception as e:
                if "outside retention policy" in str(e) or "422" in str(e):
                    pass  # batch predates the retention window; skip
                else:
                    raise
            batch.clear()

        t += step

    if batch:
        try:
            write_api.write(bucket=INFLUX_BUCKET, record=batch)
            written += len(batch)
        except Exception as e:
            if "outside retention policy" in str(e) or "422" in str(e):
                pass
            else:
                raise

    client.close()
    print(f"Seeded {written} points across {args.hours}h for {len(DEVICES)} devices")
    print(f"  bucket: {INFLUX_BUCKET}")
    print(f"  range: {start_time.isoformat()} → {end_time.isoformat()}")


if __name__ == "__main__":
    main()
