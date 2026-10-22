"""A durable flow on pgflow, run by a worker inside the edge function."""

from supie import App, Request, sleep
from supie.flow import Flow

app = App()
scoring = Flow("edge_scoring")


@scoring.step()
async def bureau(ctx, application):
    """A slow check."""
    await sleep(0.3)
    return {"score": 700}


@scoring.step()
async def fraud(ctx, application):
    """Another slow check, in parallel."""
    await sleep(0.3)
    return {"flag": application.get("suspicious", False)}


@scoring.step(depends_on=[bureau, fraud], retries=1, backoff=1)
def decide(ctx, application, bureau, fraud):
    """Pick a route; the first attempt fails, to exercise pgflow's retries."""
    if ctx.attempt == 1:
        raise RuntimeError("flaky")
    return {"route": "review" if fraud["flag"] else "approve"}


@scoring.step(depends_on=[decide], when={"decide": {"route": "approve"}})
def approve(ctx, application, decide):
    """The approve route."""
    return {"decision": "approve", "id": application["id"]}


@scoring.step(depends_on=[decide], when={"decide": {"route": "review"}})
def review(ctx, application, decide):
    """The review route."""
    return {"decision": "refer", "id": application["id"]}


scoring.mount(app, "/flows", durable=True)


@app.get("/shape")
def shape(req: Request):
    """The compiled shape, to compare runtimes."""
    return scoring.shape()
