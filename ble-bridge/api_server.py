"""aiohttp management server — exposed only on internal Docker network (port 8088).

Routes:
  GET  /health     → {"ok": true}
  POST /scan-bms   → stop scanner, probe all LiTime devices, read one frame each,
                     restart scanner; returns [{mac, soc, voltage, temp, ...}]
  POST /reload     → re-read sites.json and apply updated device map atomically
"""
import asyncio
import logging
import os

from aiohttp import web

log = logging.getLogger(__name__)

API_PORT = int(os.environ.get("API_PORT", "8088"))


def make_app(controller) -> web.Application:
    app = web.Application()
    app["controller"] = controller
    app.router.add_get("/health",        _health)
    app.router.add_get("/scan-victron",  _scan_victron)
    app.router.add_post("/scan-bms",     _scan_bms)
    app.router.add_post("/reload",       _reload)
    return app


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _scan_victron(request: web.Request) -> web.Response:
    controller = request.app["controller"]
    try:
        results = await asyncio.wait_for(controller.scan_victron(), timeout=25.0)
        return web.json_response(results)
    except asyncio.TimeoutError:
        return web.json_response({"error": "scan timed out"}, status=504)
    except Exception as exc:
        log.error("scan-victron error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


async def _scan_bms(request: web.Request) -> web.Response:
    controller = request.app["controller"]
    try:
        results = await asyncio.wait_for(controller.scan_bms(), timeout=120.0)
        return web.json_response(results)
    except asyncio.TimeoutError:
        return web.json_response({"error": "scan timed out"}, status=504)
    except Exception as exc:
        log.error("scan-bms error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


async def _reload(request: web.Request) -> web.Response:
    controller = request.app["controller"]
    try:
        await controller.reload()
        return web.json_response({"ok": True})
    except Exception as exc:
        log.error("reload error: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)


async def run_api_server(controller) -> None:
    """Start the aiohttp server; runs until cancelled."""
    app    = make_app(controller)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    log.info("BLE bridge API listening on :%d", API_PORT)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
