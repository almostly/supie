<p align="center">
  <img src="https://raw.githubusercontent.com/almostly/supie/main/docs/images/supie_icon.png" width="128" alt="supie">
</p>

# supie

Python edge functions for Supabase.

Write a plain Python handler, and supie builds it into an ordinary Supabase Edge Function. The function runs Python on WebAssembly inside Supabase's Edge Runtime, so you deploy it with the Supabase CLI like any TypeScript function.

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

supie is an independent almostly project, not an official Supabase product.

## How fast is it?

supie runs your code on one of two Python runtimes. MicroPython is the default. These figures come from hosted Supabase in eu-west-1, where nearly every request got a fresh worker:

| | bundle | cold boot | cold request, end to end | memory |
|---|---|---|---|---|
| MicroPython | 0.2 MB | 19 ms | 0.18 s | 31 MB |
| Pyodide, from a snapshot | 12 MB | 300 ms | 0.74 s | 103 MB |

Handling a request costs about the same as TypeScript. Heavy computation does not: a Python loop runs 45 to 170 times slower than the same loop in TypeScript, and hosted functions get 2 s of CPU per request. supie fits request logic, validation, scoring and glue code. Batch work belongs on a container service.

The [prototype](prototype/) has the full measurements.

### How reliable is it?

A one-hour soak on hosted Supabase (eu-west-1, MicroPython, 2026-10-22), from a client in Europe: steady traffic of one inline scoring request and one queued application per second, a burst of 30 of each every 5 minutes, and one queued application in ten sent twice as a client retry.

![An hour of traffic replayed in under a minute: requests flow through the edge function and the queue, and every application ends decided once or in the dead letters](https://github.com/almostly/supie/releases/download/0.1.0/supie-reliability-replay.gif)

| | requests | succeeded | p50 | p90 | p99 |
|---|---|---|---|---|---|
| inline scoring | 3,916 | 99.95% | 0.31 s | 2.2 s | 3.8 s |
| queued application | 4,258 | 99.77% | 0.53 s | 2.8 s | 4.3 s |

- **No server errors.** All 12 failures were connection-level (a reset or an early end of the TLS stream), none an HTTP 5xx from the function.
- **Nothing lost, nothing doubled.** Every one of 3,916 applications was accepted after at most three client retries, and each ended exactly once: 3,837 decided once, 79 in the dead letters as designed. The 334 duplicate sends and 10 retries added no messages.
- **The tail is slow.** One request in ten took over 2 s, and a few over 10 s, in steady traffic rather than in bursts. Clients should use timeouts and retries, which the queue's send keys make safe.

## Install

```bash
uv tool install git+https://github.com/almostly/supie
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

Your Python lives in `py/<name>/`. `supie build` turns it into `supabase/functions/<name>/` and adds the function to `supabase/config.toml`. Treat the generated folder like a `dist/` directory: edit `py/`, never the output. supie refuses to overwrite a function it didn't generate.

`supie serve` watches `py/`: a change rebuilds that function and the Supabase CLI reloads it, in milliseconds for MicroPython. Pass `--no-watch` to build once. Options after `--` go to `supabase functions serve`.

Everything in `py/<name>/` ships with the function, including helper modules, packages and data files. Tests, caches and hidden files are left out.

## Handlers

```python
from supie import App, HTTPError, Request, Response, env

app = App()


@app.on_boot
def setup():
    # Runs once per worker, before the first request. Read secrets here.
    app.state.api_key = env["SCORING_KEY"]


@app.get("/loans/{loan_id:int}")
async def get_loan(req: Request, loan_id: int):
    rows = await req.supabase.table("loans").select("id, amount").eq("id", loan_id).execute()
    if not rows.data:
        raise HTTPError(404, "loan not found")
    return rows.data[0]


@app.post("/score")
def score(req: Request):
    result = model(req.json())
    req.background(audit(req, result))  # runs after the response is sent
    return Response(result, status=201, headers={"x-model": "v3"})
```

- **Routes.** Use `app.get`, `post`, `put`, `patch` and `delete`, or `app.route(path, methods)`. Path parameters take a type: `{id:int}`, `{x:float}`, `{name}` for a string, and `{rest:path}` for the rest of the path. Paths are relative to the function, so `/` is `/functions/v1/<name>`.
- **Handlers.** They can be plain functions or `async def`. They receive the request and the path parameters as keyword arguments.
- **Requests.** A request has `method`, `path`, `query`, `headers` (lower case), `params`, `text()` and `json()`. `json()` answers 400 for a missing or invalid body. `req.user` holds the caller's JWT claims, and `req.token` the raw token. The claims are decoded, not verified: Supabase's gateway checks the signature only while the function keeps `verify_jwt` on. `await req.check_service()` is true only for a real service role key: the function's own key passes at once, and another token that claims the role must be accepted by Supabase Auth, which verifies its signature. supie's work, replay and resume endpoints check it, so a token that merely claims the service role is refused.
- **Return values.** A dict or list becomes JSON, a str becomes text, `bytes` stay binary, and `None` sends a 204. Return a `Response` to set the status, headers or content type.
- **Errors.** `HTTPError(status, detail)` sends `{"error": detail}`. Any other exception sends a 500 to the caller and writes the traceback to the function's logs.
- **Environment.** `env["NAME"]` and `env.get("NAME")` read environment variables and secrets at request time.

## Supabase from Python

supabase-py can't run inside the function, so supie includes an async client with the same shape:

```python
await req.supabase.table("loans").select("*", count="exact").gte("amount", 1000).order("amount", desc=True).limit(10).execute()
await req.supabase.table("score_log").insert({"pd": 0.04}).execute()
await req.supabase.table("loans").update({"status": "closed"}).eq("id", 7).execute()
await req.supabase.rpc("score_applicant", {"id": 7})
await req.supabase.functions.invoke("fraud-check", {"id": 7})
data = await req.supabase_admin.storage.from_("models").download("scorecard.json")
```

Hosted Supabase allows 30 calls from one function to another per request trace and minute, counting nested calls and calls made by background work. Past that, `functions.invoke` and `ctx.invoke` raise `supie.supabase.RateLimited`, with `retry_after` in seconds. Calls to PostgREST, Storage and other hosts don't count: 60 PostgREST calls in one request all succeeded.

`req.supabase` acts as the caller, forwarding their token so row level security applies. `req.supabase_admin` uses the service role key and bypasses it. Both read `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY`, which Supabase sets for every function.

The query builder covers `select`, `insert`, `upsert`, `update`, `delete`, the filters `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `is_` and `in_`, plus `filter`, `order`, `limit`, `offset` and `single`. A failed call raises `supie.supabase.SupabaseError` with the status and message.

## Models

Supabase's Edge Runtime has a native ONNX engine built in. supie gives Python handlers access to it, so a model scores outside WebAssembly, at native speed:

```python
from supie import App, Request
from supie.onnx import Model

app = App()
FEATURES = ["age", "income", "dti", "utilization", "delinquencies"]


@app.on_boot
async def setup():
    app.state.model = await Model.from_storage("models", "pd/v1.onnx")  # a private bucket


@app.post("/")
async def score(req: Request):
    applicant = req.json()
    out = await app.state.model.run({"features": [[applicant[f] for f in FEATURES]]})
    return {"pd": out["probabilities"][0][1]}
```

`Model.from_storage` downloads the model as the service role, unless you pass a client. `Model.from_url(url, headers)` loads one from anywhere else. `run` takes nested lists of float32, or `{"data": ..., "type": "int64"}` for other types, and returns nested lists by output name. Under CPython, as in tests, models run with the `onnxruntime` package, and the test client's `fetch` serves the model file.

To get a model into Storage, publish it. The source can be an ONNX file, an MLflow model folder, or an MLflow URI, so a registry alias works:

```bash
pip install 'supie[models]' mlflow-skinny
supie model publish models:/pd@champion --bucket models --path pd/v3.onnx --project-ref <ref>
```

`publish` downloads the model with MLflow and takes its ONNX flavor. It removes any ZipMap, the output classifier exports from CatBoost, scikit-learn and others end in, which the engine can't return. Then it uploads to the bucket, creating it as private if needed. The service role key comes from the Supabase CLI with `--project-ref`, or from `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. MLflow runs only here, at build time; the function never imports it. Log an ONNX copy next to the native model, for example with `mlflow.onnx.log_model`, and compare their predictions on a holdout set before promoting a new champion.

`supie model prepare in.onnx out.onnx` does only the ZipMap step, locally.

A CatBoost model of 1.6 MB, published from an MLflow registry alias to a private bucket on hosted Supabase:

| | |
|---|---|
| 500 holdout rows | 1.8 s end to end, largest difference from native CatBoost 2.4e-7 |
| one applicant, 20 requests | median 0.53 s end to end, each on a fresh worker |

On a fresh worker, about 0.3 s of that is downloading the model from Storage. A gradient-boosted model scores a row in about 0.1 ms.

## Workflows

A `Flow` chains steps, like AWS Step Functions over your edge functions. Steps run as soon as their dependencies are done, so independent ones run in parallel. Every finished step is saved, so a run can resume where it stopped, and the saved steps double as an audit trail.

```python
from supie import App, Request
from supie.flow import Flow

app = App()
scoring = Flow("scoring")


@scoring.step(retries=2, timeout=5)
async def bureau(ctx, application):
    return await ctx.invoke("bureau-fetch", application)  # another edge function


@scoring.step(timeout=5)
async def fraud(ctx, application):
    return await ctx.invoke("fraud-check", application)


@scoring.step(depends_on=[bureau, fraud])
def score(ctx, application, bureau, fraud):
    return {"pd": model(application, bureau), "fraud": fraud["flag"]}


@scoring.step(depends_on=[score])
def decide(ctx, application, score):
    if score["fraud"]:
        return ctx.goto(manual_review)
    return {"decision": "approve" if score["pd"] < 0.08 else "decline"}


@scoring.branch(depends_on=[score])
async def manual_review(ctx, application, score):
    await ctx.supabase.table("review_queue").insert({"application": application}).execute()
    return {"decision": "refer"}


@app.post("/decide")
async def decide_now(req: Request):
    result = await scoring.run(req.json(), request=req)  # inside the request
    return result.output


scoring.mount(app, "/flows/scoring")  # or start runs in the background
```

- **Steps.** A step receives a context, the flow's input and its dependencies' outputs, in the order of `depends_on`. It can be sync or async, and must return something JSON can hold.
- **Retries and timeouts.** `retries` adds attempts after a failure, waiting `backoff`, then twice that, and so on. `timeout` fails an async step that runs too long. When the attempts run out, the step and the run are saved as failed and `run` raises `StepFailed`.
- **Conditions.** `when=` runs a step only if its input contains a JSON pattern, and `unless=` skips it if it does. The input is the outputs of the step's dependencies, by step name, or the flow's input for a step without dependencies. So `when={"decide": {"route": "review"}}` runs a step only on that route. A skipped step's dependents still run and get `None` for it. These are pgflow's conditions, so they work in durable runs too.
- **Branches.** A step returns `ctx.goto(branch)` to hand its result to a branch, which then counts as that step's output. Branches run only when a step goes to them. They work in inline runs only; durable runs use `when=`.
- **The result.** It's the last step's output. A flow that ends in several steps returns a dict of them.
- **Calling other functions.** `ctx.invoke` counts toward the 30 calls per trace described under Supabase from Python, so a flow that fans out to other functions should stay well under 30 calls. Checkpoint writes don't count.
- **Context.** `ctx` has `run_id`, `step`, `attempt`, `supabase` (as the caller when the run came from a request), `supabase_admin` and `invoke(name, body)` for calling other edge functions.
- **Resuming.** Run the same run ID again with `scoring.run(None, run_id=...)`. Saved steps are skipped and the saved input is used.

`scoring.mount(app, prefix)` adds these endpoints:

| Endpoint | What it does |
|---|---|
| `POST <prefix>` | Starts a run with the JSON body as input, answering 202 with its `run_id`. The run continues after the response. |
| `GET <prefix>/<run_id>` | Returns the status, output, error and every saved step. |
| `POST <prefix>/<run_id>/resume` | Continues a run whose worker stopped. |
| `POST <prefix>/resume-stale?idle=300` | For the service role: resumes every run idle that long. Call it from pg_cron. |

A run started by a signed-in user is visible only to that user and the service role. Anyone else holding the random run ID can read a run started anonymously.

### How fast inline flows are

Inline steps are plain function calls in one process, so a flow costs about what its steps compute. Six steps of scorecard logic on JSON input (features, then bins and policy rules in parallel, a score, and a route chosen with `when=`), with the memory store:

| | per run, warm | first run in a request | request end to end |
|---|---|---|---|
| MicroPython, hosted | 2.7 ms | 5 ms | median 0.26 s |
| Pyodide, hosted | 2 to 5 ms | 11 ms | median 0.8 s (cold boot) |

End to end is mostly the worker's cold start and the network. The Supabase store adds a database write per step, and durable runs on pgflow add database round trips between steps, so use them for runs that must survive a worker, not for a fast answer.

### Where checkpoints live

By default, checkpoints go to two tables in your project. Create them once:

```bash
supie flow migration  # writes supabase/migrations/<timestamp>_supie_flows.sql
supabase db push
```

Only the service role can read or write those tables. For tests, pass `Flow("scoring", MemoryStore())` from `supie.flow`. The memory store only lasts as long as one worker, and hosted Supabase rarely reuses workers, so mounted flows need the tables.

A background run lives as long as its worker: up to 150 s on the free plan and 400 s on paid plans. Waiting on I/O, such as another function, doesn't count against the 2 s CPU budget. For longer runs, resume stale ones on a schedule:

```sql
select cron.schedule('resume-scoring', '* * * * *', $$
  select net.http_post(
    url := 'https://<project>.supabase.co/functions/v1/<function>/flows/scoring/resume-stale',
    headers := jsonb_build_object('Authorization', 'Bearer ' || '<service role key>')
  )
$$);
```

### Durable runs on pgflow

Inline runs live inside one worker. For runs that must outlive any worker, run the same flow on [pgflow](https://pgflow.dev): Postgres schedules each step, keeps every output, and retries failed steps; supie's workers claim and run them.

```bash
npx pgflow@0.17.2 install  # pgflow's schema and queues
supie flow migration --pgflow  # functions that let supie's workers reach pgflow
supabase db push
```

```python
scoring.mount(app, "/flows/scoring", durable=True)

# or from your own handler:
run_id = await scoring.start(application, request=req)
state = await scoring.status(run_id)
```

| Endpoint | What it does |
|---|---|
| `POST <prefix>` | Starts a run on pgflow, answers 202 with its `run_id`, and works on it after the response. |
| `GET <prefix>/<run_id>` | Returns the status, output and each step's state, including skipped steps and errors. |
| `POST <prefix>/work?seconds=60` | For the service role: claims and runs waiting tasks. Call it from pg_cron, so runs continue after a worker stops. |

- **The flow compiles itself.** On a worker's first run, supie sends the flow's shape to pgflow. A shape that differs from the one in the database is an error, so change a durable flow by giving it a new name.
- **Retries are pgflow's.** `retries` and `backoff` become pgflow's max attempts and base delay; a failed attempt goes back to the queue.
- **Names are pgflow's.** Flow and step names use letters, digits and single underscores. `goto` branches aren't supported; use `when=`.
- **Bridged over PostgREST.** pgflow's functions live in the `pgflow` and `pgmq` schemas, which PostgREST doesn't expose, and WebAssembly can't open a Postgres connection. The `--pgflow` migration adds `supie_pgflow_*` functions that only the service role can call.

- **pgflow's telemetry is on by default.** Its install schedules a daily job that sends anonymous, bucketed usage counts (runs, workers, which features are used) to telemetry.pgflow.dev. Turn it off with `select pgflow_telemetry.disable();`, or preview the payload with `select pgflow_telemetry.preview();`.
- **pgflow's install schedules cron jobs.** `pgflow_ensure_workers`, `pgflow_requeue_stalled_tasks` and `pgflow_cleanup_logs`, and it enables `pgmq`, `pg_cron` and `pg_net`.

Overhead: each level of steps costs three database calls (read, claim, complete). Locally, a chain of four no-op steps took 44 ms from start to completion by pgflow's timestamps, and four parallel steps feeding a fifth took the same, so parallel steps are free. On hosted Supabase each call also crosses the network to the database.

Tested against pgflow 0.17.2 locally and on hosted Supabase, with workers on MicroPython and Pyodide: parallel steps, a failed attempt retried by pgflow, a skipped route and the endpoints. Each hosted run took 4 to 7 s end to end.

## Queues

For work that must not be lost when a request fails, accept it fast and handle it from a queue. A failed attempt comes back after a backoff, and a message that keeps failing is kept, with its error, instead of disappearing:

```python
from supie import App, Request, Response
from supie.queue import Queue

app = App()
queue = Queue("scoring")


@app.post("/score")
async def score(req: Request) -> Response:
    await queue.send(req.json())  # durable once this returns
    req.background(queue.work(req))  # optional: start right away
    return Response({"queued": True}, status=202)


@queue.consumer(batch=10, max_attempts=5)
async def handle(ctx, message):
    ...


queue.mount(app, "/queues/scoring")  # work, stats and replay endpoints
```

```bash
supie queue migration  # pgmq, the supie_queue_* functions and the dead-letter table
supabase db push
```

When you update supie, run `supie queue migration` again and redeploy the functions in the same release: the SQL functions and the Python that calls them change together.

- **Delivery.** At least once. A message stays hidden for `visibility` seconds while it's handled, so keep that above the slowest handling; if a worker dies mid-message, it comes back after that.
- **Retries.** A failed attempt (an exception, or `timeout` passed) comes back after about `backoff`, then twice that, and so on up to `max_backoff` (300 s), each delay shortened by up to half at random so retries after an outage don't all land at once. `ctx.attempt` says which attempt this is.
- **Dead letters.** After `max_attempts`, the message moves to `supie_queue_dead` with its last error and attempt count. `POST <prefix>/dead/<id>/replay` sends it back.
- **Who handles messages.** `queue.work()` handles messages until the queue is idle: call it in the background after sending, and from pg_cron through `POST <prefix>/work` so nothing waits for the next request. The endpoint's `?seconds=` (60 by default) is capped at `mount(..., max_seconds=140)`, under the free plan's 150 s wall-clock limit; raise it on paid plans. With a consumer `timeout`, `work` doesn't start a batch that could run past its seconds.
- **Batches.** The messages in one read are handled concurrently, and the handled ones are acknowledged in one call.

### Nothing lost, nothing applied twice

A queue delivers each message at least once: nothing is lost once `send` returns, but a message can occasionally be handled twice (a worker finishes, then dies before acknowledging). Two tools make the result exactly once:

```python
await queue.send(application, key=f"application-{application['id']}")


@queue.consumer(max_attempts=5)
async def handle(ctx, message):
    await ctx.once(f"decide:{message['id']}", lambda: decide(message))
```

- **Send keys.** A message is enqueued once per key. A client that got no 202 can safely send again: the same key returns the first message's ID and enqueues nothing, even when the retries race each other. A message that goes to the dead letters takes its key along, so sending the key again enqueues it afresh, and replaying the dead letter then returns that message instead of adding a second.
- **`ctx.once(key, fn)`.** Runs `fn` once per key and stores its JSON result; any later delivery gets the stored result without running `fn`. While `fn` runs, the key holds a lease (`lease=`, five times the visibility by default; keep it above `fn`'s longest run), so a delivery arriving meanwhile comes back when the lease ends instead of running it in parallel, without using up an attempt. If `fn` raises or is cancelled, the lease is released and a retry runs it; a runner whose lease ran out can't release or finish the one that took over.
- **The one gap.** If a worker dies after `fn` finished but before its result was stored, the next delivery runs `fn` again. That can't be closed for effects outside the database (an email, a payment API), so give those their own idempotency key where the API offers one.

Tested with forced duplicates: a crash after the effect, a failed effect, concurrent deliveries of one effect, a message redelivered to a second worker mid-effect, and racing retries of one send, on pgmq and in the Edge Runtime on both runtimes. Keys and results stay in `supie_queue_keys` and `supie_queue_once`. `supie_queue_prune()` deletes those older than 7 days (pass `older_than`), and dead letters only when you pass `dead_older_than`. A key deduplicates sends for as long as it's kept: a send repeated after its key was pruned enqueues again. Schedule it with pg_cron:

```sql
select cron.schedule('supie-queue-prune', '0 3 * * *', 'select public.supie_queue_prune()');
```

Messages live in pgmq, the engine behind Supabase Queues. Like the pgflow bridge, supie reaches it through `public` functions only the service role can call, so you don't need to expose the `pgmq_public` schema. For tests, `Queue("scoring", MemoryQueue())` keeps messages in memory.

## Testing

Handlers are plain Python, so you test them with pytest and no Supabase at all:

```python
from supie.testing import FakeResponse, TestClient

from main import app


def test_score():
    client = TestClient(app, env={"SCORING_KEY": "test"}, fetch=lambda call: FakeResponse([]))
    response = client.post("/score", json={"age": 40})
    assert response.status == 201
    assert client.calls == []  # every request the handler made through Supabase
```

To test a flow outside a request, `supie.testing.run(flow.run(input), sleep_scale=0)` binds a fake host and skips retry delays.

`TestClient` runs background tasks after each response. Its `fetch` answers the Supabase client's requests, and `client.calls` records them. By default, an unexpected error fails the test with its traceback instead of returning a 500.

Tests run under CPython. A handler that passes them can still use something MicroPython lacks, so run `supie serve` before you deploy, or choose Pyodide.

## Choosing a runtime

| | MicroPython (default) | Pyodide |
|---|---|---|
| Cold boot | about 20 ms | about 300 ms |
| Python | most of the language | CPython 3.14 |
| Standard library | partial: json, math, re, datetime, random, hashlib, base64, collections, itertools, functools, asyncio and more, but no statistics or dataclasses | complete |
| Speed of Python loops | about half of Pyodide | |

Payload sizes, on the Edge Runtime with the hosted limits (2 s of CPU, 256 MB), for JSON rows:

| | parse a JSON body | return a JSON body |
|---|---|---|
| MicroPython (16 MB heap) | up to 10 MB in 0.5 s | 1 MB in 1.1 s; 5 MB runs out of CPU |
| Pyodide | up to 20 MB in 0.5 s | up to 10 MB in 0.5 s |

MicroPython parses JSON quickly but encodes it at about a second per megabyte, so return large payloads from Pyodide. Raise MicroPython's heap with `heap_mb` for request bodies over about 10 MB.

MicroPython has no `typing` module, so supie ships a stand-in where every hint does nothing. Annotated code and `from __future__ import annotations` work on both runtimes.

Pick Pyodide per function in `supie.toml`:

```toml
[functions.heavy]
runtime = "pyodide"
preload = ["statistics", "decimal"]  # imported into the snapshot, so boot doesn't pay for them
```

### Packages

A Pyodide function can use packages from PyPI: list them in a `requirements.txt` next to `main.py`, with pinned versions.

```text
pydantic==2.12.5  # Pyodide's build, with its compiled core
openpyxl==3.1.5  # pure Python, from PyPI
```

`supie build` installs them with micropip under Deno and ships them in the function. Pure-Python packages come from PyPI. Packages with compiled code only work if Pyodide builds them, such as pydantic, numpy and pandas, and those come from Pyodide's CDN. MicroPython functions can't use packages; put pure-Python modules in the function's folder instead.

Packages cost boot time, and a cold start must fit in the 2 s CPU budget. Preload them, and the snapshot holds them already imported:

```toml
[functions.validate]
runtime = "pyodide"
preload = ["pydantic", "openpyxl", "main"]  # "main" also bakes in your models
```

pydantic and openpyxl, validating rows:

| | cold boot, Edge Runtime on a Mac | hosted Supabase, 20 requests |
|---|---|---|
| imported at boot | 0.72 s | half failed on the CPU limit |
| packages preloaded | 0.42 s | all succeeded, median 1.6 s end to end |
| packages and `main` preloaded | 0.20 s | all succeeded, median 1.2 s end to end |

Preloading works for compiled extensions too. A snapshot copies the interpreter's memory but not the extensions it loaded, such as pydantic's core, so supie records where each one was loaded and loads it there again before restoring. That takes a small patch to Pyodide's loader, which `supie build` applies and checks against the pinned Pyodide version.

Preloading your own module (`"main"` here) runs its module-level code at build time and keeps the result: pydantic models, lookup tables, a loaded model. Never read secrets at module level in a preloaded module, or they end up in the snapshot file; read them in `on_boot`.

The cost is size. pydantic preloaded makes the function about 18 MB of Supabase's 20 MB limit, so heavier stacks such as pandas won't fit.

Other settings: `verify_jwt` (default `true`; turned off, anyone can send any claims in `req.user`, so authorize with `await req.check_service()` or your own checks) and, for MicroPython, `heap_mb` (default 16). A `[project]` table can move the source folder (`source = "py"`) or the Supabase folder (`supabase = "supabase"`).

## How it works

`supie build` writes a small TypeScript shim, the runtime and your Python files into the function folder. On a cold worker the shim boots Python, loads your files and imports `main`. For each request, it passes the method, path, query, headers and body to supie as JSON, and turns the answer back into a `Response`.

- **Fetching runtimes.** They come from npm at pinned versions, are checked against npm's integrity hashes, and are cached in `~/.cache/supie`.
- **Pyodide snapshot.** Pyodide boots from a memory snapshot of an already-initialized interpreter, built with Deno and cached.
- **Calls out.** Python reaches Supabase through the shim's `fetch`, since WebAssembly has no sockets.

Two workarounds are built in: Supabase's runtime refuses shared WebAssembly memory, and it doesn't define `setImmediate`. The prototype README explains both. For snapshots that hold compiled extensions, supie also patches where Pyodide's loader places them, as described under Packages.

## Limitations

- **Bundle size caps Pyodide stacks.** Pyodide plus pydantic, preloaded, is about 18 MB of the 20 MB limit.
- **No direct Postgres connection.** Use the Supabase client, or an RPC for raw SQL.
- **Text request bodies only.** Binary request bodies are not supported yet. Responses and Storage handle bytes.
- **Inline flows run inside one worker.** A worker that stops mid-run leaves the run for `resume-stale`. Use durable runs on pgflow for runs that must outlive a worker.

## Development

All of supie's Python is annotated, including the modules that run in the function: annotations are never evaluated, so they cost MicroPython nothing. ruff requires annotations in `src/` and `scripts/`, and ty checks them. Both run as hooks and in CI.

```bash
uv sync
uv run pytest -m "not edge and not postgres"  # unit tests
uv run pytest -m "edge or postgres"  # Edge Runtime, Postgres, PostgREST and pgflow, needs Docker
uv run ty check src tests scripts examples  # types
```

CI runs the Docker tests against pinned images. A weekly workflow runs them against the images the Supabase CLI ships that day, so a new Edge Runtime, Postgres or PostgREST release that breaks supie shows up before users hit it.
