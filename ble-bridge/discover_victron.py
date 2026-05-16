#!/usr/bin/env python3
"""Scan for Victron BLE devices and print their MAC addresses.

Run inside the ble-bridge container (or on a host with bleak + BlueZ):
    python /app/discover_victron.py

The scan runs for 30 seconds. Any device that broadcasts the Victron
manufacturer ID (0x02E1) is printed. Encryption keys are NOT needed for
discovery — only for decoding data.  Get keys from: Victron Connect app
→ device → Settings → Product Info → BLE key (tap the eye icon).
"""
import asyncio
import os
import sys

VICTRON_MFR_ID = 0x02E1
SCAN_TIMEOUT   = int(os.environ.get("DISCOVER_TIMEOUT", "30"))


async def main():
    try:
        from bleak import BleakScanner
    except ImportError:
        sys.exit("bleak is not installed — run inside the ble-bridge container")

    adapter = os.environ.get("BLE_ADAPTER", "")
    adapter_kw = {"bluez": {"adapter": adapter}} if adapter else {}

    found: dict[str, tuple[str, int]] = {}  # MAC → (name, rssi)

    def _callback(device, adv_data):
        if VICTRON_MFR_ID not in adv_data.manufacturer_data:
            return
        mac = device.address.upper()
        rssi = adv_data.rssi or 0
        if mac not in found:
            name = device.name or "Victron Device"
            found[mac] = (name, rssi)
            print(f"  FOUND  {mac}  {name}  rssi={rssi} dBm", flush=True)

    print(f"Scanning for Victron BLE devices for {SCAN_TIMEOUT}s "
          f"(adapter={adapter or 'default'})...", flush=True)
    scanner = BleakScanner(detection_callback=_callback, **adapter_kw)
    await scanner.start()
    await asyncio.sleep(SCAN_TIMEOUT)
    await scanner.stop()

    print(f"\n{'='*60}")
    print(f"Found {len(found)} Victron device(s)")

    if not found:
        print("No Victron devices found. Check that devices are powered on.")
        print("If using a secondary adapter, set BLE_ADAPTER=hci1 (or similar).")
        return

    print("\nAdd these to config/sites.json under the appropriate site:")
    print("  (Replace <KEY> with the BLE key from Victron Connect app)")
    print()
    for mac, (name, rssi) in sorted(found.items()):
        print(f'    {{"id": "garage_mpptX", "label": "{name}",')
        print(f'     "type": "victron_mppt", "mac": "{mac}",')
        print(f'     "key": "<KEY_FROM_VICTRON_CONNECT>",')
        print(f'     "write_interval_s": 60}}')
        print()
    print("Then restart the ble-bridge:")
    print("  docker compose restart ble-bridge")


if __name__ == "__main__":
    asyncio.run(main())
