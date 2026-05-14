#!/usr/bin/env python3
"""Interactive BMS identification wizard.

Probes all nearby LiTime BMS devices, reads live SOC/voltage from each,
and asks you to map each device to a named slot in sites.json.  The
discovered MAC addresses are saved permanently so ble-bridge connects
directly on every subsequent start — no probing overhead.

Run this ONCE per install (or whenever you add a new BMS):

    bash identify_bms.sh

(That script runs this file inside the ble-bridge container so it has
BlueZ/bleak access without installing anything on the host.)
"""
import asyncio
import json
import os
import pathlib
import sys

SITES_FILE = os.environ.get("SITES_FILE", "/app/sites.json")


def _fmt(fields: dict, key: str, fmt: str) -> str:
    v = fields.get(key)
    return fmt.format(v) if isinstance(v, (int, float)) else "?"


async def _read_snapshot(address: str, write_uuid: str, notify_uuid: str,
                          timeout: float = 5.0) -> dict:
    """Connect to one BMS and return one parsed data snapshot."""
    from bleak import BleakClient
    from drivers.litime import build_frame, parse_litime_frame, _C13_ANCHOR, FRAME_LEN

    buf   = bytearray()
    done  = asyncio.Event()
    result: dict = {}

    def _handler(_, data: bytes):
        buf.extend(data)
        idx = buf.find(_C13_ANCHOR)
        if idx >= 3 and idx - 3 + FRAME_LEN <= len(buf):
            frame  = bytes(buf[idx - 3: idx - 3 + FRAME_LEN])
            fields = parse_litime_frame(frame)
            if fields:
                result.update(fields)
                done.set()

    try:
        async with BleakClient(address, timeout=15.0) as client:
            await client.start_notify(notify_uuid, _handler)
            await client.write_gatt_char(write_uuid, build_frame(0x13), response=True)
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass
    return result


async def main() -> int:
    from drivers.litime import probe_all_litime

    path = pathlib.Path(SITES_FILE)
    if not path.exists():
        print(f"ERROR: {SITES_FILE} not found.")
        return 1

    with open(path) as f:
        config = json.load(f)

    unconfigured = [
        (site["id"], dev)
        for site in config.get("sites", [])
        for dev in site.get("devices", [])
        if dev.get("type") == "litime_bms" and not dev.get("mac")
    ]

    if not unconfigured:
        print("All BMS devices already have MAC addresses — nothing to do.")
        return 0

    print(f"\nSearching for LiTime BMS devices (takes about 10 seconds)...\n")
    found = await probe_all_litime()

    if not found:
        print("No LiTime BMS devices found.")
        print("Make sure the BMS is powered on and within Bluetooth range, then try again.")
        return 1

    # Read live data from all found devices for identification / confirmation.
    print(f"Found {len(found)} LiTime BMS device(s). Reading live data...\n")
    snapshots = []
    for i, (addr, w_uuid, n_uuid) in enumerate(found, 1):
        print(f"  Connecting to [{i}] {addr}... ", end="", flush=True)
        fields = await _read_snapshot(addr, w_uuid, n_uuid)
        snapshots.append((addr, w_uuid, n_uuid, fields))
        print(f"SOC={_fmt(fields, 'soc', '{:.0f}%')}  "
              f"V={_fmt(fields, 'battery_voltage', '{:.2f}V')}  "
              f"Temp={_fmt(fields, 'temperature', '{:.0f}°C')}")

    if len(found) == 1 and len(unconfigured) == 1:
        addr, _, _, fields = snapshots[0]
        site_id, dev = unconfigured[0]
        print(f"\nFound one BMS. Is this your \"{dev['label']}\" (site={site_id})?")
        print(f"  Address : {addr}")
        print(f"  SOC     : {_fmt(fields, 'soc', '{:.0f}%')}")
        print(f"  Voltage : {_fmt(fields, 'battery_voltage', '{:.2f}V')}")
        print(f"  Temp    : {_fmt(fields, 'temperature', '{:.0f}°C')}")
        try:
            answer = input("\nAssign this BMS to that slot? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer in ("", "y", "yes"):
            dev["mac"] = addr
            _save(path, config)
            return 0
        else:
            print("Aborted — no changes made.")
            return 1

    assigned: set = set()
    for site_id, dev in unconfigured:
        available = [i for i in range(1, len(snapshots) + 1) if i not in assigned]
        if not available:
            print(f"\nNo unassigned BMS left for '{dev['label']}' — skipping.")
            continue

        if len(available) == 1:
            idx = available[0]
            dev["mac"] = snapshots[idx - 1][0]
            assigned.add(idx)
            print(f"\nOnly one device left — auto-assigned [{idx}] to '{dev['label']}'.")
            continue

        print(f"\nWhich device is \"{dev['label']}\"?  (site={site_id}, id={dev['id']})")
        for i in available:
            addr, _, _, fields = snapshots[i - 1]
            print(f"  [{i}] {addr}  "
                  f"SOC={_fmt(fields, 'soc', '{:.0f}%')}  "
                  f"V={_fmt(fields, 'battery_voltage', '{:.2f}V')}  "
                  f"Temp={_fmt(fields, 'temperature', '{:.0f}°C')}")

        while True:
            opts = "/".join(str(i) for i in available)
            try:
                raw = input(f"Enter number [{opts}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return 1
            try:
                choice = int(raw)
                if choice in available:
                    dev["mac"] = snapshots[choice - 1][0]
                    assigned.add(choice)
                    print(f"  Assigned {dev['mac']} to '{dev['label']}'.")
                    break
            except ValueError:
                pass
            print(f"  Please enter one of: {opts}")

    _save(path, config)
    return 0


def _save(path: pathlib.Path, config: dict):
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nConfiguration saved to {path}.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
