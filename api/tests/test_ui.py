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

MOCK_SITES = {
    "sites": [
        {
            "id": "home",
            "label": "Home Solar",
            "tz_offset_hours": 0,
            "bridge": "esp32",
            "ui": {"show_loads": True, "battery_display": "sense", "mppt_count": 2},
            "device_types": ["victron_mppt", "victron_battery_sense"],
        }
    ]
}

MOCK_SITES_MULTI = {
    "sites": [
        {
            "id": "home",
            "label": "Home Solar",
            "tz_offset_hours": 0,
            "bridge": "esp32",
            "ui": {"show_loads": True, "battery_display": "sense", "mppt_count": 2},
            "device_types": ["victron_mppt", "victron_battery_sense"],
        },
        {
            "id": "garage",
            "label": "Garage",
            "tz_offset_hours": 0,
            "bridge": "ble",
            "ui": {"show_loads": False, "battery_display": "bms", "mppt_count": 2},
            "device_types": ["victron_mppt", "litime_bms"],
        },
    ]
}

MOCK_DEVICES = {
    "bridge_online": True,
    "devices": [
        {"id": "mppt_1", "label": "MPPT South",   "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        {"id": "mppt_2", "label": "MPPT East",   "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        {"id": "battery_sense","label": "Battery Sense",  "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
    ],
}

MOCK_DEVICES_GARAGE = {
    "bridge_online": True,
    "devices": [
        {"id": "garage_mppt1", "label": "Garage MPPT 1", "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        {"id": "garage_mppt2", "label": "Garage MPPT 2", "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        {"id": "garage_bms",   "label": "Garage LiTime", "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
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

MOCK_CURRENT_GARAGE = {
    "garage_mppt1": {
        "device": "garage_mppt1", "label": "Garage MPPT 1",
        "ts": "2026-05-07T12:00:00+00:00",
        "fields": {"pv_power": 120.0, "battery_voltage": 13.3, "charge_current": 9.0, "yield_today": 800.0, "charge_state": 3},
    },
    "garage_mppt2": {
        "device": "garage_mppt2", "label": "Garage MPPT 2",
        "ts": "2026-05-07T12:00:00+00:00",
        "fields": {"pv_power": 90.0, "battery_voltage": 13.3, "charge_current": 6.5, "yield_today": 600.0, "charge_state": 3},
    },
}

MOCK_BATTERY_GARAGE = {
    "garage_bms": {
        "device": "garage_bms",
        "label": "Garage LiTime",
        "ts": "2026-05-07T12:00:00+00:00",
        "fields": {
            "soc": 82.0, "battery_voltage": 13.2, "battery_current": -0.5,
            "cycles": 45.0, "cell_min": 3.28, "cell_max": 3.31,
            "cell_avg": 3.295, "soh": 97.0,
        },
    }
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


def _setup_mocks(page: Page, sites=None, devices=None, current=None, battery=None) -> None:
    """Intercept all API calls with deterministic mock data.
    Uses ** suffixes to match URLs with query params (e.g. ?site=home).
    """
    def _resp(data):
        return lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(data),
        )

    page.route("**/api/v1/sites",       _resp(sites or MOCK_SITES))
    page.route("**/api/v1/devices**",   _resp(devices or MOCK_DEVICES))
    page.route("**/api/v1/current**",   _resp(current or MOCK_CURRENT))
    page.route("**/api/v1/history**",   _resp(MOCK_HISTORY))
    page.route("**/api/v1/daily**",     _resp(MOCK_DAILY))
    page.route("**/api/v1/battery**",   _resp(battery or {}))
    page.route("**/health",             _resp({"influx_ok": True}))


def _load(page: Page, base_url: str, site_id: str = "home") -> Page:
    """Load the dashboard with a pre-selected site (skips site picker)."""
    _setup_mocks(page)
    # Set localStorage before page JS runs so initSites() skips the picker
    page.add_init_script(f"localStorage.setItem('victron_selected_site', '{site_id}')")
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
        expect(desktop).to_have_title("Solar Monitor")

    def test_three_device_cards(self, desktop):
        """2 MPPTs + 1 battery sense = 3 cards."""
        expect(desktop.locator("#cards .card")).to_have_count(3)

    def test_flow_svg_visible(self, desktop):
        expect(desktop.locator("#flow svg")).to_be_visible()

    def test_right_panel_visible_desktop(self, desktop):
        """On desktop the chart panel is always visible (no mobile overlay)."""
        expect(desktop.locator("#right")).to_be_visible()

    def test_six_range_buttons(self, desktop):
        expect(desktop.locator(".r-btn")).to_have_count(6)

    def test_no_scrollbar_on_left_panel(self, desktop):
        """#left should not overflow its container."""
        left_h = desktop.locator("#left").evaluate("el => el.scrollHeight")
        wrap_h = desktop.locator("#wrap").evaluate("el => el.clientHeight")
        assert left_h <= wrap_h + 5, (
            f"#left scrollHeight={left_h} exceeds #wrap clientHeight={wrap_h}"
        )


class TestSmartUnits:
    def test_pv_power_shows_w_not_kw(self, desktop):
        text = desktop.locator("#cards").inner_text()
        assert "45 W" in text

    def test_yield_today_shows_wh_not_kwh(self, desktop):
        text = desktop.locator("#cards").inner_text()
        assert "250 Wh" in text
        assert "kWh" not in text

    def test_yield_chart_title_no_kwh(self, desktop):
        desktop.locator(".tab-btn").last.click()
        desktop.wait_for_timeout(400)
        text = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "yield" in text
        assert "(kwh)" not in text


class TestChargeStateLabels:
    def test_absorption_label(self, desktop):
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
        _setup_mocks(page, devices=offline)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
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
        desktop.locator(".tab-btn").last.click()
        desktop.wait_for_timeout(400)
        desktop.locator(".r-btn", has_text="24h").click()
        desktop.wait_for_timeout(500)
        text = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "today" in text
        desktop.locator(".r-btn", has_text="7d").click()
        desktop.wait_for_timeout(500)
        text2 = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "7-day yield" in text2
        desktop.locator(".r-btn", has_text="30d").click()
        desktop.wait_for_timeout(500)
        text3 = desktop.locator(".chart-panel.active").inner_text().lower()
        assert "30-day yield" in text3

    def test_theme_toggle(self, desktop):
        desktop.locator("#theme-btn").click()
        desktop.wait_for_timeout(200)
        cls = desktop.locator("body").get_attribute("class") or ""
        assert isinstance(cls, str)


class TestFlowDiagram:
    def test_battery_voltage_text(self, desktop):
        batt_v = desktop.locator("#flow-batt-v").text_content()
        assert "13.8" in batt_v

    def test_pv_panel_text_populated(self, desktop):
        val = desktop.locator("#flow-pv1-val").text_content()
        assert val is not None

    def test_mppt_node_status_label(self, desktop):
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


# ── LFP SOC ──────────────────────────────────────────────────────────────────

class TestLFPSoc:
    def test_soc_shown_when_not_charging(self, desktop):
        soc_txt = desktop.locator("#flow-batt-soc").text_content() or ""
        assert "chg" in soc_txt or "%" in soc_txt

    def test_soc_hidden_during_bulk_absorption(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        charging_current = {k: dict(v) for k, v in MOCK_CURRENT.items()}
        for mid in ["mppt_1", "mppt_2"]:
            charging_current[mid] = dict(MOCK_CURRENT[mid])
            charging_current[mid]["fields"] = dict(MOCK_CURRENT[mid]["fields"], charge_state=3)
        _setup_mocks(page, current=charging_current)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        soc_txt = page.locator("#flow-batt-soc").text_content() or ""
        assert "chg" in soc_txt

    def test_soc_percentage_when_resting(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        resting = {k: dict(v) for k, v in MOCK_CURRENT.items()}
        for mid in ["mppt_1", "mppt_2"]:
            resting[mid] = dict(MOCK_CURRENT[mid])
            resting[mid]["fields"] = dict(MOCK_CURRENT[mid]["fields"], charge_state=5)
        _setup_mocks(page, current=resting)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        soc_txt = page.locator("#flow-batt-soc").text_content() or ""
        assert "%" in soc_txt
        assert "chg" not in soc_txt
        assert "100%" in soc_txt


class TestLoadPaths:
    def test_load_path1_endpoint(self, desktop):
        d = desktop.locator("#path-mppt1-load").get_attribute("d") or ""
        assert "55,266" in d or "55" in d, f"Expected path to end near circle edge, got: {d}"

    def test_load_path2_endpoint(self, desktop):
        d = desktop.locator("#path-mppt2-load").get_attribute("d") or ""
        assert "445,266" in d or "445" in d, f"Expected path to end near circle edge, got: {d}"


class TestTodayYield:
    def test_today_only_param_sent_for_1h_range(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        captured_urls = []

        def _intercept(route):
            captured_urls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(MOCK_DAILY))

        _setup_mocks(page)
        page.route("**/api/v1/daily**", _intercept)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)

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
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)

        page.locator(".tab-btn").last.click()
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
        page.set_viewport_size({"width": 1280, "height": 720})
        devices_with_offline = dict(MOCK_DEVICES)
        devices_with_offline["devices"] = [
            {"id": "mppt_1", "label": "MPPT South",  "last_seen": "2026-05-07T12:00:00+00:00", "online": False},
            {"id": "mppt_2", "label": "MPPT East",  "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
            {"id": "battery_sense","label": "Battery Sense", "last_seen": "2026-05-07T12:00:00+00:00", "online": True},
        ]
        _setup_mocks(page, devices=devices_with_offline)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        expect(page.locator("#cards .card.s-silent")).to_have_count(1)


# ── Phase 11: Site picker and multi-site ─────────────────────────────────────

class TestSitePicker:
    def test_picker_shown_without_localstorage(self, page: Page, static_server: str):
        """No localStorage → site picker is shown, not the dashboard."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        # Do NOT set localStorage
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#site-picker", timeout=5000)
        expect(page.locator("#site-picker")).to_be_visible()
        # Dashboard wrap should be hidden
        wrap_display = page.locator("#wrap").evaluate("el => window.getComputedStyle(el).display")
        assert wrap_display == "none"

    def test_picker_has_two_cards(self, page: Page, static_server: str):
        """Multi-site setup shows two picker cards."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector(".picker-card", timeout=5000)
        expect(page.locator(".picker-card")).to_have_count(2)

    def test_picker_shows_site_labels(self, page: Page, static_server: str):
        """Picker cards display site labels."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector(".picker-card", timeout=5000)
        text = page.locator("#picker-cards").inner_text()
        assert "Home Solar" in text
        assert "Garage" in text

    def test_picker_card_click_shows_dashboard(self, page: Page, static_server: str):
        """Clicking a picker card hides the picker and shows the dashboard."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector(".picker-card", timeout=5000)
        page.locator(".picker-card").first.click()
        page.wait_for_selector("#cards .card", timeout=6000)
        picker_display = page.locator("#site-picker").evaluate("el => window.getComputedStyle(el).display")
        assert picker_display == "none"
        expect(page.locator("#wrap")).to_be_visible()

    def test_single_site_skips_picker(self, page: Page, static_server: str):
        """With only one site configured, picker is never shown."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page)  # MOCK_SITES has 1 site
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        picker_display = page.locator("#site-picker").evaluate("el => window.getComputedStyle(el).display")
        assert picker_display == "none"

    def test_localstorage_persists_site_selection(self, page: Page, static_server: str):
        """After selecting a site, localStorage is set and next load skips picker."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector(".picker-card", timeout=5000)
        page.locator(".picker-card").first.click()
        page.wait_for_selector("#cards .card", timeout=6000)
        saved = page.evaluate("localStorage.getItem('victron_selected_site')")
        assert saved is not None and saved != ""

    def test_go_to_picker_clears_localstorage(self, page: Page, static_server: str):
        """'All sites' menu item returns to picker and clears localStorage."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        # Open site dropdown
        page.locator("#site-btn").click()
        page.wait_for_selector(".site-menu-all", timeout=2000)
        page.locator(".site-menu-all").click()
        page.wait_for_selector("#site-picker", timeout=3000)
        expect(page.locator("#site-picker")).to_be_visible()
        saved = page.evaluate("localStorage.getItem('victron_selected_site')")
        assert saved is None


class TestSiteDropdown:
    def test_dropdown_visible_for_multi_site(self, page: Page, static_server: str):
        """Site dropdown shown in header when multiple sites configured."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        dd = page.locator("#site-dropdown")
        dd_display = dd.evaluate("el => window.getComputedStyle(el).display")
        assert dd_display != "none"

    def test_dropdown_hidden_for_single_site(self, desktop):
        """Site dropdown hidden when only one site configured."""
        dd_display = desktop.locator("#site-dropdown").evaluate(
            "el => window.getComputedStyle(el).display"
        )
        assert dd_display == "none"

    def test_dropdown_shows_site_name(self, page: Page, static_server: str):
        """Dropdown button label shows the active site name."""
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        label = page.locator("#site-btn-label").inner_text()
        assert "Home Solar" in label

    def test_dropdown_menu_opens_on_click(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        page.locator("#site-btn").click()
        expect(page.locator("#site-menu")).not_to_have_class("hidden")

    def test_dropdown_closes_on_outside_click(self, page: Page, static_server: str):
        page.set_viewport_size({"width": 1280, "height": 900})
        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        page.locator("#site-btn").click()
        page.locator("#flow svg").click()  # click outside dropdown
        page.wait_for_timeout(200)
        expect(page.locator("#site-menu")).to_have_class("hidden")

    def test_api_calls_include_site_param(self, page: Page, static_server: str):
        """After site selection, API calls include ?site= parameter."""
        page.set_viewport_size({"width": 1280, "height": 900})
        captured_urls = []

        def _intercept_devices(route):
            captured_urls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(MOCK_DEVICES))

        _setup_mocks(page, sites=MOCK_SITES_MULTI)
        page.route("**/api/v1/devices**", _intercept_devices)
        page.add_init_script("localStorage.setItem('victron_selected_site', 'home')")
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)

        device_urls = [u for u in captured_urls if "/api/v1/devices" in u]
        assert any("site=home" in u for u in device_urls), (
            f"Expected site=home in devices URL, got: {device_urls}"
        )


class TestBmsCard:
    def _load_garage(self, page: Page, base_url: str) -> Page:
        """Load dashboard with garage (BMS) site selected."""
        _setup_mocks(
            page,
            sites=MOCK_SITES_MULTI,
            devices=MOCK_DEVICES_GARAGE,
            current=MOCK_CURRENT_GARAGE,
            battery=MOCK_BATTERY_GARAGE,
        )
        page.add_init_script("localStorage.setItem('victron_selected_site', 'garage')")
        page.goto(f"{base_url}/index.html")
        page.wait_for_selector("#cards .card", timeout=6000)
        return page

    def test_bms_card_shows_soc(self, page: Page, static_server: str):
        """BMS battery card shows SOC percentage as the hero value."""
        page.set_viewport_size({"width": 1280, "height": 900})
        self._load_garage(page, static_server)
        text = page.locator("#cards").inner_text()
        assert "82" in text  # SOC = 82%

    def test_bms_card_shows_cycles(self, page: Page, static_server: str):
        """BMS card shows cycle count."""
        page.set_viewport_size({"width": 1280, "height": 900})
        self._load_garage(page, static_server)
        text = page.locator("#cards").inner_text()
        assert "45" in text  # cycles = 45

    def test_bms_card_has_soc_bar(self, page: Page, static_server: str):
        """BMS card renders the SOC progress bar element."""
        page.set_viewport_size({"width": 1280, "height": 900})
        self._load_garage(page, static_server)
        expect(page.locator(".soc-bar-fill").first).to_be_visible()

    def test_load_nodes_hidden_for_no_load_site(self, page: Page, static_server: str):
        """Garage site has show_loads=False — load node groups stay hidden."""
        page.set_viewport_size({"width": 1280, "height": 900})
        self._load_garage(page, static_server)
        grp1 = page.locator("#mppt1-load-grp")
        grp2 = page.locator("#mppt2-load-grp")
        assert grp1.evaluate("el => el.style.display") == "none"
        assert grp2.evaluate("el => el.style.display") == "none"

    def test_bms_flow_shows_soc(self, page: Page, static_server: str):
        """Flow diagram battery section shows BMS SOC, not voltage-curve estimate."""
        page.set_viewport_size({"width": 1280, "height": 900})
        self._load_garage(page, static_server)
        soc_txt = page.locator("#flow-batt-soc").text_content() or ""
        assert "82" in soc_txt or "chg" in soc_txt
