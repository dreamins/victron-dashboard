"""Unit tests for api/repository.py — query_api injected as a mock, no InfluxDB."""
import pathlib
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from repository import (
    InfluxRepository, ID_RE, auto_interval, parse_duration_s, valid_duration,
    ONLINE_S, BRIDGE_S, SEVEN_DAYS_S, ONE_YEAR_S,
)


# ── Mock helpers ──────────────────────────────────────────────────────────────

class _Rec:
    """Minimal FluxRecord stand-in."""
    def __init__(self, time, value=None, field=None, device=None, label=None):
        self._time  = time
        self._value = value
        self._field = field
        self.values = {"device": device, "label": label, "_field": field}

    def get_time(self):  return self._time
    def get_value(self): return self._value
    def get_field(self): return self._field


class _Table:
    def __init__(self, *records):
        self.records = list(records)


def _make_repo(tables=None):
    qa = MagicMock()
    qa.query.return_value = tables or []
    return InfluxRepository(qa, "victron", "victron_medium", "victron_hourly"), qa


# ── auto_interval ─────────────────────────────────────────────────────────────

def test_auto_interval_6h_500pts():
    # 6h = 21600s  target = 21600/500 = 43  → largest natural IV ≤ 43 is 30
    assert auto_interval(21_600, 500) == "30s"


def test_auto_interval_30d_500pts():
    # 30d = 2592000s  target = 2592000/500 = 5184  → 3600
    assert auto_interval(2_592_000, 500) == "3600s"


def test_auto_interval_minimum_is_1s():
    assert auto_interval(1, 500) == "1s"


def test_auto_interval_respects_max_points():
    # With very few points requested, interval should be large
    result_small = auto_interval(86400, 10)   # 1d, 10 pts → target 8640s → 7200
    result_large = auto_interval(86400, 500)  # 1d, 500 pts → target 172s → 120
    assert result_small != result_large


# ── parse_duration_s ──────────────────────────────────────────────────────────

def test_parse_duration_s_seconds():
    assert parse_duration_s("300s") == 300


def test_parse_duration_s_minutes():
    assert parse_duration_s("10m") == 600


def test_parse_duration_s_hours():
    assert parse_duration_s("6h") == 6 * 3600


def test_parse_duration_s_days():
    assert parse_duration_s("30d") == 30 * 86400


def test_parse_duration_s_negative_prefix_stripped():
    assert parse_duration_s("-6h") == 6 * 3600


def test_parse_duration_s_invalid_raises():
    with pytest.raises(ValueError):
        parse_duration_s("abc")


def test_parse_duration_s_bad_unit_raises():
    with pytest.raises(ValueError):
        parse_duration_s("5z")


# ── valid_duration ────────────────────────────────────────────────────────────

def test_valid_duration_accepts_valid_strings():
    assert valid_duration("6h") is True
    assert valid_duration("30d") is True
    assert valid_duration("300s") is True
    assert valid_duration("5m") is True


def test_valid_duration_rejects_invalid():
    assert valid_duration("abc") is False
    assert valid_duration("") is False
    assert valid_duration("-6h") is False   # leading dash not in the pattern


# ── ID_RE ─────────────────────────────────────────────────────────────────────

def test_id_re_accepts_valid_ids():
    assert ID_RE.match("mppt1")
    assert ID_RE.match("garage_bms")
    assert ID_RE.match("device-01")


def test_id_re_rejects_injection_chars():
    assert not ID_RE.match("dev;rm")
    assert not ID_RE.match('dev"x')
    assert not ID_RE.match("dev id")


# ── _stitch ───────────────────────────────────────────────────────────────────

def test_stitch_short_range_single_bucket():
    repo, _ = _make_repo()
    segs = repo._stitch(3600)  # 1h < SEVEN_DAYS_S
    assert len(segs) == 1
    assert segs[0][0] == "victron"


def test_stitch_medium_range_two_buckets():
    repo, _ = _make_repo()
    segs = repo._stitch(SEVEN_DAYS_S + 1)
    assert len(segs) == 2
    assert segs[0][0] == "victron_medium"
    assert segs[1][0] == "victron"


def test_stitch_long_range_three_buckets():
    repo, _ = _make_repo()
    segs = repo._stitch(ONE_YEAR_S + 1)
    assert len(segs) == 3
    assert segs[0][0] == "victron_hourly"
    assert segs[1][0] == "victron_medium"
    assert segs[2][0] == "victron"


def test_stitch_boundary_exactly_seven_days():
    repo, _ = _make_repo()
    segs = repo._stitch(SEVEN_DAYS_S)
    assert len(segs) == 1  # ≤ threshold → single bucket


# ── get_devices ───────────────────────────────────────────────────────────────

def test_get_devices_marks_recent_device_online():
    now = datetime.now(timezone.utc)
    t   = now - timedelta(seconds=ONLINE_S - 10)
    repo, _ = _make_repo([_Table(_Rec(t, device="mppt1", label="MPPT 1"))])
    devs = {d["id"]: d for d in repo.get_devices()["devices"]}
    assert devs["mppt1"]["online"] is True


def test_get_devices_marks_stale_device_offline():
    now = datetime.now(timezone.utc)
    t   = now - timedelta(seconds=ONLINE_S + 60)
    repo, _ = _make_repo([_Table(_Rec(t, device="mppt1", label="MPPT 1"))])
    devs = {d["id"]: d for d in repo.get_devices()["devices"]}
    assert devs["mppt1"]["online"] is False


def test_get_devices_bridge_online_when_recent():
    now = datetime.now(timezone.utc)
    t   = now - timedelta(seconds=BRIDGE_S - 10)
    repo, _ = _make_repo([_Table(_Rec(t, device="d1", label="D1"))])
    assert repo.get_devices()["bridge_online"] is True


def test_get_devices_bridge_offline_when_stale():
    now = datetime.now(timezone.utc)
    t   = now - timedelta(seconds=BRIDGE_S + 30)
    repo, _ = _make_repo([_Table(_Rec(t, device="d1", label="D1"))])
    assert repo.get_devices()["bridge_online"] is False


def test_get_devices_empty_tables_bridge_offline():
    repo, _ = _make_repo([])
    result = repo.get_devices()
    assert result["bridge_online"] is False
    assert result["devices"] == []


def test_get_devices_deduplicates_by_id():
    now = datetime.now(timezone.utc)
    t1  = now - timedelta(seconds=10)
    t2  = now - timedelta(seconds=5)
    # Two records for same device — keep the later one
    repo, _ = _make_repo([_Table(
        _Rec(t1, device="mppt1", label="old"),
        _Rec(t2, device="mppt1", label="new"),
    )])
    devs = repo.get_devices()["devices"]
    assert len([d for d in devs if d["id"] == "mppt1"]) == 1


# ── get_health ────────────────────────────────────────────────────────────────

def test_get_health_returns_true_when_query_succeeds():
    repo, _ = _make_repo([])
    assert repo.get_health() is True


def test_get_health_returns_false_when_query_raises():
    qa = MagicMock()
    qa.query.side_effect = Exception("connection refused")
    repo = InfluxRepository(qa, "b", "bm", "bh")
    assert repo.get_health() is False


# ── get_history — measurement routing ────────────────────────────────────────

def test_get_history_passes_battery_measurement_to_query():
    repo, qa = _make_repo([])
    repo.get_history("bms1", "soc", 3600, "1m", measurement="battery")
    q = qa.query.call_args[0][0]
    assert '"battery"' in q
    assert '"bms1"' in q
    assert '"soc"' in q


def test_get_history_default_measurement_is_solar():
    repo, qa = _make_repo([])
    repo.get_history("mppt1", "pv_power", 3600, "1m")
    q = qa.query.call_args[0][0]
    assert '"solar"' in q


def test_get_history_uses_max_for_yield_fields():
    repo, qa = _make_repo([])
    repo.get_history("d1", "yield_today", 3600, "1m")
    assert "fn: max" in qa.query.call_args[0][0]


def test_get_history_uses_mean_for_power_fields():
    repo, qa = _make_repo([])
    repo.get_history("d1", "pv_power", 3600, "1m")
    assert "fn: mean" in qa.query.call_args[0][0]


def test_get_history_passes_site_filter_when_given():
    repo, qa = _make_repo([])
    repo.get_history("d1", "pv_power", 3600, "1m", site="garage")
    q = qa.query.call_args[0][0]
    assert '"garage"' in q


def test_get_history_two_bucket_query_uses_union(monkeypatch):
    repo, qa = _make_repo([])
    # Force 2-bucket path: range_s just over SEVEN_DAYS_S
    repo.get_history("d1", "pv_power", SEVEN_DAYS_S + 1, "1h")
    q = qa.query.call_args[0][0]
    assert "union" in q


def test_get_history_returns_deduped_sorted_points():
    now = datetime.now(timezone.utc)
    t1  = now - timedelta(minutes=2)
    t2  = now - timedelta(minutes=1)
    repo, _ = _make_repo([_Table(
        _Rec(t2, value=100.0),
        _Rec(t1, value=50.0),
        _Rec(t2, value=100.0),   # duplicate timestamp
    )])
    result = repo.get_history("d1", "pv_power", 3600, "1m")
    times = [p["t"] for p in result["points"]]
    assert times == sorted(times)
    assert len(times) == len(set(times))  # deduped


# ── get_battery ───────────────────────────────────────────────────────────────

def test_get_battery_groups_fields_by_device():
    now = datetime.now(timezone.utc)
    t   = now - timedelta(seconds=10)
    rec_soc = _Rec(t, value=85.0,  field="soc",             device="bms1", label="BMS")
    rec_v   = _Rec(t, value=13.2,  field="battery_voltage", device="bms1", label="BMS")
    repo, _ = _make_repo([_Table(rec_soc, rec_v)])
    result = repo.get_battery()
    assert "bms1" in result
    assert result["bms1"]["fields"]["soc"] == 85.0
    assert result["bms1"]["fields"]["battery_voltage"] == 13.2
