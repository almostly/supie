# Handlers and the Supabase client

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
- **Requests.** A request has `method`, `path`, `query`, `headers` (lower case), `params`, `text()` and `json()`. `json()` answers 400 for a missing or invalid body. `req.user` holds the caller's JWT claims, and `req.token` the raw token. The claims are decoded without verifying them: Supabase's gateway checks the signature only while the function keeps `verify_jwt` on. `await req.check_service()` is true only for a service-level key. A secret API key (`sb_secret_...`), sent in the `apikey` header as Supabase recommends, passes if it matches one of the project's secret keys, which hosted Supabase passes to the function in `SUPABASE_SECRET_KEYS`. A legacy service role key passes if it is the function's own, and another token that claims the role must be accepted by Supabase Auth, which verifies its signature. supie's work, replay and resume endpoints check it, so a token that merely claims the service role is refused. `await req.verified_user()` returns a signed-in caller's claims after Supabase Auth has verified the token, and raises 401 for a token Auth refuses and 503 when Auth can't be reached. Verified tokens are trusted until they expire, for at most an hour, and refused ones are remembered for a minute. Expired tokens, tokens issued for another project and tokens over 4 KB are refused without asking Auth, and each worker checks at most 16 tokens at once.
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
