"""Score a loan applicant with a points-based scorecard.

POST / with {"age": 40, "income": 250000, "dti": 0.3} returns the score and the
probability of default. Each decision is logged to the score_log table after the
response is sent, as the caller, so row level security applies.
"""

import math
from typing import Any

from supie import App, HTTPError, Request

app = App()

# Module level: built once per worker.
BINS = {
    "age": [(25, -18), (35, 4), (50, 12), (math.inf, 20)],
    "income": [(100_000, -25), (300_000, 6), (math.inf, 22)],
    "dti": [(0.2, 18), (0.4, 3), (math.inf, -30)],
}
OFFSET = 600
FACTOR = 20 / math.log(2)  # 20 points to double the odds


def points(feature: str, value: float) -> int:
    """The points for one feature's value."""
    for upper, pts in BINS[feature]:
        if value < upper:
            return pts
    raise ValueError(feature)


@app.get("/health")
def health(req: Request) -> dict[str, bool]:
    """Liveness check."""
    return {"ok": True}


@app.post("/")
def score(req: Request) -> dict[str, float]:
    """Score an applicant and log the decision in the background."""
    applicant = req.json()
    missing = [f for f in BINS if f not in applicant]
    if missing:
        raise HTTPError(422, "missing: " + ", ".join(sorted(missing)))
    total = OFFSET + sum(points(f, applicant[f]) for f in BINS)
    pd = 1 / (1 + math.exp((total - OFFSET) / FACTOR))
    req.background(log_decision(req, applicant, total, pd))
    return {"score": total, "pd": round(pd, 4)}


async def log_decision(
    req: Request, applicant: dict[str, Any], total: int, pd: float
) -> None:
    """Record the decision in score_log."""
    row = {"applicant_id": applicant.get("id"), "score": total, "pd": pd}
    await req.supabase.table("score_log").insert(row).execute()
