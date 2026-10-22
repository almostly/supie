"""A function exercising supie in the Edge Runtime, under MicroPython and Pyodide."""

from __future__ import annotations

import math
from typing import Any, Optional

from helpers.scoring import points
from supie import App, HTTPError, Request, Response, env

app = App()


@app.on_boot
async def setup():
    """Read an environment variable once per worker."""
    app.state.greeting = env.get("GREETING", "hello")


@app.get("/")
def root(req: Request) -> dict[str, Any]:
    """Plain JSON, with state from boot and a query parameter."""
    name: Optional[str] = req.query.get("name")
    return {"greeting": app.state.greeting, "name": name or "world"}


@app.post("/score/{applicant:int}")
def score_applicant(req: Request, applicant: int):
    """A path parameter, a JSON body and a helper module."""
    body = req.json()
    if "age" not in body:
        raise HTTPError(422, "missing: age")
    total = 600 + points(body["age"])
    pd = 1 / (1 + math.exp((total - 600) / 28.85))
    return Response(
        {"applicant": applicant, "score": total, "pd": round(pd, 4)}, status=201
    )


@app.get("/db")
async def db(req: Request):
    """A Supabase query and an RPC call, through the shim's fetch."""
    rows = (
        await req.supabase.table("loans")
        .select("id", count="exact")
        .eq("id", 7)
        .execute()
    )
    rpc = await req.supabase_admin.rpc("ping", {"x": 1})
    return {"select": rows.data, "count": rows.count, "rpc": rpc.data}


@app.get("/storage")
async def storage(req: Request):
    """Binary download and upload through Storage."""
    bucket = req.supabase_admin.storage.from_("models")
    data = await bucket.download("m.bin")
    sent = await bucket.upload("copy.bin", data)
    return {"downloaded": list(data), "uploaded": sent["body"]}


@app.get("/bytes")
def raw(req: Request):
    """A binary response."""
    return Response(bytes([0, 128, 255]), media_type="application/x-demo")


@app.post("/later")
def later(req: Request):
    """Work after the response."""

    async def work():
        """Log from a background task."""
        print("background done")

    req.background(work())
    return {"queued": True}


@app.get("/boom")
def boom(req: Request):
    """An unexpected error."""
    return 1 / 0


# A workflow, with checkpoints kept in the worker's memory.
from supie import sleep  # noqa: E402
from supie.flow import Flow, MemoryStore  # noqa: E402

scoring = Flow("scoring", MemoryStore())


@scoring.step()
async def bureau(ctx, application):
    """A slow check."""
    await sleep(0.2)
    return {"score": 700}


@scoring.step()
async def fraud(ctx, application):
    """Another slow check, in parallel with bureau."""
    await sleep(0.2)
    return {"flag": application.get("suspicious", False)}


@scoring.step(depends_on=[bureau, fraud], retries=2, backoff=0.01)
def score(ctx, application, bureau, fraud):
    """Fails on the first attempt, to exercise retries."""
    if ctx.attempt == 1:
        raise RuntimeError("flaky")
    return {"pd": 0.02, "fraud": fraud["flag"], "attempts": ctx.attempt}


@scoring.step(depends_on=[score])
def decide(ctx, application, score):
    """Approve, or hand over to review."""
    if score["fraud"]:
        return ctx.goto(review)
    return {"decision": "approve", "attempts": score["attempts"]}


@scoring.branch(depends_on=[score])
def review(ctx, application, score):
    """Only when decide goes here."""
    return {"decision": "refer"}


@app.post("/decide")
async def decide_now(req: Request):
    """Run the flow inside the request and time it."""
    import time

    started = time.time()
    result = await scoring.run(req.json(), request=req)
    return {"output": result.output, "seconds": time.time() - started}


scoring.mount(app, "/flows/scoring")


# An ONNX model from (the fake) Storage, loaded on first use.
from supie.onnx import Model  # noqa: E402


@app.post("/model")
async def model(req: Request):
    """Score rows with the tiny logistic model."""
    if not hasattr(app.state, "model"):
        app.state.model = await Model.from_storage("models", "tiny.onnx")
    out = await app.state.model.run({"features": req.json()["rows"]})
    return {
        "pd": [row[0] for row in out["probability"]],
        "inputs": app.state.model.inputs,
    }
