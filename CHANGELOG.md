# Changelog

## v0.1.1 (2026-10-23)

### Fixed

- Service-only endpoints accept the service role key on hosted Supabase again. 0.1.0 compared the caller's token with the function's `SUPABASE_SERVICE_ROLE_KEY` string, and on hosted Supabase a valid service role key can be another string, so the queue's work, stats and replay endpoints and the flows' resume and work endpoints answered 403. `await req.check_service()` passes the function's own key at once and checks any other token claiming the service role with Supabase Auth, which verifies its signature, so forged tokens stay refused. Verified on hosted Supabase with `verify_jwt` off.

## v0.1.0 (2026-10-23)

### Added

- `App` with routes, typed path parameters, `on_boot`, background tasks, `Request`, `Response`, `HTTPError` and `env`.
- An async Supabase client for handlers: table queries, RPC, Edge Function calls and Storage, acting as the caller (`req.supabase`) or with the service role (`req.supabase_admin`).
- Two runtimes: MicroPython (default, about 20 ms cold boot) and Pyodide (CPython 3.14, booting from a memory snapshot).
- `supie init`, `build`, `serve` and `deploy`.
- `req.is_service`, true only when the caller sent exactly the function's service role key, compared in constant time.
- `supie.testing.TestClient` for testing handlers under CPython with a fake environment and fetch.
- Edge Runtime fixes built into the generated shim: shared WebAssembly memory and `setImmediate` for Pyodide, the Node file path and `Buffer` for MicroPython.
- `typing` and `__future__` stand-ins for MicroPython.
- Workflows (`supie.flow`): steps with dependencies, parallel execution, retries, timeouts and `goto` branches, checkpointed per run in Supabase tables (`supie flow migration`) or in memory, with endpoints to start, inspect and resume runs.
- `gather`, `wait_for` and `sleep` for concurrency inside handlers.
- Durable flow runs on pgflow (`mount(..., durable=True)`, `flow.start`, `flow.work`, `flow.status`): Postgres schedules each step and retries failures, and workers claim tasks through `supie_pgflow_*` functions (`supie flow migration --pgflow`).
- Queues (`supie.queue`) on pgmq: `queue.send`, `@queue.consumer` with batches acknowledged in one call, retries with capped, jittered backoff and a dead-letter table, replay, stats and mounted work endpoints whose `?seconds=` is capped under the Edge Functions' wall-clock limit (`supie queue migration`).
- `supie_queue_prune()` deletes old send keys and finished effects, and old dead letters when asked; schedule it with pg_cron.
- Exactly-once results on queues: `send(..., key=...)` enqueues a message once per key, even when batches with overlapping keys race, and a dead letter takes its key along so a resend or replay enqueues it once. `ctx.once(key, fn)` runs an effect once per key under a lease that only its holder can finish or release, and replays its stored result; a delivery that finds the lease taken waits for it without using up an attempt.
- `when=` and `unless=` conditions on steps, with pgflow's semantics, in inline and durable runs.
- Type annotations throughout, checked by ty and enforced by ruff's annotation rules.
- Requests and responses cross between the shim and Python as JSON metadata, a newline and the body as is, instead of the body nested in JSON. Unescaping and escaping the nested body cost MicroPython about a second per megabyte; it now parses 10 MB in 0.5 s.
- Pyodide's asyncio callbacks run through a MessageChannel instead of `setTimeout(0)`, whose clamping made every `await` cost milliseconds: a six-step inline flow went from 19 ms to under 1 ms on a Mac.
- Packages for Pyodide functions from `requirements.txt`: pure-Python wheels from PyPI and Pyodide's prebuilt packages from its CDN, installed at build time and cached.
- Snapshots that hold compiled extensions such as pydantic-core, restored by loading each extension where it was before the memory is copied back. `preload` can also name the function's own modules. On hosted Supabase, pydantic and openpyxl went from half the cold starts failing to all succeeding.
- `supie build` reports the compressed size, close to what Supabase counts against the bundle limit.
- ONNX models (`supie.onnx`), run by the Edge Runtime's native ONNX engine and loaded from Storage or a URL; under CPython they run with onnxruntime.
- `supie model prepare` removes ZipMap outputs from classifier exports, which the engine can't return.
- `supie serve` rebuilds a function when its Python changes, while `supabase functions serve` runs.
- `supie model publish` takes an ONNX file, an MLflow model folder or an MLflow URI such as `models:/pd@champion`, prepares it and uploads it to a private Storage bucket.
- `compare_digest` for constant-time comparison on every runtime, since MicroPython's `hmac` lacks it.
- `supie.supabase.RateLimited` when Supabase refuses a call between functions (30 per trace and minute), instead of an untyped error and a 500.
