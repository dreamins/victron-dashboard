"""InfluxDB query layer.

``InfluxRepository`` accepts an injected ``query_api`` so tests can pass a
mock without standing up InfluxDB.  Production code (main.py) passes the real
client's query_api.
"""
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config import YIELD_FIELDS, FIELD_UNITS

# ── Timing thresholds ─────────────────────────────────────────────────────────
ONLINE_S     = 90    # device is considered online if last write < this many seconds ago
BRIDGE_S     = 120   # bridge is considered online if any device wrote within this window
SEVEN_DAYS_S = 7 * 86400
ONE_YEAR_S   = 365 * 86400

# ── Duration / ID helpers ─────────────────────────────────────────────────────
_NATURAL_IV  = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800,
                3600, 7200, 14400, 21600, 43200, 86400]
_DURATION_RE = re.compile(r"^(\d+)([smhdy])$")
_UNITS_S     = {"s": 1, "m": 60, "h": 3600, "d": 86400, "y": 365 * 86400}
ID_RE        = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def auto_interval(range_s: int, max_points: int) -> str:
    """Largest natural interval that keeps response ≤ max_points points."""
    target = max(1, range_s // max_points)
    result = _NATURAL_IV[0]
    for iv in _NATURAL_IV:
        if iv <= target:
            result = iv
        else:
            break
    return f"{result}s"


def parse_duration_s(duration: str) -> int:
    """Parse a duration string like '-6h' or '30d' into seconds."""
    m = _DURATION_RE.match(duration.lstrip("-"))
    if not m:
        raise ValueError(f"invalid duration: {duration!r}")
    return int(m.group(1)) * _UNITS_S[m.group(2)]


def valid_duration(s: str) -> bool:
    return bool(_DURATION_RE.match(s))


def site_filter(site: Optional[str]) -> str:
    if not site:
        return ""
    return f'|> filter(fn: (r) => r.site == "{site}")'


# ── Repository ────────────────────────────────────────────────────────────────

class InfluxRepository:
    """All InfluxDB queries for solar-api.

    Parameters
    ----------
    query_api:
        The InfluxDB ``QueryApi`` instance.  Pass a mock in tests.
    bucket / bucket_medium / bucket_hourly:
        Bucket names for the three retention tiers.
    """

    def __init__(self, query_api, bucket: str,
                 bucket_medium: str, bucket_hourly: str):
        self.query_api      = query_api
        self._bucket        = bucket
        self._bucket_medium = bucket_medium
        self._bucket_hourly = bucket_hourly

    # ── health ────────────────────────────────────────────────────────────────

    def get_health(self) -> bool:
        try:
            self.query_api.query(
                f'from(bucket: "{self._bucket}") |> range(start: -1s) |> limit(n: 1)'
            )
            return True
        except Exception:
            return False

    # ── devices ───────────────────────────────────────────────────────────────

    def get_devices(self, site: Optional[str] = None) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        q = f"""
from(bucket: "{self._bucket}")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "solar")
  {site_filter(site)}
  |> group(columns: ["device", "label"])
  |> last()
  |> keep(columns: ["_time", "device", "label"])
"""
        tables = self.query_api.query(q)
        seen: Dict[str, Dict] = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                t   = rec.get_time()
                if dev not in seen or t > seen[dev]["last_seen"]:
                    seen[dev] = {"id": dev, "label": rec.values.get("label", dev), "last_seen": t}

        result = []
        latest = None
        for info in seen.values():
            ls = info["last_seen"]
            if latest is None or ls > latest:
                latest = ls
            age = (now - ls).total_seconds()
            result.append({
                "id":        info["id"],
                "label":     info["label"],
                "last_seen": ls.isoformat(),
                "online":    age < ONLINE_S,
            })

        bridge_online = latest is not None and (now - latest).total_seconds() < BRIDGE_S
        return {"bridge_online": bridge_online, "devices": sorted(result, key=lambda d: d["id"])}

    # ── battery snapshot ─────────────────────────────────────────────────────

    def get_battery(self, site: Optional[str] = None,
                    device: Optional[str] = None) -> Dict[str, Any]:
        dev_filter = (f'|> filter(fn: (r) => r.device == "{device}")'
                      if device and ID_RE.match(device) else "")
        q = f"""
from(bucket: "{self._bucket}")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "battery")
  {site_filter(site)}
  {dev_filter}
  |> group(columns: ["device", "label", "_field"])
  |> last()
"""
        tables = self.query_api.query(q)
        result: Dict[str, Dict] = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                if not dev:
                    continue
                if dev not in result:
                    result[dev] = {"device": dev, "label": rec.values.get("label", dev),
                                   "ts": None, "fields": {}}
                result[dev]["fields"][rec.get_field()] = rec.get_value()
                t = rec.get_time()
                if result[dev]["ts"] is None or t > result[dev]["ts"]:
                    result[dev]["ts"] = t
        for d in result.values():
            if d["ts"]:
                d["ts"] = d["ts"].isoformat()
        return result

    # ── current snapshot ─────────────────────────────────────────────────────

    def get_current(self, site: Optional[str] = None) -> Dict[str, Any]:
        q = f"""
from(bucket: "{self._bucket}")
  |> range(start: -7d)
  {site_filter(site)}
  |> group(columns: ["device", "label", "_field"])
  |> last()
"""
        tables = self.query_api.query(q)
        result: Dict[str, Dict] = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                if not dev:
                    continue
                if dev not in result:
                    result[dev] = {"device": dev, "label": rec.values.get("label", dev),
                                   "ts": None, "fields": {}}
                result[dev]["fields"][rec.get_field()] = rec.get_value()
                t = rec.get_time()
                if result[dev]["ts"] is None or t > result[dev]["ts"]:
                    result[dev]["ts"] = t
        for d in result.values():
            if d["ts"]:
                d["ts"] = d["ts"].isoformat()
        return result

    # ── history ───────────────────────────────────────────────────────────────

    def _actual_span_s(self, device: str, field: str, start_s: int,
                       site: Optional[str],
                       measurement: str = "solar") -> Optional[int]:
        """Seconds from earliest matching point to now (cheap: uses first())."""
        segs = self._stitch(start_s)
        bucket, t_start, _ = segs[0]
        q = f"""
from(bucket: "{bucket}")
  |> range(start: {t_start})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.device == "{device}" and r._field == "{field}")
  {site_filter(site)}
  |> first()
  |> keep(columns: ["_time"])
"""
        try:
            tables = self.query_api.query(q)
            for table in tables:
                for rec in table.records:
                    span = int((datetime.now(timezone.utc) - rec.get_time()).total_seconds())
                    return max(span, 1)
        except Exception:
            pass
        return None

    def get_history(self, device: str, field: str, start_s: int, interval: str,
                    site: Optional[str] = None, max_points: int = 500,
                    measurement: str = "solar") -> Dict[str, Any]:
        fn   = "max" if field in YIELD_FIELDS else "mean"
        segs = self._stitch(start_s)
        bucket_names = [s[0] for s in segs]

        if len(segs) == 1:
            bucket, t_start, t_stop = segs[0]
            q = f"""
from(bucket: "{bucket}")
  |> range(start: {t_start}, stop: {t_stop})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.device == "{device}" and r._field == "{field}")
  {site_filter(site)}
  |> aggregateWindow(every: {interval}, fn: {fn}, createEmpty: false)
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
"""
        else:
            parts = []
            for i, (bucket, t_start, t_stop) in enumerate(segs):
                parts.append(f"""t{i} = (
  from(bucket: "{bucket}")
    |> range(start: {t_start}, stop: {t_stop})
    |> filter(fn: (r) => r._measurement == "{measurement}" and r.device == "{device}" and r._field == "{field}")
    {site_filter(site)}
    |> aggregateWindow(every: {interval}, fn: {fn}, createEmpty: false)
    |> keep(columns: ["_time", "_value"])
)""")
            union_args = ", ".join(f"t{i}" for i in range(len(segs)))
            q = "\n".join(parts) + f'\nunion(tables: [{union_args}])\n  |> sort(columns: ["_time"])'

        tables     = self.query_api.query(q)
        raw_points = []
        for table in tables:
            for rec in table.records:
                raw_points.append({"t": rec.get_time().isoformat(), "v": rec.get_value()})

        seen_t: Dict[str, Dict] = {}
        for p in raw_points:
            seen_t[p["t"]] = p

        return {
            "device":       device,
            "field":        field,
            "unit":         FIELD_UNITS.get(field, ""),
            "interval":     interval,
            "buckets_used": bucket_names,
            "points":       sorted(seen_t.values(), key=lambda p: p["t"]),
        }

    # ── daily yield ──────────────────────────────────────────────────────────

    def get_daily(self, days: int, offset: timedelta, today_only: bool = False,
                  site: Optional[str] = None) -> List[Dict[str, Any]]:
        if today_only:
            now_local       = datetime.now(timezone.utc) + offset
            midnight_local  = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_utc    = midnight_local - offset
            start_str       = midnight_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            q = f"""
from(bucket: "{self._bucket}")
  |> range(start: {start_str})
  |> filter(fn: (r) => r._measurement == "solar" and r._field == "yield_today")
  {site_filter(site)}
  |> group(columns: ["device"])
  |> last()
"""
            tables   = self.query_api.query(q)
            date_str = (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%d")
            by_date: Dict[str, Dict] = {}
            for table in tables:
                for rec in table.records:
                    dev = rec.values.get("device", "")
                    if not dev:
                        continue
                    v = float(rec.get_value() or 0.0)
                    if date_str not in by_date:
                        by_date[date_str] = {"date": date_str, "devices": {}, "total": 0.0}
                    by_date[date_str]["devices"][dev] = v
                    by_date[date_str]["total"] += v
            return sorted(by_date.values(), key=lambda d: d["date"])

        bucket     = self._bucket_medium if days > 1 else self._bucket
        offset_s   = int(offset.total_seconds())
        flux_offset = f"-{abs(offset_s)}s" if offset_s >= 0 else f"{abs(offset_s)}s"
        q = f"""
from(bucket: "{bucket}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "solar" and r._field == "yield_today")
  {site_filter(site)}
  |> group(columns: ["device"])
  |> aggregateWindow(every: 1d, fn: max, offset: {flux_offset}, createEmpty: false)
  |> keep(columns: ["_time", "_value", "device"])
"""
        tables  = self.query_api.query(q)
        by_date = {}
        for table in tables:
            for rec in table.records:
                dev = rec.values.get("device", "")
                if not dev:
                    continue
                local_t  = (rec.get_time() - timedelta(seconds=1)) + offset
                date_str = local_t.strftime("%Y-%m-%d")
                v        = float(rec.get_value() or 0.0)
                if date_str not in by_date:
                    by_date[date_str] = {"date": date_str, "devices": {}, "total": 0.0}
                by_date[date_str]["devices"][dev] = v
                by_date[date_str]["total"] += v
        return sorted(by_date.values(), key=lambda d: d["date"])

    # ── internal ─────────────────────────────────────────────────────────────

    def _stitch(self, range_s: int) -> List[Tuple[str, str, str]]:
        if range_s <= SEVEN_DAYS_S:
            return [(self._bucket, f"-{range_s}s", "now()")]
        if range_s <= ONE_YEAR_S:
            return [(self._bucket_medium, f"-{range_s}s", "-1h"),
                    (self._bucket,        "-1h",           "now()")]
        return [
            (self._bucket_hourly, f"-{range_s}s", "-7d"),
            (self._bucket_medium, "-7d",           "-1h"),
            (self._bucket,        "-1h",           "now()"),
        ]
