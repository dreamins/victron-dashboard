#!/usr/bin/env python3
"""
migrate_site_tags.py — Backfill site=<SITE> tag onto every existing InfluxDB record.

InfluxDB has no UPDATE. Strategy per bucket:
  1. Create a temp bucket (same retention)
  2. Per device: query original → write to temp with site tag added
  3. Verify temp count >= 99% of original (guard against races / write failures)
  4. Delete all original records (measurement="solar", all time)
  5. Per device: query temp → write back to original
  6. Verify final count >= 99% of original
  7. Delete temp bucket

The temp bucket is a checkpoint: if this script is re-run after a crash it
detects the temp bucket and resumes from step 3.

Run inside the solar-api container (has influxdb-client):
  docker compose exec solar-api python3 /tmp/migrate_site_tags.py
"""
import logging
import os
import sys
import time
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, BucketRetentionRules, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

INFLUX_URL   = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"]
INFLUX_ORG   = os.environ.get("INFLUX_ORG", "home")
SITE         = os.environ.get("MIGRATION_SITE", "home")
MEASUREMENT  = "solar"
BATCH_SIZE   = 10_000

# (bucket_name, retention_seconds)
BUCKETS = [
    ("victron",         720 * 3600),
    ("victron_medium",  8760 * 3600),
    ("victron_hourly",  87600 * 3600),
]

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
FAR   = datetime(2099, 1, 1, tzinfo=timezone.utc)


def count_records(qapi, bucket: str) -> int:
    q = f'from(bucket: "{bucket}") |> range(start: 0) |> group() |> count()'
    tables = qapi.query(q)
    for t in tables:
        for r in t.records:
            return int(r.get_value())
    return 0


def get_devices(qapi, bucket: str) -> list:
    q = f'''
from(bucket: "{bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}")
  |> keep(columns: ["device"])
  |> distinct(column: "device")
'''
    tables = qapi.query(q)
    return [r.get_value() for t in tables for r in t.records]


def copy_device(qapi, write_api, src: str, dst: str, device: str, add_site: bool) -> int:
    """Copy all records for one device from src to dst.
    If add_site is True, injects site=SITE tag (src→temp phase).
    If add_site is False, tags already present (temp→original phase).
    Returns number of points written."""
    q = f'''
from(bucket: "{src}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r.device == "{device}")
'''
    tables = qapi.query(q)

    batch: list[Point] = []
    total = 0

    def flush():
        nonlocal total
        if not batch:
            return
        write_api.write(bucket=dst, record=batch)
        total += len(batch)
        batch.clear()

    for table in tables:
        for rec in table.records:
            p = Point(rec.get_measurement())
            for k, v in rec.values.items():
                if k.startswith("_") or k in ("result", "table"):
                    continue
                p = p.tag(k, str(v))
            if add_site:
                p = p.tag("site", SITE)
            p = p.field(rec.get_field(), rec.get_value())
            p = p.time(rec.get_time())
            batch.append(p)
            if len(batch) >= BATCH_SIZE:
                flush()
                log.info("    [%s] %d points written so far...", device, total)

    flush()
    return total


def ensure_temp_bucket(buckets_api, temp_name: str, retention_s: int) -> bool:
    """Create temp bucket if it doesn't exist. Returns True if it already existed."""
    existing = {b.name for b in buckets_api.find_buckets().buckets}
    if temp_name in existing:
        log.info("  Temp bucket %s already exists — resuming from verification step", temp_name)
        return True
    buckets_api.create_bucket(
        bucket_name=temp_name,
        retention_rules=BucketRetentionRules(type="expire", every_seconds=retention_s),
        org=INFLUX_ORG,
    )
    log.info("  Created temp bucket %s", temp_name)
    return False


def delete_temp_bucket(buckets_api, temp_name: str):
    buckets = buckets_api.find_buckets().buckets
    match = next((b for b in buckets if b.name == temp_name), None)
    if match:
        buckets_api.delete_bucket(match)
        log.info("  Deleted temp bucket %s", temp_name)


def migrate_bucket(client, bucket: str, retention_s: int):
    qapi     = client.query_api()
    wapi     = client.write_api(write_options=SYNCHRONOUS)
    dapi     = client.delete_api()
    bapi     = client.buckets_api()
    temp     = f"{bucket}_migration_temp"

    log.info("── Bucket: %s ──────────────────────────────────", bucket)

    original_count = count_records(qapi, bucket)
    log.info("  Original record count: %d", original_count)
    if original_count == 0:
        log.info("  Empty bucket — skipping")
        return

    devices = get_devices(qapi, bucket)
    log.info("  Devices found: %s", devices)

    already_existed = ensure_temp_bucket(bapi, temp, retention_s)

    # ── Phase 1: copy to temp with site tag ──────────────────────────────
    if not already_existed:
        log.info("  Phase 1: copying %d records to temp with site=%s ...", original_count, SITE)
        for device in devices:
            n = copy_device(qapi, wapi, bucket, temp, device, add_site=True)
            log.info("  [%s → temp] %d points", device, n)
    else:
        log.info("  Phase 1: skipped (temp already populated)")

    # ── Phase 2: verify temp ──────────────────────────────────────────────
    temp_count = count_records(qapi, temp)
    log.info("  Temp count: %d  (original: %d)", temp_count, original_count)
    if temp_count < original_count * 0.99:
        raise RuntimeError(
            f"ABORT: temp count {temp_count} is below 99% of original {original_count}. "
            "Fix the issue and re-run — temp bucket is preserved as checkpoint."
        )

    # ── Phase 3: delete originals ─────────────────────────────────────────
    log.info("  Phase 3: deleting all records from %s ...", bucket)
    dapi.delete(
        start=EPOCH,
        stop=FAR,
        predicate=f'_measurement="{MEASUREMENT}"',
        bucket=bucket,
        org=INFLUX_ORG,
    )
    # Give InfluxDB a moment to process the delete before writing back
    time.sleep(2)
    after_delete = count_records(qapi, bucket)
    log.info("  Records remaining after delete: %d", after_delete)

    # ── Phase 4: copy back to original ───────────────────────────────────
    log.info("  Phase 4: restoring %d records to %s ...", temp_count, bucket)
    for device in devices:
        n = copy_device(qapi, wapi, temp, bucket, device, add_site=False)
        log.info("  [temp → %s] %s: %d points", bucket, device, n)

    # ── Phase 5: verify final ─────────────────────────────────────────────
    final_count = count_records(qapi, bucket)
    log.info("  Final count in %s: %d", bucket, final_count)
    if final_count < original_count * 0.99:
        raise RuntimeError(
            f"ABORT: final count {final_count} is below 99% of original {original_count}. "
            "Data is still in temp bucket — do NOT delete it."
        )

    # ── Phase 6: delete temp ──────────────────────────────────────────────
    delete_temp_bucket(bapi, temp)
    log.info(
        "  ✓ %s migration complete: %d → %d records (site=%s)",
        bucket, original_count, final_count, SITE,
    )


def main():
    log.info("Connecting to InfluxDB at %s (org=%s)", INFLUX_URL, INFLUX_ORG)
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)

    try:
        client.ping()
    except Exception as e:
        log.error("Cannot reach InfluxDB: %s", e)
        sys.exit(1)

    log.info("Adding site=%s to all records in %d buckets", SITE, len(BUCKETS))
    log.info("Bucket order: raw → medium → hourly (largest first for early failure detection)")

    for bucket, retention_s in BUCKETS:
        migrate_bucket(client, bucket, retention_s)

    log.info("All buckets migrated successfully.")


if __name__ == "__main__":
    main()
