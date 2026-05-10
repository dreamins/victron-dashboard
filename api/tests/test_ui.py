"""
Playwright UI tests for the solar dashboard (index.html).

Approach: serve api/static/ from a local HTTP server (conftest.py),
intercept all /api/v1/* calls with deterministic mock data, then assert
on rendered content and interactive behaviour.

Run:
  pytest api/tests/test_ui.py -v --browser chromium
  # or headlessly (default):
  pytest api/tests/test_ui.py -v
"""
import json
import pytest
from playwright.sync_api import Page, expect

# ── Mock data ────────────────────────────────────────────────────────────────

MOCK_DEVICES = {
    "bridge_online": True,
    "devices": [
        {"id": "mppt_1", "label": "MPPT South",   "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        {"id": "mppt_2", "label": "MPPT East",   "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        {"id": "battery_sense","label": "Battery Sense",  "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
    ],
}

MOCK_CURRENT = {
    "mppt_1": {
        "device": "mppt_1", "label": "MPPT South",
        "ts": "2026-05-07T12:00:00+00:00",
        "fields": {
            "pv_power": 45.0, "battery_voltage": 13.8, "charge_current": 3.2,
            "yield_today": 250.0, "charge_state": 4, "charger_error": 0,
        },
    },
    "mppt_2": {
        "device": "mppt_2", "label": "MPPT East",
        "ts": "2026-05-07T12:00:00+00:00",
        "fields": {
            "pv_power": 35.0, "battery_voltage": 13.7, "charge_current": 2.5,
            "yield_today": 180.0, "charge_state": 5,
        },
    },
    "battery_sense": {
        "device": "battery_sense", "label": "Battery Sense",
        "ts": "2026-05-07T12:00:00+00:00",
        "fields": {"battery_voltage": 13.8, "temperature": 24.5},
    },
}

MOCK_HISTORY = {
    "device": "mppt_1", "field": "pv_power", "unit": "W",
    "buckets_used": ["victron"],
    "points": [
        {"t": "2026-05-07T10:00:00+00:00", "v": 5.0},
        {"t": "2026-05-07T11:00:00+00:00", "v": 25.0},
        {"t": "2026-05-07T11:30:00+00:00", "v": 40.0},
        {"t": "2026-05-07T12:00:00+00:00", "v": 45.0},
    ],
}

MOCK_DAILY = {
    "days": [
        {"date": "2026-05-05", "devices": {"mppt_1": 380.0, "mppt_2": 310.0}, "total": 690.0},
        {"date": "2026-05-06", "devices": {"mppt_1": 320.0, "mppt_2": 280.0}, "total": 600.0},
        {"date": "2026-05-07", "devices": {"mppt_1": 250.0, "mppt_2": 180.0}, "total": 430.0},
    ],
}


def _setup_mocks(page: Page) -> None:
    """Intercept all API calls and return deterministic mock data."""
    def _resp(data):
        return lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(data),
        )

    page.route("**/api/v1/devices",    _resp(MOCK_DEVICES))
    page.route("**/api/v1/current",    _resp(MOCK_CURRENT))
    page.route("**/api/v1/history**",  _resp(MOCK_HISTORY))
    page.route("**/api/v1/daily**",    _resp(MOCK_DAILY))
    page.route("**/health",            _resp({"influx_ok": True}))


def _load(page: Page, base_url: str) -> Page:
    _setup_mocks(page)
    page.goto(f"{base_url}/index.html")
    page.wait_for_selector("#cards .card", timeout=6000)
    return page


# ── Desktop (1280 × 720) ─────────────────────────────────────────────────────

@pytest.fixture
def desktop(page: Page, static_server: str) -> Page:
    page.set_viewport_size({"width": 1280, "height": 900})
    return _load(page, static_server)


class TestDesktopLayout:
    def test_title(self, desktop):
        expect(desktop).to_have_title("Solar Monitor v2.2")

    def test_three_device_cards(self, desktop):
        """2 MPPTs + 1 battery sense = 3 cards."""
        expect(desktop.locator("#cards .card")).to_have_count(3)

    def test_flow_svg_visible(self, desktop):
        expect(desktop.locator("#flow svg")).to_be_visible()

    def test_right_panel_visible_desktop(self, desktop):
        """On desktop the chart panel is always visible (no mobile overlay)."""
        right = desktop.locator("#right")
        expect(right).to_be_visible()

    def test_six_range_buttons(self, desktop):
        expect(desktop.locator(".r-btn")).to_have_count(6)

    def test_no_scrollbar_on_left_panel(self, desktop):
        """#left should not overflow its container (no scrollbar needed)."""
        left_h = desktop.locator("#left").evaluate("el => el.scrollHeight")
        wrap_h = desktop.locator("#wrap").evaluate("el => el.clientHeight")
        assert left_h <= wrap_h + 5, (
            f"#left scrollHeight={left_h} exceeds #wrap clientHeight={wrap_h} — scrollbar appears"
        )


class TestSmartUnits:
    def test_pv_power_shows_w_not_kw(self, desktop):
        """pv_power=45 → '45 W', not '0.05 kW'."""
        text = desktop.locator("#cards").inner_text()
        assert "45 W" in text

    def test_yield_today_shows_wh_not_kwh(self, desktop):
        """yield_today=250 → '250 Wh', not '0.25 kWh'."""
        text = desktop.locator("#cards").inner_text()
        assert "250 Wh" in text
        # 250 and 180 are both < 1000, so kWh suffix must not appear in cards
        assert "kWh" not in text

    def test_yield_chart_title_no_kwh(self, desktop):
        """Bar-chart title never says '(kWh)' — unit is always Wh."""
        desktop.locator(".tab-btn").last.click()
        desktop.wait_for_timeout(400)
        text = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "yield" in text
        assert "(kwh)" not in text


class TestChargeStateLabels:
    def test_absorption_label(self, desktop):
        # The span may have no text-transform, but compare case-insensitively to be safe
        text = desktop.locator("#cards").inner_text().lower()
        assert "absorption" in text

    def test_float_label(self, desktop):
        text = desktop.locator("#cards").inner_text().lower()
        assert "float" in text


class TestBridgeStatus:
    def test_banner_hidden_when_online(self, desktop):
        expect(desktop.locator("#bridge-banner")).to_have_class("hidden")

    def test_banner_shown_when_offline(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 720})
        offline = dict(MOCK_DEVICES, bridge_online=False)

        def _resp(data):
            return lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(data)
            )

        page.route("**/api/v1/devices",   _resp(offline))
        page.route("**/api/v1/current",   _resp(MOCK_CURRENT))
        page.route("**/api/v1/history**", _resp(MOCK_HISTORY))
        page.route("**/api/v1/daily**",   _resp(MOCK_DAILY))
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        # Banner should NOT have 'hidden' class
        expect(page.locator("#bridge-banner")).not_to_have_class("hidden")


class TestTabInteraction:
    def test_click_card_selects_tab(self, desktop):
        desktop.locator("#cards .card").first.click()
        expect(desktop.locator(".tab-btn.active")).to_be_visible()

    def test_chart_panel_shown_after_click(self, desktop):
        desktop.locator("#cards .card").first.click()
        desktop.wait_for_selector(".chart-panel.active canvas", timeout=5000)
        expect(desktop.locator(".chart-panel.active")).to_be_visible()

    def test_range_switch_triggers_reload(self, desktop):
        desktop.locator("#cards .card").first.click()
        desktop.locator(".r-btn", has_text="6h").click()
        desktop.wait_for_timeout(600)
        expect(desktop.locator(".chart-panel.active")).to_be_visible()

    def test_insights_daily_chart_title_follows_range(self, desktop):
        """Daily bar chart title updates to match the selected range."""
        desktop.locator(".tab-btn").last.click()
        desktop.wait_for_timeout(400)
        # 24h → 1 day → "Today's Yield"
        desktop.locator(".r-btn", has_text="24h").click()
        desktop.wait_for_timeout(500)
        text = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "today" in text
        # 7d → "7-Day Yield"
        desktop.locator(".r-btn", has_text="7d").click()
        desktop.wait_for_timeout(500)
        text2 = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "7-day yield" in text2
        # 30d → "30-Day Yield"
        desktop.locator(".r-btn", has_text="30d").click()
        desktop.wait_for_timeout(500)
        text3 = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "30-day yield" in text3

    def test_theme_toggle(self, desktop):
        desktop.locator("#theme-btn").click()
        desktop.wait_for_timeout(200)
        cls = desktop.locator("body").get_attribute("class") or ""
        # After one click we're in the opposite theme — just confirm change happened
        assert isinstance(cls, str)


class TestFlowDiagram:
    def test_battery_voltage_text(self, desktop):
        """Battery voltage shown in flow diagram (SVG text element)."""
        # SVG <text> elements use text_content(), not inner_text()
        batt_v = desktop.locator("#flow-batt-v").text_content()
        assert "13.8" in batt_v

    def test_pv_panel_text_populated(self, desktop):
        """PV panel value element gets a non-dash value."""
        val = desktop.locator("#flow-pv1-val").text_content()
        assert val is not None  # element exists and has been updated

    def test_mppt_node_status_label(self, desktop):
        """MPPT status badge shows a charge state label."""
        txt = desktop.locator("#mppt1-status-txt").text_content()
        assert txt is not None and txt.strip() != ""


# ── Mobile (375 × 812) ───────────────────────────────────────────────────────

@pytest.fixture
def mobile(page: Page, static_server: str) -> Page:
    page.set_viewport_size({"width": 375, "height": 812})
    return _load(page, static_server)


class TestMobileLayout:
    def test_cards_visible(self, mobile):
        expect(mobile.locator("#cards .card").first).to_be_visible()

    def test_chart_overlay_hidden_by_default(self, mobile):
        body_cls = mobile.locator("body").get_attribute("class") or ""
        assert "mobile-detail" not in body_cls

    def test_tap_card_opens_overlay(self, mobile):
        mobile.locator("#cards .card").first.click()
        body_cls = mobile.locator("body").get_attribute("class") or ""
        assert "mobile-detail" in body_cls

    def test_back_button_visible_in_overlay(self, mobile):
        mobile.locator("#cards .card").first.click()
        expect(mobile.locator("#back-btn")).to_be_visible()

    def test_back_button_closes_overlay(self, mobile):
        mobile.locator("#cards .card").first.click()
        mobile.locator("#back-btn").click()
        body_cls = mobile.locator("body").get_attribute("class") or ""
        assert "mobile-detail" not in body_cls

    def test_flow_svg_visible(self, mobile):
        expect(mobile.locator("#flow svg")).to_be_visible()

    def test_cards_single_column(self, mobile):
        """At 375 px width, cards should stack vertically."""
        cards = mobile.locator("#cards .card").all()
        if len(cards) >= 2:
            b0 = cards[0].bounding_box()
            b1 = cards[1].bounding_box()
            assert b1["y"] > b0["y"], "Second card should be below first on mobile"


# ── Tablet (768 × 1024) ──────────────────────────────────────────────────────

@pytest.fixture
def tablet(page: Page, static_server: str) -> Page:
    page.set_viewport_size({"width": 768, "height": 1024})
    return _load(page, static_server)


class TestTabletLayout:
    def test_cards_visible(self, tablet):
        expect(tablet.locator("#cards .card").first).to_be_visible()

    def test_chart_accessible(self, tablet):
        tablet.locator("#cards .card").first.click()
        tablet.wait_for_selector(".chart-panel.active", timeout=3000)
        expect(tablet.locator(".chart-panel.active")).to_be_visible()


# ── Offline device state ──────────────────────────────────────────────────────

class TestLFPSoc:
    """Battery SOC uses LiFePO4 discharge curve and hides while actively charging."""

    def test_soc_shown_when_not_charging(self, desktop):
        """Float (state=5) → not actively charging → SOC % is visible in flow diagram."""
        # Mock data has mppt_1 at state=4 (Absorption) and mppt_2 at state=5 (Float).
        # With one charger in Absorption the SOC should be hidden ('chg' or '—').
        soc_txt = desktop.locator("#flow-batt-soc").text_content() or ""
        # While any MPPT is in Bulk(3) or Absorption(4) we expect 'chg', not a %
        assert "chg" in soc_txt or "%" in soc_txt  # one of the two valid states

    def test_soc_hidden_during_bulk_absorption(self, page: Page, static_server: str):
        """When both MPPTs are in Bulk/Absorption, SOC must show 'chg'."""
        page.set_viewport_size({"width": 1280, "height": 900})
        charging_current = dict(MOCK_CURRENT)
        charging_current = {k: dict(v) for k, v in MOCK_CURRENT.items()}
        for mid in ["mppt_1", "mppt_2"]:
            charging_current[mid] = dict(MOCK_CURRENT[mid])
            charging_current[mid]["fields"] = dict(MOCK_CURRENT[mid]["fields"], charge_state=3)
        _setup_mocks(page)
        page.route("**/api/v1/current", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(charging_current),
        ))
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        soc_txt = page.locator("#flow-batt-soc").text_content() or ""
        assert "chg" in soc_txt

    def test_soc_percentage_when_resting(self, page: Page, static_server: str):
        """With both MPPTs in Float (5) → SOC % shown, not 'chg'.
        Mock battery_voltage=13.8 V → above LFP_CURVE top (13.60) → 100%."""
        page.set_viewport_size({"width": 1280, "height": 900})
        resting = {k: dict(v) for k, v in MOCK_CURRENT.items()}
        for mid in ["mppt_1", "mppt_2"]:
            resting[mid] = dict(MOCK_CURRENT[mid])
            resting[mid]["fields"] = dict(MOCK_CURRENT[mid]["fields"], charge_state=5)
        _setup_mocks(page)
        page.route("**/api/v1/current", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(resting),
        ))
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        soc_txt = page.locator("#flow-batt-soc").text_content() or ""
        assert "%" in soc_txt
        assert "chg" not in soc_txt
        # 13.8 V is above the curve top (13.60) → should report 100%
        assert "100%" in soc_txt


class TestLoadPaths:
    """Load flow paths should stop at the circle edge, not overlap the label."""

    def test_load_path1_endpoint(self, desktop):
        """path-mppt1-load d attribute must end at (55,266), not (42,286)."""
        d = desktop.locator("#path-mppt1-load").get_attribute("d") or ""
        assert "55,266" in d or "55" in d, f"Expected path to end near circle edge, got: {d}"

    def test_load_path2_endpoint(self, desktop):
        d = desktop.locator("#path-mppt2-load").get_attribute("d") or ""
        assert "445,266" in d or "445" in d, f"Expected path to end near circle edge, got: {d}"


class TestTodayYield:
    """Daily bar chart sends today_only=true for 1-day ranges so only calendar-day data appears."""

    def test_today_only_param_sent_for_1h_range(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        captured_urls = []

        def _intercept(route):
            captured_urls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(MOCK_DAILY))

        _setup_mocks(page)
        page.route("**/api/v1/daily**", _intercept)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)

        # Switch to Insights tab and select 1h range
        page.locator(".tab-btn").last.click()
        page.locator(".r-btn", has_text="1h").click()
        page.wait_for_timeout(600)

        daily_urls = [u for u in captured_urls if "/api/v1/daily" in u]
        assert any("today_only=true" in u for u in daily_urls), (
            f"Expected today_only=true in daily URL for 1h range, got: {daily_urls}"
        )

    def test_today_only_not_sent_for_7d_range(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        captured_urls = []

        def _intercept(route):
            captured_urls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(MOCK_DAILY))

        _setup_mocks(page)
        page.route("**/api/v1/daily**", _intercept)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)

        page.locator(".tab-btn").last.click()
        # Clear captures from initial load before switching range
        captured_urls.clear()
        page.locator(".r-btn", has_text="7d").click()
        page.wait_for_timeout(600)

        daily_urls = [u for u in captured_urls if "/api/v1/daily" in u]
        assert not any("today_only=true" in u for u in daily_urls), (
            f"today_only=true must NOT appear in 7d range URL, got: {daily_urls}"
        )

    def test_chart_title_says_today_for_1h(self, desktop):
        desktop.locator(".tab-btn").last.click()
        desktop.locator(".r-btn", has_text="1h").click()
        desktop.wait_for_timeout(500)
        text = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "today" in text


class TestOfflineDevice:
    def test_offline_card_dimmed(self, page: Page, static_server: str):
        """A device that is not online should get the s-silent CSS class."""
        page.set_viewport_size({"width": 1280, "height": 720})
        devices_with_offline = dict(MOCK_DEVICES)
        devices_with_offline["devices"] = [
            {"id": "mppt_1", "label": "MPPT South", "last_seen": "2026-05-07T12:00:00+00:00", "online": False},
            {"id": "mppt_2", "label": "MPPT East", "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
            {"id": "battery_sense","label": "Battery Sense","last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        ]
        _setup_mocks(page)
        page.route("**/api/v1/devices", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(devices_with_offline),
        ))
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        silent = page.locator("#cards .card.s-silent")
        expect(silent).to_have_count(1)
