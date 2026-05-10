import pytest
from playwright.sync_api import Page, expect
import os
import json
import threading
import http.server
import socketserver
import time

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

def run_server():
    os.chdir(os.path.join(os.path.dirname(__file__), "..", "static"))
    with socketserver.TCPServer(("", 8099), QuietHandler) as httpd:
        httpd.serve_forever()

@pytest.fixture(scope="module")
def static_server():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(1) # Give server time to start
    return "http://localhost:8099/index.html"

def test_ui_visual_verification(page: Page, static_server: str):
    page.on("console", lambda msg: print(f"CONSOLE: {msg.type}: {msg.text}"))
    page.on("pageerror", lambda err: print(f"PAGE ERROR: {err.message}"))

    # 1. Mock API responses
    mock_devices = {
        "bridge_online": True,
        "devices": [
            {"id": "mppt_bulk", "label": "MPPT Bulk", "last_seen": "2026-05-10T20:00:00Z", "online": True},
            {"id": "mppt_float", "label": "MPPT Float", "last_seen": "2026-05-10T20:00:00Z", "online": True},
            {"id": "bsense", "label": "Battery Sense", "last_seen": "2026-05-10T20:00:00Z", "online": True}
        ]
    }
    mock_current = {
        "mppt_bulk": {
            "device": "mppt_bulk", "label": "MPPT Bulk", "fields": {
                "pv_power": 100.0, "charge_state": 3, "charge_current": 8.0, "battery_voltage": 13.2
            }
        },
        "mppt_float": {
            "device": "mppt_float", "label": "MPPT Float", "fields": {
                "pv_power": 1.0, "charge_state": 5, "charge_current": 0.1, "battery_voltage": 14.2
            }
        },
        "bsense": {
            "device": "bsense", "label": "Battery Sense", "fields": {
                "battery_voltage": 14.2, "temperature": 25.5
            }
        }
    }

    # Intercept API calls - now these will be relative to localhost:8099
    page.route("**/api/v1/devices", lambda route: route.fulfill(json=mock_devices))
    page.route("**/api/v1/current", lambda route: route.fulfill(json=mock_current))
    page.route("**/api/v1/history*", lambda route: route.fulfill(json={"points": []}))
    page.route("**/api/v1/daily*", lambda route: route.fulfill(json={"days": []}))

    # 2. Load Page
    page.goto(static_server)
    
    # Wait for JS to initialize S and run at least one poll
    page.wait_for_function("typeof S !== 'undefined' && S.last !== null", timeout=10000)
    
    # 3. VERIFY ENCODING
    temp_node = page.locator("#flow-batt-t")
    assert "25.5°C" in temp_node.text_content()
    
    # 4. VERIFY SOLAR FLOW (AMBER + GLOW)
    pv1 = page.locator("#path-pv1")
    expect(pv1).to_be_visible()
    # Stroke might be returned as RGB
    expect(pv1).to_have_css("stroke", "rgb(245, 158, 11)")
    # Check if filter is applied
    filter_val = pv1.evaluate("e => getComputedStyle(e).filter")
    assert "url" in filter_val and "glow" in filter_val

    # 5. VERIFY CHARGING FLOW (DYNAMIC COLOR)
    # Bulk = Blue
    mppt1_path = page.locator("#path-mppt1")
    expect(mppt1_path).to_have_css("stroke", "rgb(59, 130, 246)")
    # Float = Green
    mppt2_path = page.locator("#path-mppt2")
    expect(mppt2_path).to_have_css("stroke", "rgb(16, 185, 129)")

    # 6. VERIFY STICKY STATE
    # Mock subsequent poll with missing data
    page.route("**/api/v1/current", lambda route: route.fulfill(json={
        "mppt_bulk": {"device": "mppt_bulk", "ts": "2026-05-10T20:00:05Z", "fields": {"charge_state": 3}}
    }))
    page.evaluate("poll()")
    page.wait_for_timeout(1000)
    
    # Bulk card should still show 100W (from first poll)
    bulk_card_val = page.locator(".card").filter(has_text="MPPT Bulk").locator(".c-hero-num")
    expect(bulk_card_val).to_have_text("100 W")

    # 7. VIEWPORT AND ELEMENT CHECKS (No screenshots saved)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.evaluate("setTheme('light')")
    expect(page.locator("body")).to_have_class("light")
    
    page.evaluate("setTheme('dark')")
    expect(page.locator("body")).not_to_have_class("light")
    
    page.set_viewport_size({"width": 400, "height": 800})
    page.click(".card >> text=MPPT Bulk")
    expect(page.locator("#back-btn")).to_be_visible()
    # Check for SVG content in back button
    assert page.locator("#back-btn svg").count() > 0
