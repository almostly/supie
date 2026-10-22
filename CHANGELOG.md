# Changelog

## v0.1.0 (unreleased)

### Added

- `App` with routes, typed path parameters, `on_boot`, background tasks, `Request`, `Response`, `HTTPError` and `env`.
- An async Supabase client for handlers: table queries, RPC, Edge Function calls and Storage, acting as the caller (`req.supabase`) or with the service role (`req.supabase_admin`).
- Two runtimes: MicroPython (default, about 20 ms cold boot) and Pyodide (CPython 3.14, booting from a memory snapshot).
- `supie init`, `build`, `serve` and `deploy`.
- `supie.testing.TestClient` for testing handlers under CPython with a fake environment and fetch.
- Edge Runtime fixes built into the generated shim: shared WebAssembly memory and `setImmediate` for Pyodide, the Node file path and `Buffer` for MicroPython.
- `typing` and `__future__` stand-ins for MicroPython.
- Workflows (`supie.flow`): steps with dependencies, parallel execution, retries, timeouts and `goto` branches, checkpointed per run in Supabase tables (`supie flow migration`) or in memory, with endpoints to start, inspect and resume runs.
- `gather`, `wait_for` and `sleep` for concurrency inside handlers.
- Packages for Pyodide functions from `requirements.txt`: pure-Python wheels from PyPI and Pyodide's prebuilt packages from its CDN, installed at build time and cached.
- Snapshots that hold compiled extensions such as pydantic-core, restored by loading each extension where it was before the memory is copied back. `preload` can also name the function's own modules. On hosted Supabase, pydantic and openpyxl went from half the cold starts failing to all succeeding.
- `supie build` reports the compressed size, close to what Supabase counts against the bundle limit.
- ONNX models (`supie.onnx`), run by the Edge Runtime's native ONNX engine and loaded from Storage or a URL; under CPython they run with onnxruntime.
- `supie model prepare` removes ZipMap outputs from classifier exports, which the engine can't return.
- `compare_digest` for constant-time comparison on every runtime, since MicroPython's `hmac` lacks it.
- `supie.supabase.RateLimited` when Supabase refuses a call between functions (30 per trace and minute), instead of an untyped error and a 500.
