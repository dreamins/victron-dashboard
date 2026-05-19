"""Unit tests for ble-bridge/api_server.py using aiohttp's test client."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytest_plugins = ("pytest_asyncio",)


class _FakeController:
    """Minimal BridgeController stand-in for tests."""

    def __init__(self, scan_result=None, victron_result=None, reload_raises=None):
        self._scan_result    = scan_result or []
        self._victron_result = victron_result or []
        self._reload_raises  = reload_raises
        self.reload_called   = False
        self.scan_called     = False
        self.victron_scan_called = False

    async def reload(self):
        self.reload_called = True
        if self._reload_raises:
            raise self._reload_raises

    async def scan_bms(self):
        self.scan_called = True
        return self._scan_result

    async def scan_victron(self):
        self.victron_scan_called = True
        return self._victron_result


@pytest.fixture
def controller():
    return _FakeController(
        scan_result=[
            {"mac": "AA:BB:CC:DD:00:01", "soc": 91.0, "voltage": 13.31, "temp": 22.0,
             "write_uuid": "aaaa", "notify_uuid": "bbbb"},
        ],
        victron_result=[
            {"mac": "CC:08:F7:F7:00:01", "name": "BSC IP22 12/30", "rssi": -72},
            {"mac": "F5:0D:91:F4:00:02", "name": "SmartSolar 150/75", "rssi": -59},
        ],
    )


@pytest.fixture
def app(controller):
    from api_server import make_app
    return make_app(controller)


# ── /health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(app):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True}


# ── /reload ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reload_ok(app, controller):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/reload")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True}
        assert controller.reload_called


@pytest.mark.asyncio
async def test_reload_error():
    ctrl = _FakeController(reload_raises=RuntimeError("disk full"))
    from api_server import make_app
    app = make_app(ctrl)
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/reload")
        assert resp.status == 500
        body = await resp.json()
        assert "error" in body


# ── /scan-bms ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_bms_returns_results(app, controller):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/scan-bms")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["mac"] == "AA:BB:CC:DD:00:01"
        assert body[0]["soc"] == 91.0
        assert controller.scan_called


@pytest.mark.asyncio
async def test_scan_bms_empty():
    ctrl = _FakeController(scan_result=[])
    from api_server import make_app
    app = make_app(ctrl)
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/scan-bms")
        assert resp.status == 200
        assert await resp.json() == []


@pytest.mark.asyncio
async def test_scan_bms_timeout():
    class _SlowCtrl:
        async def scan_bms(self):
            await asyncio.sleep(999)  # never returns in test

    from api_server import make_app
    app = make_app(_SlowCtrl())
    from aiohttp.test_utils import TestClient, TestServer
    # Patch asyncio.wait_for to simulate timeout
    import api_server as _as_mod
    original = asyncio.wait_for

    async def _timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    _as_mod_wait = _as_mod
    with patch.object(_as_mod, "asyncio") as mock_asyncio:
        mock_asyncio.wait_for = _timeout
        mock_asyncio.TimeoutError = asyncio.TimeoutError
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/scan-bms")
            assert resp.status == 504


# ── /scan-victron ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_victron_returns_results(app, controller):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/scan-victron")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert body[0]["mac"] == "CC:08:F7:F7:00:01"
        assert body[0]["rssi"] == -72
        assert controller.victron_scan_called


@pytest.mark.asyncio
async def test_scan_victron_empty():
    ctrl = _FakeController(victron_result=[])
    from api_server import make_app
    app = make_app(ctrl)
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/scan-victron")
        assert resp.status == 200
        assert await resp.json() == []


@pytest.mark.asyncio
async def test_scan_victron_error():
    class _ErrorCtrl:
        async def scan_victron(self):
            raise RuntimeError("adapter failure")

    from api_server import make_app
    app = make_app(_ErrorCtrl())
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/scan-victron")
        assert resp.status == 500
        body = await resp.json()
        assert "error" in body


@pytest.mark.asyncio
async def test_scan_bms_error():
    class _ErrorCtrl:
        async def scan_bms(self):
            raise RuntimeError("BLE adapter not found")

    from api_server import make_app
    app = make_app(_ErrorCtrl())
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/scan-bms")
        assert resp.status == 500
        body = await resp.json()
        assert "error" in body
