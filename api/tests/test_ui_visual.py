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

    mock_sites = {"sites": [{"id": "home", "label": "Home Solar", "tz_offset_hours": 0,
                              "bridge": "esp32", "ui": {"show_loads": True, "battery_display": "sense", "mppt_count": 2},
                              "device_types": ["victron_mppt", "victron_battery_sense"]}]}

    # Intercept API calls - now these will be relative to localhost:8099
    page.route("**/api/v1/sites",   lambda route: route.fulfill(json=mock_sites))
    page.route("**/api/v1/devices**", lambda route: route.fulfill(json=mock_devices))
    page.route("**/api/v1/current**", lambda route: route.fulfill(json=mock_current))
    page.route("**/api/v1/history*", lambda route: route.fulfill(json={"points": []}))
    page.route("**/api/v1/daily*", lambda route: route.fulfill(json={"days": []}))
    page.route("**/api/v1/battery**", lambda route: route.fulfill(json={}))

    # 2. Load Page (single site — picker skipped automatically)
    page.goto(static_server)

    # Wait for JS to initialize S and run at least one poll
    page.wait_for_function("typeof S !== 'undefined' && S.last !== null", timeout=10000)
    
    # 3. VERIFY ENCODING
    temp_node = page.locator("#flow-batt-t")
    assert "25.5°C" in temp_node.text_content()
    
    # 4. VERIFY SOLAR FLOW (AMBER + GLOW)
    # to_have_css implicitly proves the element exists and is active; to_be_visible() is
    # unreliable for vertical SVG paths because getBoundingClientRect() returns zero geometric
    # width for a straight vertical line (no horizontal span), which Playwright treats as hidden.
    pv1 = page.locator("#path-pv1")
    expect(pv1).to_have_css("stroke", "rgb(245, 158, 11)")
    # Check if filter is applied
    filter_val = pv1.evaluate("e => getComputedStyle(e).filter")
    assert "url" in filter_val and "glow" in filter_val

    # 5. VERIFY CHARGING FLOW (DYNAMIC COLOR)
    # Bulk = Blue
    mppt1_path = page.locator("#path-mppt1")
    expect(mppt1_path).to_have_css("stroke", "rgb(96, 165, 250)")
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

    # 8. VERIFY BATTERY TAB GRAPHS
    # Switch back to desktop view to click the tab
    page.set_viewport_size({"width": 1280, "height": 800})
    page.click("button:has-text('Battery')")
    page.wait_for_timeout(500)
    
    # Check for the 4 charts in battery tab
    active_panel = page.locator(".chart-panel.active")
    titles = active_panel.locator(".chart-title").all_text_contents()
    assert "Battery Voltage - All Sources" in titles
    assert "Temperature" in titles
    assert "Total Charging Current" in titles
    assert "Total Charging Power" in titles
    
    # Check if 4 canvases are present
    assert active_panel.locator("canvas").count() == 4

def test_ui_battery_power_math_logic(page: Page, static_server: str):
    """Verify that Total Charging Power is correctly calculated as I * V."""
    page.on("console", lambda msg: print(f"CONSOLE: {msg.type}: {msg.text}"))
    page.on("pageerror", lambda err: print(f"PAGE ERROR: {err.message}"))
    
    mock_devices = {"bridge_online": True, "devices": [
        {"id": "m1", "label": "MPPT 1", "last_seen": "2026-05-10T20:00:00Z", "online": True},
    ]}
    # Mock history: 2 Amps at 14.0 Volts = 28 Watts
    mock_history = {
        "points": [
            {"t": "2026-05-10T20:00:00Z", "v": 2.0} # 2A Current
        ]
    }
    mock_voltage = {
        "points": [
            {"t": "2026-05-10T20:00:00Z", "v": 14.0} # 14V Voltage
        ]
    }

    mock_sites = {"sites": [{"id": "home", "label": "Home Solar", "tz_offset_hours": 0,
                              "bridge": "esp32", "ui": {"show_loads": True, "battery_display": "sense", "mppt_count": 2},
                              "device_types": ["victron_mppt"]}]}

    # Intercept specific fields
    def handle_route(route):
        url = route.request.url
        if "api/v1/sites" in url:
            route.fulfill(json=mock_sites)
        elif "field=charge_current" in url:
            route.fulfill(json=mock_history)
        elif "field=battery_voltage" in url:
            route.fulfill(json=mock_voltage)
        elif "api/v1/devices" in url:
            route.fulfill(json=mock_devices)
        elif "api/v1/current" in url:
            route.fulfill(json={"m1": {"device": "m1", "label": "MPPT 1", "fields": {"charge_state": 3, "pv_power": 50}}})
        else:
            route.fulfill(json={"points": [], "devices": {"devices": []}, "days": {"days": []}})

    page.route("**/api/v1/**", handle_route)
    page.goto(static_server)
    page.wait_for_function("typeof S !== 'undefined' && S.last !== null")
    
    # Switch to Battery tab
    page.click("button:has-text('Battery')")
    page.wait_for_timeout(2000) # Give charts time to render
    
    # Inspect the Chart data directly via evaluate
    power_chart_data = page.evaluate('''() => {
        const cid = `chart-${S.tab}-p`;
        const chart = CH[cid];
        if(!chart) return "no_chart";
        if(!chart.data.datasets[0].data[0]) return "no_data";
        return chart.data.datasets[0].data[0].y;
    }''')
    
    print(f"DEBUG: power_chart_data={power_chart_data}")
    # 2A * 14V should be 28W
    assert power_chart_data == 28

if __name__ == "__main__":
    import sys
    pytest.main([__file__])
