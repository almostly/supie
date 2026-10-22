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

## Install

```bash
uv tool install git+https://github.com/almostly/supie
```

You also need the [Supabase CLI](https://supabase.com/docs/guides/cli). The Pyodide runtime needs Deno or Node.js at build time, to build its snapshot.

## Quickstart

In a Supabase project (run `supabase init` first if you don't have one):

```bash
supie init hello      # py/hello/main.py and py/hello/test_main.py
pytest py/hello       # handlers are plain Python
supie build           # writes supabase/functions/hello/
supie serve           # supabase functions serve, after building
supie deploy          # supabase functions deploy, after building
```

Your Python lives in `py/<name>/`. `supie build` turns it into `supabase/functions/<name>/` and adds the function to `supabase/config.toml`. Treat the generated folder like a `dist/` directory: edit `py/`, never the output. supie refuses to overwrite a function it didn't generate.

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
- **Requests.** A request has `method`, `path`, `query`, `headers` (lower case), `params`, `text()` and `json()`. `json()` answers 400 for a missing or invalid body. `req.user` holds the caller's JWT claims, and `req.token` the raw token.
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

Classifier exports from CatBoost, scikit-learn and others end in a ZipMap, which the engine can't return. Remove it before uploading:

```bash
pip install 'supie[models]'
supie model prepare pd.onnx pd-prepared.onnx
```

A CatBoost model of 1.6 MB, from a private bucket on hosted Supabase:

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
- **Branches.** A step returns `ctx.goto(branch)` to hand its result to a branch. Branches run only when a step goes to them, like a Choice state.
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

### Where checkpoints live

By default, checkpoints go to two tables in your project. Create them once:

```bash
supie flow migration   # writes supabase/migrations/<timestamp>_supie_flows.sql
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
pydantic==2.12.5   # Pyodide's build, with its compiled core
openpyxl==3.1.5    # pure Python, from PyPI
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

Other settings: `verify_jwt` (default `true`) and, for MicroPython, `heap_mb` (default 16). A `[project]` table can move the source folder (`source = "py"`) or the Supabase folder (`supabase = "supabase"`).

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
- **No watch mode.** `supie serve` doesn't rebuild on file changes yet.
- **Flows run inside one worker.** A worker that stops mid-run leaves the run for `resume-stale`. A pgflow-backed mode, where Postgres schedules each step, is planned.

## Development

```bash
uv sync
uv run pytest -m "not edge and not postgres"   # unit tests
uv run pytest -m "edge or postgres"   # Edge Runtime, Postgres and PostgREST images, needs Docker
```

CI runs the Docker tests against pinned images. A weekly workflow runs them against the images the Supabase CLI ships that day, so a new Edge Runtime, Postgres or PostgREST release that breaks supie shows up before users hit it.
