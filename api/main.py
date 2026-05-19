"""FastAPI application — route definitions only.

All business logic lives in config.py and repository.py.
This file's only job is to wire them together with HTTP.
"""
import os
import pathlib
from datetime import timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from influxdb_client import InfluxDBClient
from pydantic import BaseModel

from config import (
    VALID_FIELDS,
    device_measurement, load_sites, load_sites_raw, write_sites_atomic,
)
from repository import InfluxRepository, ID_RE, auto_interval, parse_duration_s, valid_duration

# ── Environment ───────────────────────────────────────────────────────────────
INFLUX_URL           = os.environ["INFLUX_URL"]
INFLUX_TOKEN         = os.environ["INFLUX_TOKEN"]
INFLUX_ORG           = os.environ.get("INFLUX_ORG", "home")
INFLUX_BUCKET        = os.environ["INFLUX_BUCKET"]
INFLUX_BUCKET_MEDIUM = os.environ.get("INFLUX_BUCKET_MEDIUM", f"{INFLUX_BUCKET}_medium")
INFLUX_BUCKET_HOURLY = os.environ.get("INFLUX_BUCKET_HOURLY", f"{INFLUX_BUCKET}_hourly")
TZ_OFFSET_HOURS      = float(os.environ.get("TZ_OFFSET_HOURS", "0"))
SITES_FILE           = os.environ.get("SITES_FILE", "")
BLE_BRIDGE_URL       = os.environ.get("BLE_BRIDGE_URL", "")

# ── App + repo ────────────────────────────────────────────────────────────────
app  = FastAPI(title="solar-api")
_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
repo = InfluxRepository(
    query_api      = _client.query_api(),
    bucket         = INFLUX_BUCKET,
    bucket_medium  = INFLUX_BUCKET_MEDIUM,
    bucket_hourly  = INFLUX_BUCKET_HOURLY,
)


class DeviceCreate(BaseModel):
    id:          str
    label:       str
    type:        str
    mac:         Optional[str] = None
    write_uuid:  Optional[str] = None
    notify_uuid: Optional[str] = None
    key:         Optional[str] = None


async def _bridge_reload() -> None:
    if not BLE_BRIDGE_URL:
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{BLE_BRIDGE_URL}/reload")
        resp.raise_for_status()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"influx_ok": repo.get_health()}


@app.get("/api/v1/sites")
def sites():
    return {"sites": load_sites(SITES_FILE)}


@app.get("/api/v1/devices")
def devices(site: Optional[str] = Query(default=None)):
    return repo.get_devices(site=site)


@app.get("/api/v1/current")
def current(site: Optional[str] = Query(default=None)):
    return repo.get_current(site=site)


@app.get("/api/v1/history")
def history(
    device:     str           = Query(...),
    field:      str           = Query(...),
    start:      str           = Query(...),
    interval:   Optional[str] = Query(default=None),
    max_points: int           = Query(default=500, ge=1, le=5000),
    site:       Optional[str] = Query(default=None),
):
    if not ID_RE.match(device):
        raise HTTPException(400, "invalid device")
    if field not in VALID_FIELDS:
        raise HTTPException(400, "invalid field")
    if interval is not None and not valid_duration(interval):
        raise HTTPException(400, "invalid interval")
    try:
        range_s = parse_duration_s(start)
    except ValueError:
        raise HTTPException(400, "invalid start")
    meas = device_measurement(SITES_FILE, site, device)
    if interval is not None:
        iv = interval
    else:
        span = repo._actual_span_s(device, field, range_s, site, measurement=meas)
        iv   = auto_interval(span if span else range_s, max_points)
    return repo.get_history(device, field, range_s, iv, site=site, measurement=meas)


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
    return {"days": repo.get_daily(days, timedelta(hours=tz_off),
                                   today_only=today_only, site=site)}


@app.get("/api/v1/scan/bms")
async def scan_bms(site: str = Query(...)):
    all_sites = load_sites(SITES_FILE)
    s = next((x for x in all_sites if x["id"] == site), None)
    if s is None:
        raise HTTPException(404, "site not found")
    if s.get("bridge") not in ("ble", "linux_ble"):
        raise HTTPException(400, "site does not use a BLE bridge — scan not available")
    if not BLE_BRIDGE_URL:
        raise HTTPException(503, "BLE_BRIDGE_URL not configured")
    async with httpx.AsyncClient(timeout=130.0) as client:
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


@app.get("/api/v1/scan/victron")
async def scan_victron(site: str = Query(...)):
    all_sites = load_sites(SITES_FILE)
    s = next((x for x in all_sites if x["id"] == site), None)
    if s is None:
        raise HTTPException(404, "site not found")
    if s.get("bridge") not in ("ble", "linux_ble"):
        raise HTTPException(400, "site does not use a BLE bridge — scan not available")
    if not BLE_BRIDGE_URL:
        raise HTTPException(503, "BLE_BRIDGE_URL not configured")
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            resp = await client.get(f"{BLE_BRIDGE_URL}/scan-victron")
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            raise HTTPException(504, "Victron scan timed out")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"Bridge error: {exc.response.text}")
        except Exception as exc:
            raise HTTPException(502, f"Bridge unreachable: {exc}")


@app.post("/api/v1/sites/{site_id}/devices")
async def add_device(site_id: str, device: DeviceCreate):
    if not ID_RE.match(site_id):
        raise HTTPException(400, "invalid site_id")
    if not ID_RE.match(device.id):
        raise HTTPException(400, "invalid device id")
    if not SITES_FILE:
        raise HTTPException(503, "SITES_FILE not configured")
    if not pathlib.Path(SITES_FILE).exists():
        raise HTTPException(404, "sites.json not found")

    data = load_sites_raw(SITES_FILE)
    site = next((s for s in data.get("sites", []) if s["id"] == site_id), None)
    if site is None:
        raise HTTPException(404, "site not found")
    if any(d["id"] == device.id for d in site.get("devices", [])):
        raise HTTPException(409, f"device '{device.id}' already exists in site '{site_id}'")

    entry = {"id": device.id, "label": device.label, "type": device.type}
    if device.mac:         entry["mac"]         = device.mac
    if device.write_uuid:  entry["write_uuid"]  = device.write_uuid
    if device.notify_uuid: entry["notify_uuid"] = device.notify_uuid
    if device.key:         entry["key"]         = device.key

    site.setdefault("devices", []).append(entry)
    write_sites_atomic(SITES_FILE, data)
    await _bridge_reload()
    return {"ok": True, "device": device.id}


@app.delete("/api/v1/sites/{site_id}/devices/{device_id}")
async def remove_device(site_id: str, device_id: str):
    if not ID_RE.match(site_id) or not ID_RE.match(device_id):
        raise HTTPException(400, "invalid id")
    if not SITES_FILE:
        raise HTTPException(503, "SITES_FILE not configured")
    if not pathlib.Path(SITES_FILE).exists():
        raise HTTPException(404, "sites.json not found")

    data = load_sites_raw(SITES_FILE)
    site = next((s for s in data.get("sites", []) if s["id"] == site_id), None)
    if site is None:
        raise HTTPException(404, "site not found")

    before = len(site.get("devices", []))
    site["devices"] = [d for d in site.get("devices", []) if d["id"] != device_id]
    if len(site["devices"]) == before:
        raise HTTPException(404, f"device '{device_id}' not found in site '{site_id}'")

    write_sites_atomic(SITES_FILE, data)
    await _bridge_reload()
    return {"ok": True}


_STATIC = pathlib.Path(__file__).parent / "static"
if _STATIC.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
