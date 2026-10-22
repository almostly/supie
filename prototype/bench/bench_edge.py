"""Cold and warm timings against the Edge Runtime container, plus a CPU limit check.

Usage: python3 bench/bench_edge.py [cold-runs]
"""

import json
import statistics
import sys
import urllib.request

URL = "http://127.0.0.1:9123/py-hello"  # the container started in the README

def call(headers, body):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}

runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
for mode in ("0", "1"):
    boots, mems, warm = [], [], []
    for _ in range(runs):
        cold = call({"x-cold": "1", "x-snapshot": mode}, {"n": 0})
        boots.append(cold["_meta"]["boot"]["wall_ms"])
        mems.append(cold["_meta"]["boot"]["mem_mb"]["heap_plus_external"])
        for _ in range(20):
            warm.append(call({"x-snapshot": mode}, {"n": 100})["_meta"]["req_wall_ms"])
    name = "snapshot" if mode == "1" else "plain"
    print(f"{name}: boot wall median {statistics.median(boots):.0f} ms, min {min(boots):.0f}, max {max(boots):.0f}; "
          f"warm median {statistics.median(warm):.2f} ms; boot memory {max(mems):.0f} MB")

for n in (1_000_000, 30_000_000):
    r = call({"x-snapshot": "1"}, {"n": n})
    print(f"loop n={n}:", r.get("_meta", {}).get("req_wall_ms"), "ms" if "_meta" in r else r)
