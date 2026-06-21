"""Phase 4 API tests — run inside the solar-api container against localhost:8080."""

import httpx
import pytest

BASE = "http://localhost:8080"
PEAK_DAILY_WH = 1500.0  # matches seed_test_data.py test_mppt1 peak


def get(path, **params):
    return httpx.get(f"{BASE}{path}", params=params, timeout=30)


# ─── Health ───────────────────────────────────────────────────────────────────

def test_health():
    r = get("/health")
    assert r.status_code == 200
    assert r.json()["influx_ok"] is True


# ─── /devices ─────────────────────────────────────────────────────────────────

def test_devices_structure():
    r = get("/api/v1/devices")
    assert r.status_code == 200
    body = r.json()
    assert "bridge_online" in body
    assert isinstance(body["bridge_online"], bool)
    assert "devices" in body
    for dev in body["devices"]:
        assert "id" in dev
        assert "label" in dev
        assert "online" in dev
        assert "last_seen" in dev
        assert "type" in dev


def test_devices_include_type_from_sites_json():
    r = get("/api/v1/devices")
    by_id = {d["id"]: d for d in r.json()["devices"]}
    assert by_id["test_mppt1"]["type"] == "victron_mppt"
    assert by_id["test_battery_sense"]["type"] == "victron_battery_sense"


def test_devices_known_devices_present():
    r = get("/api/v1/devices")
    ids = {d["id"] for d in r.json()["devices"]}
    assert "test_mppt1" in ids
    assert "test_mppt2" in ids
    assert "test_battery_sense" in ids


def test_devices_bridge_offline():
    # Seed data ends 200s ago; BRIDGE threshold is 120s → must be offline
    r = get("/api/v1/devices")
    assert r.json()["bridge_online"] is False


def test_devices_all_offline():
    # Seed data ends 200s ago; ONLINE threshold is 90s → all devices offline
    r = get("/api/v1/devices")
    for dev in r.json()["devices"]:
        assert dev["online"] is False, f"{dev['id']} should be offline"


def test_devices_configured_but_unseeded_appears_offline():
    # test_ip22 is in sites_fixture.json but has no seed data.
    # It must still appear in the device list so the flow SVG can render it.
    r = get("/api/v1/devices", site="test")
    by_id = {d["id"]: d for d in r.json()["devices"]}
    assert "test_ip22" in by_id, "configured device with no InfluxDB data must appear"
    assert by_id["test_ip22"]["online"] is False
    assert by_id["test_ip22"]["type"] == "victron_ac_charger"
    assert by_id["test_ip22"]["last_seen"] is None


def test_devices_bms_type_not_injected():
    # litime_bms devices write to 'battery' measurement, not 'solar'.
    # They must NOT appear in the solar device list regardless of InfluxDB data.
    r = get("/api/v1/devices", site="test")
    ids = {d["id"] for d in r.json()["devices"]}
    assert "test_bms" not in ids


# ─── /current ─────────────────────────────────────────────────────────────────

def test_current_returns_200():
    r = get("/api/v1/current")
    assert r.status_code == 200


def test_current_structure():
    r = get("/api/v1/current")
    # Seed data ends 90s ago but /current uses -2m range, so data IS present
    body = r.json()
    for dev_data in body.values():
        assert "device" in dev_data
        assert "label" in dev_data
        assert "fields" in dev_data
        assert "ts" in dev_data


# ─── /history — bucket stitching ──────────────────────────────────────────────

def test_history_raw_only_for_short_range():
    # 6h < 7d → raw bucket only
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-6h", interval="1m")
    assert r.status_code == 200
    body = r.json()
    assert body["buckets_used"] == ["victron_test"]


def test_history_medium_plus_raw_for_long_range():
    # 30d > 7d → medium + raw (both map to victron_test in test mode)
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-30d", interval="5m")
    assert r.status_code == 200
    body = r.json()
    assert body["buckets_used"] == ["victron_test", "victron_test"]


def test_history_includes_device_and_field():
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-6h", interval="1m")
    body = r.json()
    assert body["device"] == "test_mppt1"
    assert body["field"] == "pv_power"
    assert body["unit"] == "W"
    assert isinstance(body["points"], list)


def test_history_has_data_points():
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-3d", interval="1h")
    assert r.status_code == 200
    assert len(r.json()["points"]) > 0


def test_history_points_sorted():
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-3d", interval="1h")
    times = [p["t"] for p in r.json()["points"]]
    assert times == sorted(times)


def test_history_rejects_invalid_field():
    r = get("/api/v1/history", device="test_mppt1", field="__evil__",
            start="-6h", interval="1m")
    assert r.status_code == 400


def test_history_rejects_invalid_device():
    r = get("/api/v1/history", device="test mppt1; DROP", field="pv_power",
            start="-6h", interval="1m")
    assert r.status_code == 400


# ─── /history — yield_today uses MAX not MEAN ─────────────────────────────────

def test_history_yield_today_uses_max():
    # yield_today is cumulative, goes 0 → ~1500 Wh per day.
    # With 1h interval: MAX per window ≈ end-of-window value (high).
    # MEAN per window ≈ midpoint (half the MAX). The daily peak should be
    # close to PEAK_DAILY_WH only if MAX is used.
    r = get("/api/v1/history", device="test_mppt1", field="yield_today",
            start="-3d", interval="1h")
    assert r.status_code == 200
    points = r.json()["points"]
    assert len(points) > 0

    # Group points by calendar day, find max per day
    daily: dict[str, float] = {}
    for p in points:
        date = p["t"][:10]
        v = p["v"] or 0.0
        daily[date] = max(daily.get(date, 0.0), v)

    # At least one complete day should show a peak ≥ 80% of PEAK_DAILY_WH.
    # If MEAN were used the daily max would be ~50% of PEAK_DAILY_WH.
    assert any(v >= PEAK_DAILY_WH * 0.8 for v in daily.values()), (
        f"yield_today daily max {max(daily.values()):.1f} Wh is too low "
        f"(expected ≥ {PEAK_DAILY_WH * 0.8:.0f} Wh) — MEAN may be used instead of MAX"
    )


def test_history_yield_today_monotone_within_day():
    # Within each day, yield_today should not decrease — except at the midnight reset.
    # aggregateWindow uses window-stop as the output timestamp, so the last hour of
    # day D gets timestamp day D+1 00:00 — yielding a high value followed by 0.
    # We allow that specific drop (prev_v near peak, v near 0) but flag mid-day dips.
    r = get("/api/v1/history", device="test_mppt1", field="yield_today",
            start="-3d", interval="1h")
    points = r.json()["points"]

    prev_date = None
    prev_v = None
    for p in points:
        date = p["t"][:10]
        v = p["v"] or 0.0
        if date == prev_date and prev_v is not None:
            is_midnight_reset = v < 1.0 and prev_v > PEAK_DAILY_WH * 0.5
            if not is_midnight_reset:
                assert v >= prev_v - 0.1, (
                    f"yield_today decreased mid-day {date}: {prev_v:.1f} → {v:.1f}"
                )
        if date != prev_date:
            prev_v = None
        prev_date = date
        prev_v = v


# ─── /daily ───────────────────────────────────────────────────────────────────

def test_daily_structure():
    r = get("/api/v1/daily", days=3)
    assert r.status_code == 200
    body = r.json()
    assert "days" in body
    assert len(body["days"]) > 0
    for day in body["days"]:
        assert "date" in day
        assert "devices" in day
        assert "total" in day
        assert len(day["date"]) == 10  # YYYY-MM-DD


def test_daily_has_test_devices():
    r = get("/api/v1/daily", days=3)
    all_devices = set()
    for day in r.json()["days"]:
        all_devices.update(day["devices"].keys())
    assert "test_mppt1" in all_devices
    assert "test_mppt2" in all_devices


def test_daily_tz_offset_changes_grouping():
    # A 12h timezone shift moves day boundaries by 12h; daily totals must differ.
    r0 = get("/api/v1/daily", days=3, tz_offset=0)
    r12 = get("/api/v1/daily", days=3, tz_offset=12)
    assert r0.status_code == 200
    assert r12.status_code == 200
    # Different timezone → different grouping → different response
    assert r0.json() != r12.json(), (
        "Expected different daily groupings for tz_offset=0 vs tz_offset=12"
    )


def test_daily_totals_positive():
    r = get("/api/v1/daily", days=3)
    for day in r.json()["days"]:
        assert day["total"] >= 0


def test_daily_today_only_returns_single_date():
    """today_only=true must return at most one calendar date (today)."""
    r = get("/api/v1/daily", days=1, today_only=True, tz_offset=0)
    assert r.status_code == 200
    days = r.json()["days"]
    assert len(days) <= 1, f"Expected at most 1 day, got {len(days)}: {[d['date'] for d in days]}"


def test_daily_today_only_date_is_today():
    """today_only=true result date must equal today's UTC date."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = get("/api/v1/daily", days=1, today_only=True, tz_offset=0)
    assert r.status_code == 200
    days = r.json()["days"]
    if days:
        assert days[0]["date"] == today, f"Expected date={today}, got {days[0]['date']}"


# ─── Phase 7: /sites ──────────────────────────────────────────────────────────

def test_sites_returns_200():
    r = get("/api/v1/sites")
    assert r.status_code == 200


def test_sites_structure():
    r = get("/api/v1/sites")
    body = r.json()
    assert "sites" in body
    for s in body["sites"]:
        assert "id" in s
        assert "label" in s
        assert "bridge" in s
        assert "ui" in s
        assert "device_types" in s
        # Must NOT expose secrets
        assert "mac" not in s
        assert "key" not in s


def test_sites_contains_test_site():
    r = get("/api/v1/sites")
    ids = {s["id"] for s in r.json()["sites"]}
    assert "test" in ids


def test_sites_no_mac_or_key_leaked():
    """Secrets must never appear anywhere in the /sites response."""
    body = get("/api/v1/sites").text
    assert "AA:BB:CC:DD:EE" not in body
    assert "aabbccddeeff" not in body


# ─── Phase 7: site filter on existing endpoints ───────────────────────────────

def test_devices_site_filter_known_site():
    r = get("/api/v1/devices", site="test")
    assert r.status_code == 200
    body = r.json()
    assert "devices" in body
    # Seeded data has site=test — all returned devices belong to it
    ids = {d["id"] for d in body["devices"]}
    assert "test_mppt1" in ids


def test_devices_unknown_site_returns_empty():
    r = get("/api/v1/devices", site="nonexistent_site_xyz")
    assert r.status_code == 200
    assert r.json()["devices"] == []


def test_history_site_filter():
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-3d", interval="1h", site="test")
    assert r.status_code == 200
    assert len(r.json()["points"]) > 0


def test_history_wrong_site_returns_no_points():
    r = get("/api/v1/history", device="test_mppt1", field="pv_power",
            start="-3d", interval="1h", site="nonexistent_site_xyz")
    assert r.status_code == 200
    assert r.json()["points"] == []


def test_daily_site_filter():
    r = get("/api/v1/daily", days=3, site="test")
    assert r.status_code == 200
    assert len(r.json()["days"]) > 0


def test_daily_wrong_site_returns_empty():
    r = get("/api/v1/daily", days=3, site="nonexistent_site_xyz")
    assert r.status_code == 200
    assert r.json()["days"] == []


def test_current_site_filter():
    r = get("/api/v1/current", site="test")
    assert r.status_code == 200
    assert "test_mppt1" in r.json()


# ─── Phase 9: /battery ────────────────────────────────────────────────────────

def test_battery_returns_200():
    r = get("/api/v1/battery")
    assert r.status_code == 200


def test_battery_structure():
    r = get("/api/v1/battery")
    for dev_data in r.json().values():
        assert "device" in dev_data
        assert "label" in dev_data
        assert "fields" in dev_data
        assert "ts" in dev_data


def test_battery_has_soc_field():
    r = get("/api/v1/battery", device="test_bms")
    body = r.json()
    assert "test_bms" in body, "test_bms device not found in battery response"
    assert "soc" in body["test_bms"]["fields"]
    assert 0 <= body["test_bms"]["fields"]["soc"] <= 100


def test_battery_site_filter():
    r = get("/api/v1/battery", site="test")
    assert r.status_code == 200
    assert "test_bms" in r.json()


def test_battery_unknown_site_returns_empty():
    r = get("/api/v1/battery", site="nonexistent_xyz")
    assert r.status_code == 200
    assert r.json() == {}


# ─── Phase 10: multi-site isolation ──────────────────────────────────────────

def test_sites_contains_both_sites():
    r = get("/api/v1/sites")
    ids = {s["id"] for s in r.json()["sites"]}
    assert "test" in ids
    assert "test_garage" in ids


def test_sites_garage_ui_metadata():
    r = get("/api/v1/sites")
    garage = next(s for s in r.json()["sites"] if s["id"] == "test_garage")
    assert garage["bridge"] == "ble"
    assert garage["ui"]["show_loads"] is False
    assert garage["ui"]["battery_display"] == "bms"
    assert garage["ui"]["mppt_count"] == 2


def test_sites_test_ui_metadata():
    r = get("/api/v1/sites")
    test_site = next(s for s in r.json()["sites"] if s["id"] == "test")
    assert test_site["bridge"] == "esp32"
    assert test_site["ui"]["show_loads"] is True
    assert test_site["ui"]["battery_display"] == "sense"


def test_devices_garage_returns_garage_devices():
    r = get("/api/v1/devices", site="test_garage")
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()["devices"]}
    assert "test_garage_mppt1" in ids
    assert "test_garage_mppt2" in ids


def test_devices_site_isolation_test_excludes_garage():
    r = get("/api/v1/devices", site="test")
    ids = {d["id"] for d in r.json()["devices"]}
    assert "test_garage_mppt1" not in ids
    assert "test_garage_mppt2" not in ids


def test_devices_site_isolation_garage_excludes_test():
    r = get("/api/v1/devices", site="test_garage")
    ids = {d["id"] for d in r.json()["devices"]}
    assert "test_mppt1" not in ids
    assert "test_battery_sense" not in ids


def test_history_garage_has_data():
    r = get("/api/v1/history", device="test_garage_mppt1", field="pv_power",
            start="-3d", interval="1h", site="test_garage")
    assert r.status_code == 200
    assert len(r.json()["points"]) > 0


def test_history_site_isolation_garage_device_wrong_site():
    # Garage device returns no points when queried under the test site filter.
    r = get("/api/v1/history", device="test_garage_mppt1", field="pv_power",
            start="-3d", interval="1h", site="test")
    assert r.status_code == 200
    assert r.json()["points"] == []


def test_battery_garage_site_returns_garage_bms():
    r = get("/api/v1/battery", site="test_garage")
    assert r.status_code == 200
    body = r.json()
    assert "test_garage_bms" in body
    assert "soc" in body["test_garage_bms"]["fields"]
    assert 0 <= body["test_garage_bms"]["fields"]["soc"] <= 100


def test_battery_isolation_test_excludes_garage_bms():
    r = get("/api/v1/battery", site="test")
    assert "test_garage_bms" not in r.json()


def test_battery_isolation_garage_excludes_test_bms():
    r = get("/api/v1/battery", site="test_garage")
    assert "test_bms" not in r.json()
