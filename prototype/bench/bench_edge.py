"""Cold and warm timings against the Edge Runtime container, plus a CPU-heavy loop.

Usage: python3 bench/bench_edge.py [function] [cold-runs]

py-hello runs both plain and snapshot boots. Other functions run once per cold worker.
"""

import json
import statistics
import sys
import urllib.request

BASE = "http://127.0.0.1:9123"  # the container started in the README


def call(fn, headers, body):
    req = urllib.request.Request(f"{BASE}/{fn}", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}


fn = sys.argv[1] if len(sys.argv) > 1 else "py-hello"
runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5
modes = {"plain": "0", "snapshot": "1"} if fn == "py-hello" else {fn: "0"}

for name, snap in modes.items():
    boots, mems, warm = [], [], []
    for _ in range(runs):
        cold = call(fn, {"x-cold": "1", "x-snapshot": snap}, {"n": 0})
        boots.append(cold["_meta"]["boot"]["wall_ms"])
        mems.append(cold["_meta"]["boot"]["mem_mb"]["heap_plus_external"])
        for _ in range(20):
            warm.append(call(fn, {"x-snapshot": snap}, {"n": 100})["_meta"]["req_wall_ms"])
    print(f"{name}: boot wall median {statistics.median(boots):.1f} ms, min {min(boots):.1f}, max {max(boots):.1f}; "
          f"warm median {statistics.median(warm):.2f} ms; boot memory {max(mems):.0f} MB")

snap = "1" if fn == "py-hello" else "0"
for n in (1_000_000, 30_000_000):
    r = call(fn, {"x-snapshot": snap}, {"n": n})
    print(f"loop n={n}:", f"{r['_meta']['req_wall_ms']} ms" if "_meta" in r else r)
