"""Unit tests for Phase 12 device-management endpoints.

Run inside the solar-api container (docker compose exec solar-api pytest tests/):
  pytest tests/test_device_mgmt.py -v
"""
import json
import os
import pathlib
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Import path works both in-container (/app/main.py) and from repo root (api/main.py)
try:
    from main import app, _write_sites_atomic
except ImportError:
    from api.main import app, _write_sites_atomic


SITES_DATA = {
    "sites": [
        {
            "id": "garage",
            "label": "Garage Solar",
            "bridge": "ble",
            "tz_offset_hours": 0,
            "ui": {"show_loads": False, "battery_display": "bms", "mppt_count": 2},
            "devices": [
                {"id": "mppt1", "label": "MPPT 1", "type": "victron_smartsolar",
                 "mac": "AA:BB:CC:DD:EE:11", "key": "aabbccddeeff00112233445566778811"},
            ],
        },
        {
            "id": "home",
            "label": "Home Solar",
            "bridge": "esp32",
            "tz_offset_hours": 0,
            "ui": {"show_loads": True, "battery_display": "sense", "mppt_count": 2},
            "devices": [],
        },
    ]
}


@pytest.fixture
def sites_path(tmp_path):
    p = tmp_path / "sites.json"
    p.write_text(json.dumps(SITES_DATA, indent=2))
    return p


@pytest.fixture
def client(sites_path):
    """TestClient with patched module-level SITES_FILE and a silent InfluxDB."""
    from fastapi.testclient import TestClient
    import main as _main
    orig_sites_file      = _main.SITES_FILE
    orig_ble_bridge_url  = _main.BLE_BRIDGE_URL

    _main.SITES_FILE     = str(sites_path)
    _main.BLE_BRIDGE_URL = ""          # disabled — no bridge in test

    with patch.object(_main, "repo") as mock_repo:
        mock_repo.get_health.return_value = True
        yield TestClient(app, raise_server_exceptions=False)

    _main.SITES_FILE     = orig_sites_file
    _main.BLE_BRIDGE_URL = orig_ble_bridge_url


# ── POST /api/v1/sites/{site}/devices ────────────────────────────────────────

def test_add_bms_device(client, sites_path):
    with patch("main._bridge_reload", new_callable=AsyncMock):
        resp = client.post("/api/v1/sites/garage/devices", json={
            "id": "bms_main",
            "label": "LiTime Battery",
            "type": "litime_bms",
            "mac": "AA:BB:CC:DD:EE:33",
            "write_uuid":  "0000ff01-0000-1000-8000-00805f9b34fb",
            "notify_uuid": "0000ff02-0000-1000-8000-00805f9b34fb",
        })
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    updated = json.loads(sites_path.read_text())
    ids = [d["id"] for d in updated["sites"][0]["devices"]]
    assert "bms_main" in ids
    bms = next(d for d in updated["sites"][0]["devices"] if d["id"] == "bms_main")
    assert bms["mac"] == "AA:BB:CC:DD:EE:33"
    assert bms["write_uuid"] == "0000ff01-0000-1000-8000-00805f9b34fb"


def test_add_device_duplicate_id(client):
    with patch("main._bridge_reload", new_callable=AsyncMock):
        resp = client.post("/api/v1/sites/garage/devices", json={
            "id": "mppt1", "label": "MPPT duplicate", "type": "victron_smartsolar",
        })
    assert resp.status_code == 409


def test_add_device_site_not_found(client):
    with patch("main._bridge_reload", new_callable=AsyncMock):
        resp = client.post("/api/v1/sites/nonexistent/devices", json={
            "id": "bms1", "label": "BMS", "type": "litime_bms",
        })
    assert resp.status_code == 404


def test_add_device_invalid_id(client):
    resp = client.post("/api/v1/sites/garage/devices", json={
        "id": "bad id!!", "label": "Bad", "type": "litime_bms",
    })
    assert resp.status_code == 400


def test_add_mppt_without_key(client, sites_path):
    """Key field is optional — device added without it."""
    with patch("main._bridge_reload", new_callable=AsyncMock):
        resp = client.post("/api/v1/sites/garage/devices", json={
            "id": "mppt2", "label": "MPPT 2", "type": "victron_smartsolar",
            "mac": "AA:BB:CC:DD:EE:22",
        })
    assert resp.status_code == 200


# ── DELETE /api/v1/sites/{site}/devices/{device} ──────────────────────────────

def test_remove_device_ok(client, sites_path):
    with patch("main._bridge_reload", new_callable=AsyncMock) as mock_reload:
        resp = client.delete("/api/v1/sites/garage/devices/mppt1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    mock_reload.assert_called_once()

    updated = json.loads(sites_path.read_text())
    ids = [d["id"] for d in updated["sites"][0]["devices"]]
    assert "mppt1" not in ids


def test_remove_device_not_found(client):
    with patch("main._bridge_reload", new_callable=AsyncMock):
        resp = client.delete("/api/v1/sites/garage/devices/nonexistent")
    assert resp.status_code == 404


def test_remove_device_site_not_found(client):
    with patch("main._bridge_reload", new_callable=AsyncMock):
        resp = client.delete("/api/v1/sites/nosite/devices/mppt1")
    assert resp.status_code == 404


# ── GET /api/v1/scan/bms ──────────────────────────────────────────────────────

def test_scan_bms_esp32_site_rejected(client):
    """ESP32 sites should return 400 — scan requires BLE bridge."""
    resp = client.get("/api/v1/scan/bms", params={"site": "home"})
    assert resp.status_code == 400


def test_scan_bms_site_not_found(client):
    resp = client.get("/api/v1/scan/bms", params={"site": "nosite"})
    assert resp.status_code == 404


def test_scan_bms_no_bridge_url(client):
    """When BLE_BRIDGE_URL is empty, scan returns 503."""
    resp = client.get("/api/v1/scan/bms", params={"site": "garage"})
    assert resp.status_code == 503


def test_scan_bms_proxies_to_bridge(client, sites_path):
    import main as _main
    _main.BLE_BRIDGE_URL = "http://fake-bridge:8088"
    try:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"mac": "AA:BB:CC:DD:EE:FF", "soc": 91.0, "voltage": 13.31, "temp": 22.0}
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch("main.httpx.AsyncClient") as mock_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__  = AsyncMock(return_value=False)
            mock_ctx.post       = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_ctx

            resp = client.get("/api/v1/scan/bms", params={"site": "garage"})

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert body[0]["mac"] == "AA:BB:CC:DD:EE:FF"
    finally:
        _main.BLE_BRIDGE_URL = ""


# ── Atomic write safety ───────────────────────────────────────────────────────

def test_atomic_write_leaves_no_temp_on_failure(tmp_path):
    """If serialization fails, no .tmp file is left and original is intact."""
    path = tmp_path / "sites.json"
    path.write_text(json.dumps({"sites": []}))

    import main as _main
    orig = _main.SITES_FILE
    _main.SITES_FILE = str(path)
    try:
        class _Unserializable:
            pass
        with pytest.raises(Exception):
            _write_sites_atomic({"sites": [_Unserializable()]})

        assert path.read_text() == json.dumps({"sites": []})
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        _main.SITES_FILE = orig
