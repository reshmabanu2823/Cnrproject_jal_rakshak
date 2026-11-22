"""Pretend to be a flow sensor, so the IoT path can be demoed without hardware.

    python tools/simulate_device.py --key jr_xxx [--url http://127.0.0.1:8000] [--leak] [--interval 5]

Sends a batch every --interval seconds with pulse counts, like the ESP32 firmware.
--leak adds a constant trickle. Unsent batches are retried with the same `seq`, so
the server never double-counts them.
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime

PULSES_PER_LITER = 450


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True, help="device key from POST /api/devices")
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--leak", action="store_true")
    args = p.parse_args()

    seq, pending = int(time.time()), []
    while True:
        litres = random.choice([0, 0, 0, 0.5, 2, 6]) + (0.08 if args.leak else 0)
        seq += 1
        pending.append({"seq": seq, "ts": datetime.now().isoformat(timespec="seconds"),
                        "pulses": int(litres * PULSES_PER_LITER)})
        body = json.dumps({"firmware": "sim-1.0", "readings": pending[-500:]}).encode()
        req = urllib.request.Request(args.url + "/api/ingest", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Device-Key": args.key})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                print(datetime.now().strftime("%H:%M:%S"), json.loads(r.read()))
                pending.clear()
        except urllib.error.HTTPError as e:
            print("server said", e.code, e.read().decode()[:200])
            if e.code in (401, 403, 422):
                return
        except OSError as e:
            print("offline, will retry", len(pending), "reading(s):", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
