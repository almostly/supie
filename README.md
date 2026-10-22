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

`req.supabase` acts as the caller, forwarding their token so row level security applies. `req.supabase_admin` uses the service role key and bypasses it. Both read `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY`, which Supabase sets for every function.

The query builder covers `select`, `insert`, `upsert`, `update`, `delete`, the filters `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `ilike`, `is_` and `in_`, plus `filter`, `order`, `limit`, `offset` and `single`. A failed call raises `supie.supabase.SupabaseError` with the status and message.

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

Other settings: `verify_jwt` (default `true`) and, for MicroPython, `heap_mb` (default 16). A `[project]` table can move the source folder (`source = "py"`) or the Supabase folder (`supabase = "supabase"`).

## How it works

`supie build` writes a small TypeScript shim, the runtime and your Python files into the function folder. On a cold worker the shim boots Python, loads your files and imports `main`. For each request, it passes the method, path, query, headers and body to supie as JSON, and turns the answer back into a `Response`.

- **Fetching runtimes.** They come from npm at pinned versions, are checked against npm's integrity hashes, and are cached in `~/.cache/supie`.
- **Pyodide snapshot.** Pyodide boots from a memory snapshot of an already-initialized interpreter, built with Deno and cached.
- **Calls out.** Python reaches Supabase through the shim's `fetch`, since WebAssembly has no sockets.

Two workarounds are built in: Supabase's runtime refuses shared WebAssembly memory, and it doesn't define `setImmediate`. The prototype README explains both.

## Limitations

- **No PyPI packages yet.** Ship pure-Python modules by putting them in the function's folder.
- **No direct Postgres connection.** Use the Supabase client, or an RPC for raw SQL.
- **Text request bodies only.** Binary request bodies are not supported yet. Responses and Storage handle bytes.
- **No watch mode.** `supie serve` doesn't rebuild on file changes yet.

## Development

```bash
uv sync
uv run pytest -m "not edge"   # unit tests
uv run pytest -m edge         # functions on the Edge Runtime image, needs Docker
```
