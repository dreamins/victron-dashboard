import json
import os
import pathlib
import re
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from influxdb_client import InfluxDBClient
from pydantic import BaseModel

# --- Configuration ---
INFLUX_URL            = os.environ["INFLUX_URL"]
INFLUX_TOKEN          = os.environ["INFLUX_TOKEN"]
INFLUX_ORG            = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET         = os.environ["INFLUX_BUCKET"]
INFLUX_BUCKET_MEDIUM  = os.environ.get("INFLUX_BUCKET_MEDIUM", f"{INFLUX_BUCKET}_medium")
INFLUX_BUCKET_HOURLY  = os.environ.get("INFLUX_BUCKET_HOURLY", f"{INFLUX_BUCKET}_hourly")
TZ_OFFSET_HOURS       = float(os.environ.get("TZ_OFFSET_HOURS", "0"))
SITES_FILE            = os.environ.get("SITES_FILE", "")
BLE_BRIDGE_URL        = os.environ.get("BLE_BRIDGE_URL", "")

ONLINE_S      = 90   # covers 60s write_interval_s with 30s buffer
BRIDGE_S      = 120
SEVEN_DAYS_S  = 7 * 86400
ONE_YEAR_S    = 365 * 86400

YIELD_FIELDS = {"yield_today", "yield_total"}

# Natural aggregation intervals in seconds, ascending.
# _auto_interval rounds DOWN to the largest one that fits, giving slightly
# more points than max_points but at a clean human-readable granularity.
_NATURAL_IV = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800,
               3600, 7200, 14400, 21600, 43200, 86400]


def _auto_interval(range_s: int, max_points: int) -> str:
    target = max(1, range_s // max_points)
    result = _NATURAL_IV[0]
    for iv in _NATURAL_IV:
        if iv <= target:
            result = iv
        else:
            break
    return f"{result}s"
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
_UNITS_S     = {"s": 1, "m": 60, "h": 3600, "d": 86400, "y": 365 * 86400}
_ID_RE       = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _parse_s(duration: str) -> int:
    m = _DURATION_RE.match(duration.lstrip("-"))
    if not m:
        raise ValueError(f"invalid duration: {duration!r}")
    return int(m.group(1)) * _UNITS_S[m.group(2)]


def _site_filter(site: Optional[str]) -> str:
    """Return a Flux filter line for site, or empty string if no site specified."""
    if not site:
        return ""
    return f'|> filter(fn: (r) => r.site == "{site}")'


def _load_sites_config() -> List[Dict[str, Any]]:
    """Read sites.json; return empty list if file absent or not configured."""
    if not SITES_FILE or not pathlib.Path(SITES_FILE).exists():
        return []
    with open(SITES_FILE) as f:
        data = json.load(f)
    # Strip secrets (mac, key) before returning to callers
    sites = []
    for s in data.get("sites", []):
        sites.append({
            "id":              s["id"],
            "label":           s.get("label", s["id"]),
            "tz_offset_hours": s.get("tz_offset_hours", 0),
            "bridge":          s.get("bridge", "esp32"),
            "ui":              s.get("ui", {}),
            "device_types":    list({d.get("type", "unknown") for d in s.get("devices", [])}),
        })
    return sites


def _load_sites_raw() -> dict:
    """Read sites.json with secrets intact (mac, key). Returns full dict."""
    if not SITES_FILE or not pathlib.Path(SITES_FILE).exists():
        return {"sites": []}
    with open(SITES_FILE) as f:
        return json.load(f)


def _write_sites_atomic(data: dict) -> None:
    """Write sites.json safely: validate serialization via a temp file, then overwrite.

    os.replace() fails with EBUSY on Docker bind-mounted files (the mount point is
    pinned and cannot be replaced by rename). shutil.copy2 writes in-place to the
    existing inode, which works on bind mounts.
    """
    path = pathlib.Path(SITES_FILE)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        shutil.copy2(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    else:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def _bridge_reload() -> None:
    """Signal ble-bridge to reload sites.json. No-op if BLE_BRIDGE_URL unset."""
    if not BLE_BRIDGE_URL:
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{BLE_BRIDGE_URL}/reload")
        resp.raise_for_status()


class DeviceCreate(BaseModel):
    id:          str
    label:       str
    type:        str            # e.g. "litime_bms", "victron_smartsolar"
    mac:         Optional[str] = None
    write_uuid:  Optional[str] = None
    notify_uuid: Optional[str] = None
    key:         Optional[str] = None  # BLE encryption key (Victron devices)


class InfluxRepository:
    def __init__(self):
        self.client    = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        self.query_api = self.client.query_api()

    def get_health(self) -> bool:
        try:
            self.query_api.query(f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -1s) |> limit(n: 1)')
            return True
        except Exception:
            return False

    def get_devices(self, site: Optional[str] = None) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._field == "battery_voltage")
  {_site_filter(site)}
  |> group(columns: ["device", "label"])
  |> last()
  |> keep(columns: ["_time", "device", "label"])
"""
        tables = self.query_api.query(q)
        seen: Dict[str, Dict] = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                t   = rec.get_time()
                if dev not in seen or t > seen[dev]["last_seen"]:
                    seen[dev] = {
                        "id":        dev,
                        "label":     rec.values.get("label", dev),
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
                "id":        info["id"],
                "label":     info["label"],
                "last_seen": ls.isoformat(),
                "online":    age < ONLINE_S,
            })

        bridge_online = latest is not None and (now - latest).total_seconds() < BRIDGE_S
        return {"bridge_online": bridge_online, "devices": sorted(result, key=lambda d: d["id"])}

    def get_battery(self, site: Optional[str] = None,
                    device: Optional[str] = None) -> Dict[str, Any]:
        dev_filter = (f'|> filter(fn: (r) => r.device == "{device}")'
                      if device and _ID_RE.match(device) else "")
        q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "battery")
  {_site_filter(site)}
  {dev_filter}
  |> group(columns: ["device", "label", "_field"])
  |> last()
"""
        tables = self.query_api.query(q)
        result: Dict[str, Dict] = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                if not dev:
                    continue
                if dev not in result:
                    result[dev] = {
                        "device": dev,
                        "label":  rec.values.get("label", dev),
                        "ts":     None,
                        "fields": {},
                    }
                result[dev]["fields"][rec.get_field()] = rec.get_value()
                t = rec.get_time()
                if result[dev]["ts"] is None or t > result[dev]["ts"]:
                    result[dev]["ts"] = t
        for d in result.values():
            if d["ts"]:
                d["ts"] = d["ts"].isoformat()
        return result

    def get_current(self, site: Optional[str] = None) -> Dict[str, Any]:
        q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -7d)
  {_site_filter(site)}
  |> group(columns: ["device", "label", "_field"])
  |> last()
"""
        tables = self.query_api.query(q)
        result: Dict[str, Dict] = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                if not dev:
                    continue
                if dev not in result:
                    result[dev] = {"device": dev, "label": rec.values.get("label", dev), "ts": None, "fields": {}}
                result[dev]["fields"][rec.get_field()] = rec.get_value()
                t = rec.get_time()
                if result[dev]["ts"] is None or t > result[dev]["ts"]:
                    result[dev]["ts"] = t

        for d in result.values():
            if d["ts"]:
                d["ts"] = d["ts"].isoformat()
        return result

    def _actual_span_s(self, device: str, field: str, start_s: int,
                       site: Optional[str]) -> Optional[int]:
        """Return seconds from earliest data point to now, or None if no data.
        Uses first() which hits the TSM index — cheap even on large buckets."""
        segs = self._stitch(start_s)
        bucket, t_start, _ = segs[0]
        q = f"""
from(bucket: "{bucket}")
  |> range(start: {t_start})
  |> filter(fn: (r) => r._measurement == "solar" and r.device == "{device}" and r._field == "{field}")
  {_site_filter(site)}
  |> first()
  |> keep(columns: ["_time"])
"""
        try:
            tables = self.query_api.query(q)
            for table in tables:
                for rec in table.records:
                    span = int((datetime.now(timezone.utc) - rec.get_time()).total_seconds())
                    return max(span, 1)
        except Exception:
            pass
        return None

    def get_history(self, device: str, field: str, start_s: int, interval: str,
                    site: Optional[str] = None, max_points: int = 500) -> Dict[str, Any]:
        fn   = "max" if field in YIELD_FIELDS else "mean"
        segs = self._stitch(start_s)
        bucket_names = [s[0] for s in segs]

        if len(segs) == 1:
            bucket, t_start, t_stop = segs[0]
            q = f"""
from(bucket: "{bucket}")
  |> range(start: {t_start}, stop: {t_stop})
  |> filter(fn: (r) => r._measurement == "solar" and r.device == "{device}" and r._field == "{field}")
  {_site_filter(site)}
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
    {_site_filter(site)}
    |> aggregateWindow(every: {interval}, fn: {fn}, createEmpty: false)
    |> keep(columns: ["_time", "_value"])
)""")
            union_args = ", ".join(f"t{i}" for i in range(len(segs)))
            q = "\n".join(parts) + f'\nunion(tables: [{union_args}])\n  |> sort(columns: ["_time"])'

        tables     = self.query_api.query(q)
        raw_points = []
        for table in tables:
            for rec in table.records:
                raw_points.append({"t": rec.get_time().isoformat(), "v": rec.get_value()})

        seen_t: Dict[str, Dict] = {}
        for p in raw_points:
            seen_t[p["t"]] = p

        return {
            "device":       device,
            "field":        field,
            "unit":         FIELD_UNITS.get(field, ""),
            "interval":     interval,
            "buckets_used": bucket_names,
            "points":       sorted(seen_t.values(), key=lambda p: p["t"]),
        }

    def get_daily(self, days: int, offset: timedelta, today_only: bool = False,
                  site: Optional[str] = None) -> List[Dict[str, Any]]:
        if today_only:
            now_local    = datetime.now(timezone.utc) + offset
            midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_utc   = midnight_local - offset
            start_str      = midnight_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            q = f"""
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {start_str})
  |> filter(fn: (r) => r._measurement == "solar" and r._field == "yield_today")
  {_site_filter(site)}
  |> group(columns: ["device"])
  |> last()
"""
            tables   = self.query_api.query(q)
            date_str = (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%d")
            by_date: Dict[str, Dict] = {}
            for table in tables:
                for rec in table.records:
                    dev = rec.values.get("device", "")
                    if not dev:
                        continue
                    v = float(rec.get_value() or 0.0)
                    if date_str not in by_date:
                        by_date[date_str] = {"date": date_str, "devices": {}, "total": 0.0}
                    by_date[date_str]["devices"][dev] = v
                    by_date[date_str]["total"] += v
            return sorted(by_date.values(), key=lambda d: d["date"])

        bucket    = INFLUX_BUCKET_MEDIUM if days > 1 else INFLUX_BUCKET
        offset_s  = int(offset.total_seconds())
        flux_offset = f"-{abs(offset_s)}s" if offset_s >= 0 else f"{abs(offset_s)}s"
        q = f"""
from(bucket: "{bucket}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "solar" and r._field == "yield_today")
  {_site_filter(site)}
  |> group(columns: ["device"])
  |> aggregateWindow(every: 1d, fn: max, offset: {flux_offset}, createEmpty: false)
  |> keep(columns: ["_time", "_value", "device"])
"""
        tables  = self.query_api.query(q)
        by_date = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                if not dev:
                    continue
                local_t  = (rec.get_time() - timedelta(seconds=1)) + offset
                date_str = local_t.strftime("%Y-%m-%d")
                v        = float(rec.get_value() or 0.0)
                if date_str not in by_date:
                    by_date[date_str] = {"date": date_str, "devices": {}, "total": 0.0}
                by_date[date_str]["devices"][dev] = v
                by_date[date_str]["total"] += v
        return sorted(by_date.values(), key=lambda d: d["date"])

    def _stitch(self, range_s: int) -> List[Tuple[str, str, str]]:
        if range_s <= SEVEN_DAYS_S:
            return [(INFLUX_BUCKET, f"-{range_s}s", "now()")]
        if range_s <= ONE_YEAR_S:
            return [(INFLUX_BUCKET_MEDIUM, f"-{range_s}s", "-1h"), (INFLUX_BUCKET, "-1h", "now()")]
        return [
            (INFLUX_BUCKET_HOURLY, f"-{range_s}s", "-7d"),
            (INFLUX_BUCKET_MEDIUM, "-7d", "-1h"),
            (INFLUX_BUCKET,        "-1h", "now()"),
        ]


app  = FastAPI(title="solar-api")
repo = InfluxRepository()


@app.get("/health")
def health():
    return {"influx_ok": repo.get_health()}


@app.get("/api/v1/sites")
def sites():
    return {"sites": _load_sites_config()}


@app.get("/api/v1/devices")
def devices(site: Optional[str] = Query(default=None)):
    return repo.get_devices(site=site)


@app.get("/api/v1/current")
def current(site: Optional[str] = Query(default=None)):
    return repo.get_current(site=site)


@app.get("/api/v1/history")
def history(
    device:     str            = Query(...),
    field:      str            = Query(...),
    start:      str            = Query(...),
    interval:   Optional[str]  = Query(default=None),
    max_points: int            = Query(default=500, ge=1, le=5000),
    site:       Optional[str]  = Query(default=None),
):
    if not _ID_RE.match(device):
        raise HTTPException(400, "invalid device")
    if field not in VALID_FIELDS:
        raise HTTPException(400, "invalid field")
    if interval is not None and not _DURATION_RE.match(interval):
        raise HTTPException(400, "invalid interval")
    try:
        range_s = _parse_s(start)
    except ValueError:
        raise HTTPException(400, "invalid start")
    if interval is not None:
        iv = interval
    else:
        # Use actual data span so short datasets get fine resolution even on
        # long-range queries (e.g. "All" with only a week of data uses ~30s
        # intervals instead of 12h).
        span = repo._actual_span_s(device, field, range_s, site)
        iv = _auto_interval(span if span else range_s, max_points)
    return repo.get_history(device, field, range_s, iv, site=site)


@app.get("/api/v1/battery")
def battery(
    site:   Optional[str] = Query(default=None),
    device: Optional[str] = Query(default=None),
):
    return repo.get_battery(site=site, device=device)


@app.get("/api/v1/daily")
def daily(
    days:       int            = Query(default=30, ge=1, le=365),
    tz_offset:  Optional[float] = Query(default=None),
    today_only: bool           = Query(default=False),
    site:       Optional[str]  = Query(default=None),
):
    tz_off = tz_offset if tz_offset is not None else TZ_OFFSET_HOURS
    return {"days": repo.get_daily(days, timedelta(hours=tz_off), today_only=today_only, site=site)}


@app.get("/api/v1/scan/bms")
async def scan_bms(site: str = Query(...)):
    """Proxy a BMS discovery scan to ble-bridge. Returns found devices with live SOC/V/temp.
    Only valid for sites with bridge=ble. Takes up to ~55 s."""
    all_sites = _load_sites_config()
    s = next((x for x in all_sites if x["id"] == site), None)
    if s is None:
        raise HTTPException(404, "site not found")
    if s.get("bridge") not in ("ble", "linux_ble"):
        raise HTTPException(400, "site does not use a BLE bridge — scan not available")
    if not BLE_BRIDGE_URL:
        raise HTTPException(503, "BLE_BRIDGE_URL not configured")
    async with httpx.AsyncClient(timeout=65.0) as client:
        try:
            resp = await client.post(f"{BLE_BRIDGE_URL}/scan-bms")
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            raise HTTPException(504, "BMS scan timed out")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"Bridge error: {exc.response.text}")
        except Exception as exc:
            raise HTTPException(502, f"Bridge unreachable: {exc}")


@app.post("/api/v1/sites/{site_id}/devices")
async def add_device(site_id: str, device: DeviceCreate):
    """Add a device to a site in sites.json and reload ble-bridge."""
    if not _ID_RE.match(site_id):
        raise HTTPException(400, "invalid site_id")
    if not _ID_RE.match(device.id):
        raise HTTPException(400, "invalid device id")
    if not SITES_FILE:
        raise HTTPException(503, "SITES_FILE not configured")
    path = pathlib.Path(SITES_FILE)
    if not path.exists():
        raise HTTPException(404, "sites.json not found")

    data = _load_sites_raw()
    site = next((s for s in data.get("sites", []) if s["id"] == site_id), None)
    if site is None:
        raise HTTPException(404, "site not found")
    if any(d["id"] == device.id for d in site.get("devices", [])):
        raise HTTPException(409, f"device '{device.id}' already exists in site '{site_id}'")

    entry: Dict[str, Any] = {"id": device.id, "label": device.label, "type": device.type}
    if device.mac:         entry["mac"]         = device.mac
    if device.write_uuid:  entry["write_uuid"]  = device.write_uuid
    if device.notify_uuid: entry["notify_uuid"] = device.notify_uuid
    if device.key:         entry["key"]         = device.key

    site.setdefault("devices", []).append(entry)
    _write_sites_atomic(data)
    await _bridge_reload()
    return {"ok": True, "device": device.id}


@app.delete("/api/v1/sites/{site_id}/devices/{device_id}")
async def remove_device(site_id: str, device_id: str):
    """Remove a device from a site in sites.json and reload ble-bridge."""
    if not _ID_RE.match(site_id) or not _ID_RE.match(device_id):
        raise HTTPException(400, "invalid id")
    if not SITES_FILE:
        raise HTTPException(503, "SITES_FILE not configured")
    path = pathlib.Path(SITES_FILE)
    if not path.exists():
        raise HTTPException(404, "sites.json not found")

    data = _load_sites_raw()
    site = next((s for s in data.get("sites", []) if s["id"] == site_id), None)
    if site is None:
        raise HTTPException(404, "site not found")

    before = len(site.get("devices", []))
    site["devices"] = [d for d in site.get("devices", []) if d["id"] != device_id]
    if len(site["devices"]) == before:
        raise HTTPException(404, f"device '{device_id}' not found in site '{site_id}'")

    _write_sites_atomic(data)
    await _bridge_reload()
    return {"ok": True}


_STATIC = pathlib.Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
