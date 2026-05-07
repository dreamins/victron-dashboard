import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, Query, HTTPException
from influxdb_client import InfluxDBClient

INFLUX_URL = os.environ["INFLUX_URL"]
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"]
INFLUX_ORG = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET = os.environ["INFLUX_BUCKET"]
INFLUX_BUCKET_MEDIUM = os.environ.get("INFLUX_BUCKET_MEDIUM", f"{INFLUX_BUCKET}_medium")
INFLUX_BUCKET_HOURLY = os.environ.get("INFLUX_BUCKET_HOURLY", f"{INFLUX_BUCKET}_hourly")
TZ_OFFSET_HOURS = float(os.environ.get("TZ_OFFSET_HOURS", "0"))

ONLINE_S = 15
BRIDGE_S = 30
SEVEN_DAYS_S = 7 * 86400
ONE_YEAR_S = 365 * 86400

YIELD_FIELDS = {"yield_today", "yield_total"}
VALID_FIELDS = {
    "pv_power", "pv_voltage", "battery_voltage", "charge_current",
    "load_current", "load_power", "load_state", "charge_state",
    "yield_today", "yield_total", "error_code", "temperature",
    "battery_current", "charger_error",
}
FIELD_UNITS = {
    "pv_power": "W", "load_power": "W",
    "pv_voltage": "V", "battery_voltage": "V",
    "charge_current": "A", "load_current": "A", "battery_current": "A",
    "yield_today": "Wh", "yield_total": "kWh",
    "temperature": "°C",
}

_DURATION_RE = re.compile(r"^(\d+)([smhdy])$")
_UNITS_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "y": 365 * 86400}
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
query_api = client.query_api()

app = FastAPI(title="solar-api")


def _parse_s(duration: str) -> int:
    m = _DURATION_RE.match(duration.lstrip("-"))
    if not m:
        raise ValueError(f"invalid duration: {duration!r}")
    return int(m.group(1)) * _UNITS_S[m.group(2)]


def _stitch(range_s: int) -> list[tuple[str, str, str]]:
    """Return [(bucket, flux_start, flux_stop), ...] for stitched query."""
    if range_s <= SEVEN_DAYS_S:
        return [(INFLUX_BUCKET, f"-{range_s}s", "now()")]
    if range_s <= ONE_YEAR_S:
        return [
            (INFLUX_BUCKET_MEDIUM, f"-{range_s}s", "-1h"),
            (INFLUX_BUCKET, "-1h", "now()"),
        ]
    return [
        (INFLUX_BUCKET_HOURLY, f"-{range_s}s", "-7d"),
        (INFLUX_BUCKET_MEDIUM, "-7d", "-1h"),
        (INFLUX_BUCKET, "-1h", "now()"),
    ]


@app.get("/health")
def health():
    ok = False
    try:
        query_api.query(
            f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -1s) |> limit(n: 1)'
        )
        ok = True
    except Exception:
        pass
    return {"influx_ok": ok}


@app.get("/api/v1/devices")
def devices():
    now = datetime.now(timezone.utc)
    q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._field == "battery_voltage")
  |> group(columns: ["device", "label"])
  |> last()
  |> keep(columns: ["_time", "device", "label"])
"""
    tables = query_api.query(q)
    seen: dict[str, dict] = {}
    for table in tables:
        for rec in table.records:
            dev = rec.values.get("device", "")
            t = rec.get_time()
            if dev not in seen or t > seen[dev]["last_seen"]:
                seen[dev] = {
                    "id": dev,
                    "label": rec.values.get("label", dev),
                    "last_seen": t,
                }

    result = []
    latest = None
    for info in seen.values():
        ls = info["last_seen"]
        if latest is None or ls > latest:
            latest = ls
        age = (now - ls).total_seconds()
        result.append({
            "id": info["id"],
            "label": info["label"],
            "last_seen": ls.isoformat(),
            "online": age < ONLINE_S,
        })

    bridge_online = (
        latest is not None and (now - latest).total_seconds() < BRIDGE_S
    )
    return {
        "bridge_online": bridge_online,
        "devices": sorted(result, key=lambda d: d["id"]),
    }


@app.get("/api/v1/current")
def current():
    q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -2m)
  |> group(columns: ["device", "label", "_field"])
  |> last()
"""
    tables = query_api.query(q)
    result: dict[str, dict] = {}
    for table in tables:
        for rec in table.records:
            dev = rec.values.get("device", "")
            if not dev:
                continue
            if dev not in result:
                result[dev] = {
                    "device": dev,
                    "label": rec.values.get("label", dev),
                    "ts": None,
                    "fields": {},
                }
            result[dev]["fields"][rec.get_field()] = rec.get_value()
            t = rec.get_time()
            if result[dev]["ts"] is None or t > result[dev]["ts"]:
                result[dev]["ts"] = t

    for d in result.values():
        if d["ts"] is not None:
            d["ts"] = d["ts"].isoformat()
    return result


@app.get("/api/v1/history")
def history(
    device: str = Query(...),
    field: str = Query(...),
    start: str = Query(...),
    interval: str = Query(...),
):
    if not _ID_RE.match(device):
        raise HTTPException(400, "invalid device")
    if field not in VALID_FIELDS:
        raise HTTPException(400, "invalid field")
    if not _DURATION_RE.match(interval):
        raise HTTPException(400, "invalid interval")
    try:
        range_s = _parse_s(start)
    except ValueError:
        raise HTTPException(400, "invalid start")

    fn = "max" if field in YIELD_FIELDS else "mean"
    segs = _stitch(range_s)
    bucket_names = [s[0] for s in segs]

    if len(segs) == 1:
        bucket, t_start, t_stop = segs[0]
        q = f"""
from(bucket: "{bucket}")
  |> range(start: {t_start}, stop: {t_stop})
  |> filter(fn: (r) => r._measurement == "solar" and r.device == "{device}" and r._field == "{field}")
  |> aggregateWindow(every: {interval}, fn: {fn}, createEmpty: false)
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
"""
    else:
        parts = []
        for i, (bucket, t_start, t_stop) in enumerate(segs):
            parts.append(f"""t{i} = (
  from(bucket: "{bucket}")
    |> range(start: {t_start}, stop: {t_stop})
    |> filter(fn: (r) => r._measurement == "solar" and r.device == "{device}" and r._field == "{field}")
    |> aggregateWindow(every: {interval}, fn: {fn}, createEmpty: false)
    |> keep(columns: ["_time", "_value"])
)""")
        union_args = ", ".join(f"t{i}" for i in range(len(segs)))
        q = "\n".join(parts) + f"""
union(tables: [{union_args}])
  |> sort(columns: ["_time"])
"""

    tables = query_api.query(q)
    raw_points = []
    for table in tables:
        for rec in table.records:
            raw_points.append({"t": rec.get_time().isoformat(), "v": rec.get_value()})

    # Deduplicate at bucket boundaries and sort
    seen_t: dict[str, dict] = {}
    for p in raw_points:
        seen_t[p["t"]] = p
    points = sorted(seen_t.values(), key=lambda p: p["t"])

    return {
        "device": device,
        "field": field,
        "unit": FIELD_UNITS.get(field, ""),
        "buckets_used": bucket_names,
        "points": points,
    }


@app.get("/api/v1/daily")
def daily(
    days: int = Query(default=30, ge=1, le=365),
    tz_offset: Optional[float] = Query(default=None),
):
    tz_off = tz_offset if tz_offset is not None else TZ_OFFSET_HOURS
    offset = timedelta(hours=tz_off)

    q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "solar" and r._field == "yield_today")
  |> group(columns: ["device"])
"""
    tables = query_api.query(q)

    daily_max: dict[tuple[str, str], float] = {}
    for table in tables:
        for rec in table.records:
            dev = rec.values.get("device", "")
            local_t = rec.get_time() + offset
            date_str = local_t.strftime("%Y-%m-%d")
            v = float(rec.get_value() or 0.0)
            key = (dev, date_str)
            if key not in daily_max or v > daily_max[key]:
                daily_max[key] = v

    by_date: dict[str, dict] = {}
    for (dev, date_str), max_val in daily_max.items():
        if date_str not in by_date:
            by_date[date_str] = {"date": date_str, "devices": {}, "total": 0.0}
        by_date[date_str]["devices"][dev] = max_val
        by_date[date_str]["total"] += max_val

    return {"days": sorted(by_date.values(), key=lambda d: d["date"])}
