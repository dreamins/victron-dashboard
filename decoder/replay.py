#!/usr/bin/env python3
"""Replay Victron BLE fixture data via MQTT for isolated testing."""
import argparse
import json
import sys
import time

import paho.mqtt.client as mqtt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default="esp32-bridge")
    parser.add_argument("--password", default="test_esp32_pass")
    parser.add_argument("--site", default="test", help="Site ID — publishes to victron/{site}/raw")
    parser.add_argument("--rate", type=float, default=3.0, help="Messages/second across all devices")
    parser.add_argument("--loop", action="store_true", help="Loop fixture indefinitely")
    parser.add_argument("--duration", type=float, default=0,
                        help="Stop after N seconds (0 = no limit; implies --loop)")
    parser.add_argument("--fixture", default="decoder/fixtures/victron_sample.jsonl")
    args = parser.parse_args()

    connected = False

    def on_connect(client, userdata, flags, rc):
        nonlocal connected
        if rc == 0:
            connected = True
        else:
            print(f"MQTT connect failed rc={rc}", file=sys.stderr)
            sys.exit(1)

    client = mqtt.Client()
    client.username_pw_set(args.username, args.password)
    client.on_connect = on_connect
    client.connect(args.broker, args.port, 60)
    client.loop_start()

    deadline = time.time() + 5
    while not connected:
        if time.time() > deadline:
            print("Timed out waiting for MQTT connection", file=sys.stderr)
            sys.exit(1)
        time.sleep(0.05)

    print(f"Connected to {args.broker}:{args.port}", flush=True)

    with open(args.fixture) as f:
        messages = [json.loads(line) for line in f if line.strip()]

    if not messages:
        print("Fixture file is empty", file=sys.stderr)
        sys.exit(1)

    interval = 1.0 / args.rate
    total = 0
    deadline = time.time() + args.duration if args.duration > 0 else None
    loop = args.loop or args.duration > 0

    try:
        while True:
            for msg in messages:
                if deadline and time.time() >= deadline:
                    break
                payload = json.dumps(msg)
                mac = msg.get("mac", "?")
                size = len(msg.get("data", "")) // 2 if "data" in msg else "-"
                client.publish(f"victron/{args.site}/raw", payload).wait_for_publish()
                total += 1
                print(f"[{total}] {mac}  {size}B", flush=True)
                time.sleep(interval)
            if deadline and time.time() >= deadline:
                break
            if not loop:
                break
    except KeyboardInterrupt:
        pass

    print(f"\nPublished {total} messages total", flush=True)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
