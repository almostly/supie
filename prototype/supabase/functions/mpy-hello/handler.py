"""Python side of the MicroPython edge function, the same work as py-hello.

MicroPython has no statistics module, so mean and stdev are computed here.
"""

import math


def handle(req):
    body = req.get("body") or {}
    xs = body.get("xs", [1, 2, 3, 4, 5])
    n = int(body.get("n", 0))

    acc = 0.0
    for i in range(1, n + 1):
        p = i / (n + 1)
        acc += math.log(p / (1 - p))

    mean = sum(xs) / len(xs) if xs else None
    stdev = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs)) if len(xs) > 1 else None
    return {
        "hello": body.get("name", "world"),
        "mean": mean,
        "stdev": stdev,
        "logit_sum": acc,
        "n": n,
    }
