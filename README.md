<p align="center">
  <img src="https://raw.githubusercontent.com/almostly/supie/main/docs/images/supie_icon.png" width="128" alt="supie">
</p>

# supie

Python edge functions for Supabase.

supie builds a plain Python handler into an ordinary Supabase Edge Function. The function runs Python on WebAssembly inside Supabase's Edge Runtime, so you deploy it with the Supabase CLI like any TypeScript function. supie also adds workflows with parallel steps and retries, queues with exactly-once results and ONNX models that score at native speed.

```python
from supie import App, HTTPError, Request

app = App()


@app.post("/")
def score(req: Request):
    applicant = req.json()
    if "age" not in applicant:
        raise HTTPError(422, "missing: age")
    return {"score": 600 + points(applicant["age"])}
```

supie is an independent almostly project, unaffiliated with Supabase.

## Install

```bash
pip install supie
```

You also need the [Supabase CLI](https://supabase.com/docs/guides/cli). The Pyodide runtime needs Deno or Node.js at build time, to build its snapshot.

## Quickstart

In a Supabase project (run `supabase init` first if you don't have one):

```bash
supie init hello  # py/hello/main.py and py/hello/test_main.py
pytest py/hello  # handlers are plain Python
supie build  # writes supabase/functions/hello/
supie serve  # supabase functions serve, rebuilding when py/ changes
supie deploy  # supabase functions deploy, after building
```

Your Python lives in `py/<name>/`, and `supie build` turns it into `supabase/functions/<name>/`. Treat the generated folder like a `dist/` directory: edit `py/`, never the output.

## Performance

supie runs your code on MicroPython by default, or on Pyodide (CPython compiled to WebAssembly) for the full standard library and packages. On hosted Supabase, where nearly every request gets a fresh worker:

| | bundle | cold boot | cold request, end to end |
|---|---|---|---|
| MicroPython | 0.2 MB | 19 ms | 0.18 s |
| Pyodide, from a snapshot | 12 MB | 300 ms | 0.74 s |

Handling a request costs about the same as TypeScript, but a Python loop runs 45 to 170 times slower, and hosted functions get 2 s of CPU per request. supie fits request logic, validation, scoring and glue code.

In a one-hour run on hosted Supabase, 99.95% of 3,916 scoring requests and 99.77% of 4,258 queued applications succeeded, with no server errors, and every application ended exactly once. See [performance and reliability](https://github.com/almostly/supie/blob/main/docs/performance.md).

![An hour of traffic replayed in under a minute: requests flow through the edge function and the queue, and every application ends decided once or in the dead letters](https://github.com/almostly/supie/releases/download/0.1.0/supie-reliability-replay.gif)

## Handlers

Routes take typed path parameters, handlers can be sync or async, and `req.supabase` is an async Supabase client that acts as the caller, so row level security applies:

```python
@app.get("/loans/{loan_id:int}")
async def get_loan(req: Request, loan_id: int):
    rows = await req.supabase.table("loans").select("id, amount").eq("id", loan_id).execute()
    if not rows.data:
        raise HTTPError(404, "loan not found")
    return rows.data[0]
```

See [handlers and the Supabase client](https://github.com/almostly/supie/blob/main/docs/handlers.md).

## Workflows

A `Flow` chains steps, like AWS Step Functions over your edge functions. Independent steps run in parallel, failed steps retry, and every finished step is saved, so a run can resume where it stopped. Runs can also go to [pgflow](https://pgflow.dev), so Postgres schedules each step and runs outlive any worker.

```python
scoring = Flow("scoring")


@scoring.step(retries=2, timeout=5)
async def bureau(ctx, application):
    return await ctx.invoke("bureau-fetch", application)


@scoring.step(depends_on=[bureau])
def decide(ctx, application, bureau):
    return {"decision": "approve" if bureau["score"] > 650 else "decline"}
```

See [workflows](https://github.com/almostly/supie/blob/main/docs/workflows.md) and the [flow walkthrough notebook](https://github.com/almostly/supie/blob/main/examples/notebooks/flow_walkthrough.ipynb).

## Queues

A queue on `pgmq`, the engine behind Supabase Queues, accepts work fast and handles it in the background, with retries and a dead-letter table. Send keys and `ctx.once` make each result happen exactly once, even when a client retries or a message is delivered twice:

```python
@app.post("/score")
async def score(req: Request) -> Response:
    application = req.json()
    await queue.send(application, key=f"application-{application['id']}")
    return Response({"queued": True}, status=202)


@queue.consumer(max_attempts=5)
async def handle(ctx, message):
    await ctx.once(f"decide:{message['id']}", lambda: decide(message))
```

See [queues](https://github.com/almostly/supie/blob/main/docs/queues.md).

## Models

Supabase's Edge Runtime has a native ONNX engine, and supie gives Python handlers access to it, so a model scores at native speed. `supie model publish` takes a model from an MLflow registry alias and uploads it to a private bucket:

```python
@app.on_boot
async def setup():
    app.state.model = await Model.from_storage("models", "pd/v1.onnx")
```

See [models](https://github.com/almostly/supie/blob/main/docs/models.md).

## Testing

Handlers are plain Python, so you test them with pytest and no Supabase:

```python
def test_score():
    client = TestClient(app, env={"SCORING_KEY": "test"})
    assert client.post("/score", json={"age": 40}).status == 201
```

See [testing](https://github.com/almostly/supie/blob/main/docs/testing.md).

## Documentation

- [Runtimes and packages](https://github.com/almostly/supie/blob/main/docs/runtimes.md): choosing between MicroPython and Pyodide, payload sizes, PyPI packages and preloading them into the snapshot
- [How it works](https://github.com/almostly/supie/blob/main/docs/how-it-works.md): the build, the TypeScript shim and the limitations

## Development

```bash
uv sync
uv run pytest -m "not edge and not postgres"  # unit tests
uv run pytest -m "edge or postgres"  # Edge Runtime, Postgres, PostgREST and pgflow, needs Docker
uv run ty check src tests scripts examples  # types
```

CI runs the Docker tests against pinned images, and a weekly workflow runs them against the images the Supabase CLI ships that day.
