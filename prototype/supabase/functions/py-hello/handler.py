"""Python side of the edge function.

`handle(req)` receives a dict with method, path, query and the JSON body,
and returns a dict that becomes the JSON response.
"""

import math
import statistics


def handle(req: dict) -> dict:
    body = req.get("body") or {}
    xs = body.get("xs", [1, 2, 3, 4, 5])
    n = int(body.get("n", 0))

    # A small CPU workload so the timing numbers mean something:
    # sum of log-odds over n synthetic probabilities.
    acc = 0.0
    for i in range(1, n + 1):
        p = i / (n + 1)
        acc += math.log(p / (1 - p))

    return {
        "hello": body.get("name", "world"),
        "mean": statistics.fmean(xs) if xs else None,
        "stdev": statistics.pstdev(xs) if len(xs) > 1 else None,
        "logit_sum": acc,
        "n": n,
    }
