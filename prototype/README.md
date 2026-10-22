# Prototype: Python in a Supabase Edge Function

A Supabase Edge Function whose request handler is written in Python (`supabase/functions/py-hello/handler.py`). A thin TypeScript shim boots Pyodide 314.0.7 (CPython 3.14 on WebAssembly) once per worker and passes each request to `handle(req: dict) -> dict`.

This is the experiment supie grows out of. It runs on Supabase's real Edge Runtime under the hosted limits.

## Layout

```
fetch_pyodide.sh               vendors Pyodide from npm, --snapshot also builds the snapshot
supabase/
  config.toml                  per_worker policy, static_files for Pyodide
  functions/snapshot.env       PY_SNAPSHOT=snapshot.bin.gz (omit to boot cold)
  functions/py-hello/
    index.ts                   shim: boot Pyodide, dispatch requests, report timings
    compat.ts                  lets Pyodide start under the Edge Runtime (see below)
    handler.py                 the Python code
    pyodide/                   fetched: wasm, stdlib zip, lockfile
    snapshot.bin.gz            fetched: pre-booted interpreter memory, about 6 MB
edge-runtime/index.ts          main service for running the Edge Runtime image directly
bench/
  bench_edge.py                cold and warm timings against the Edge Runtime container
  bench.ts                     cold and warm timings under plain Deno
  make_snapshot.ts             builds the snapshot
```

## Setup

```bash
./fetch_pyodide.sh --snapshot
```

This needs npm. Deno runs through npx, so nothing else gets installed.

## Run on the Edge Runtime image

This skips the Supabase CLI, so it doesn't clash with other local Supabase stacks. The main service applies the hosted limits: 2 s CPU and 256 MB memory.

```bash
docker run -d --name supie-edge -p 127.0.0.1:9123:9000 \
  -v "$PWD/supabase/functions":/home/deno/functions:ro \
  -v "$PWD/edge-runtime":/home/deno/main:ro \
  public.ecr.aws/supabase/edge-runtime:v1.77.4 start --main-service /home/deno/main

curl -s -XPOST http://127.0.0.1:9123/py-hello -H "x-snapshot: 1" \
  -d '{"name":"Denis","xs":[2,4,4,4,5,5,7,9],"n":100000}'

python3 bench/bench_edge.py 7
```

Send `x-cold: 1` to force a fresh worker.

## Run with the Supabase CLI

```bash
supabase start
supabase functions serve --env-file supabase/functions/snapshot.env
curl -s -XPOST http://127.0.0.1:54321/functions/v1/py-hello -d '{"n":100000}'
```

The CLI's local CPU limits are looser than hosted (10 s soft, 20 s hard), so the container above is the better check. Deploy with `supabase functions deploy py-hello` and watch the bundle size it reports. The limit is 20 MB.

## The shared memory fix

The Edge Runtime refuses to create shared WebAssembly memory, while plain Deno allows it. Pyodide 314 creates one shared page at startup, only to sleep with `Atomics.wait` while stdin returns EAGAIN, and it already handles that call failing. Without a fix, `loadPyodide` fails with this error:

```
TypeError: Creating a shared memory is not supported
```

`compat.ts` wraps `WebAssembly.Memory` to fall back to ordinary memory when shared memory is refused. It must be imported before Pyodide.

A second difference: bundled static files can be read whole with `Deno.readFile` but not opened as streams with `Deno.open`, so the shim reads the snapshot whole before decompressing it.

## Results

Edge Runtime v1.77.4 in Docker on an Apple Silicon Mac, with 2 s CPU and 256 MB limits, 7 cold workers per mode:

| | cold boot, wall | warm request, wall | memory (heap+external) |
|---|---|---|---|
| plain | 0.86 to 0.97 s | 0.21 ms | 71 MB |
| snapshot (.gz) | 0.12 to 0.13 s | 0.24 ms | 102 MB |

A 1M-iteration Python loop takes about 0.19 s. A 30M-iteration loop is cancelled by the runtime's CPU limit.

The runtime doesn't expose CPU counters to the worker, so `cpu_ms` reads 0 there and wall time is the measure. Boot runs on the isolate thread, so its CPU is at most its wall time. Plain boots therefore fit the 2 s budget on this machine, and snapshot boots fit with a wide margin.

Earlier numbers from plain Deno 2.9.6 in a 2-vCPU Linux sandbox, where thread CPU could be read:

| | cold boot, thread CPU | cold boot, wall | warm request, wall | memory |
|---|---|---|---|---|
| plain | 2.0 to 2.2 s | 2.2 to 2.6 s | 0.4 ms | 36 MB |
| snapshot (.gz) | 0.30 to 0.39 s | 0.32 to 0.38 s | 0.5 ms | 68 MB |

Hosted hardware is unknown, so treat the snapshot as required for production.
