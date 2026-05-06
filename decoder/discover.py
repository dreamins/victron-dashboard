#!/usr/bin/env python3
"""Interactive discovery: listens to victron/raw and helps populate config/devices.json."""
import argparse
import json
import os
import sys
import time

import paho.mqtt.client as mqtt


def detect_type_name(hex_data: str) -> str:
    try:
        from victron_ble.devices import detect_device_type
        raw = bytes.fromhex(hex_data)
        klass = detect_device_type(raw)
        return klass.__name__ if klass else "Unknown"
    except Exception:
        return "Unknown"


def main():
    parser = argparse.ArgumentParser(
        description="Discover Victron BLE devices from MQTT and populate devices.json"
    )
    parser.add_argument("--broker", default=os.environ.get("MQTT_BROKER", "localhost"))
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default=os.environ.get("MQTT_USERNAME", "decoder"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD", ""))
    parser.add_argument("--duration", type=int, default=60,
                        help="Seconds to listen for advertisements (default: 60)")
    parser.add_argument("--output", default="config/devices.json")
    args = parser.parse_args()

    seen: dict[str, str] = {}  # mac -> device_type_name

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print(f"Connected to {args.broker}:{args.port} — listening for Victron advertisements...\n",
                  flush=True)
            client.subscribe("victron/raw")
        else:
            print(f"MQTT connect failed rc={rc}", file=sys.stderr)
            sys.exit(1)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            mac = payload.get("mac", "").upper()
            if not mac or mac in seen:
                return
            device_type = detect_type_name(payload.get("data", ""))
            suffix = mac.replace(":", "")[-4:].lower()
            suggested = f"{device_type.lower()}_{suffix}"
            seen[mac] = device_type
            print(f"Found: {device_type:<30} | MAC: {mac} | Suggested ID: {suggested}",
                  flush=True)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

    client = mqtt.Client()
    client.username_pw_set(args.username, args.password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, 60)
    client.loop_start()

    print(f"Scanning for {args.duration}s (Ctrl+C to stop early)...", flush=True)
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        pass
    client.loop_stop()

    if not seen:
        print("\nNo Victron devices found. Is the ESP32 bridge running and near devices?")
        return

    print(f"\nFound {len(seen)} device(s).")

    existing: dict = {"devices": []}
    if os.path.exists(args.output):
        with open(args.output) as f:
            existing = json.load(f)

    existing_macs = {d["mac"].upper() for d in existing.get("devices", [])}
    new_devices = []

    for mac, device_type in seen.items():
        if mac in existing_macs:
            print(f"Skipping {mac} — already in {args.output}")
            continue

        suffix = mac.replace(":", "")[-4:].lower()
        suggested_id = f"{device_type.lower()}_{suffix}"

        print(f"\nNew device: {device_type} | MAC: {mac}")
        device_id = input(f"  Device ID [{suggested_id}]: ").strip() or suggested_id
        label     = input("  Human label (e.g. 'MPPT North'): ").strip() or device_id
        print("  Open VictronConnect → tap device → three-dot menu → Product Info → Encryption Key")
        key = input("  Encryption key (32 hex chars): ").strip()

        new_devices.append({"id": device_id, "label": label, "mac": mac, "key": key})

    if not new_devices:
        print("No new devices to add.")
        return

    existing.setdefault("devices", []).extend(new_devices)
    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"\nSaved {len(new_devices)} device(s) to {args.output}")
    print("Restart ble-decoder to apply: docker compose restart ble-decoder")


if __name__ == "__main__":
    main()
