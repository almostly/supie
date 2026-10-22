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
