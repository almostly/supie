# Prototype: Python in a Supabase Edge Function

Two Supabase Edge Functions whose request handlers are written in Python. A thin TypeScript shim boots the interpreter once per worker and passes each request to `handle(req: dict) -> dict`.

- `py-hello` runs Pyodide 314.0.7, which is CPython 3.14 on WebAssembly, with packages.
- `mpy-hello` runs MicroPython 1.29, a much smaller interpreter covering most of the language with a small standard library.
- `ts-hello` does the same work in TypeScript, as the baseline.

This is the experiment supie grows out of. It runs on Supabase's real Edge Runtime under the hosted limits.

## Layout

```
fetch_runtimes.sh              vendors Pyodide and MicroPython from npm, --snapshot builds the snapshot
supabase/
  config.toml                  per_worker policy, static_files for Pyodide
  functions/snapshot.env       PY_SNAPSHOT=snapshot.bin.gz (omit to boot cold)
  functions/py-hello/
    index.ts                   shim: boot Pyodide, dispatch requests, report timings
    compat.ts                  lets Pyodide start under the Edge Runtime (see below)
    handler.py                 the Python code
    pyodide/                   fetched: wasm, stdlib zip, lockfile
    snapshot.bin.gz            fetched: pre-booted interpreter memory, about 6 MB
  functions/mpy-hello/
    index.ts                   shim for MicroPython
    handler.py                 the same work, without the statistics module
    micropython/               fetched: wasm and loader, under 0.5 MB
  functions/ts-hello/index.ts  the same handler in TypeScript
edge-runtime/index.ts          main service for running the Edge Runtime image directly
bench/
  bench_edge.py                cold and warm timings against the Edge Runtime container
  bench.ts                     cold and warm timings under plain Deno
  make_snapshot.ts             builds the snapshot
```

## Setup

```bash
./fetch_runtimes.sh --snapshot
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

python3 bench/bench_edge.py py-hello 7
python3 bench/bench_edge.py mpy-hello 7
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

## MicroPython fixes

MicroPython's loader takes its Node code path under the Edge Runtime and reads the wasm with Node's fs. Two things follow:

- **Pass a file path.** A blob URL gets treated as a file name and fails, so the shim passes the wasm's path.
- **Define Buffer.** The loader expects a global `Buffer`, which the runtime doesn't define, so the shim sets it from `node:buffer`.

MicroPython needs no shared memory, so it doesn't need `compat.ts`.

## Results

Edge Runtime v1.77.4 in Docker on an Apple Silicon Mac, with 2 s CPU and 256 MB limits, 7 cold workers per mode:

| | cold boot, wall | warm request, wall | memory (heap+external) |
|---|---|---|---|
| Pyodide, plain | 0.86 to 0.97 s | 0.21 ms | 71 MB |
| Pyodide, snapshot (.gz) | 0.12 to 0.13 s | 0.24 ms | 102 MB |
| MicroPython | 4.3 to 5.7 ms | 0.18 ms | 32 MB |

A 1M-iteration Python loop takes about 0.17 s on Pyodide and 0.52 s on MicroPython. A 30M-iteration loop is cancelled by the runtime's CPU limit on both.

MicroPython boots about 26 times faster than the Pyodide snapshot but runs Python loops about 3 times slower. Hosted Supabase boots a fresh worker for nearly every request, so MicroPython wins for handlers that do little computation.

### Compared with TypeScript

The same container, timed from the client, so a cold request includes creating the worker and loading the module:

| | cold request | warm request | 1M-iteration loop | 10M-iteration loop |
|---|---|---|---|---|
| TypeScript | 14.8 ms | 0.98 ms | 4 ms | 35 ms |
| MicroPython | 25.2 ms | 1.50 ms | 668 ms | cancelled by the CPU limit |
| Pyodide, snapshot | 164.5 ms | 1.34 ms | 175 ms | 1757 ms |

- **Startup and request handling are close.** MicroPython adds about 10 ms to a cold request and about 0.5 ms to a warm one.
- **Computation is not.** V8 compiles the TypeScript loop to machine code, so it runs about 45 times faster than Pyodide and about 170 times faster than MicroPython.

The runtime doesn't expose CPU counters to the worker, so `cpu_ms` reads 0 there and wall time is the measure. Boot runs on the isolate thread, so its CPU is at most its wall time. Plain boots therefore fit the 2 s budget on this machine, and snapshot boots fit with a wide margin.

Earlier numbers from plain Deno 2.9.6 in a 2-vCPU Linux sandbox, where thread CPU could be read:

| | cold boot, thread CPU | cold boot, wall | warm request, wall | memory |
|---|---|---|---|---|
| plain | 2.0 to 2.2 s | 2.2 to 2.6 s | 0.4 ms | 36 MB |
| snapshot (.gz) | 0.30 to 0.39 s | 0.32 to 0.38 s | 0.5 ms | 68 MB |

### Hosted Supabase

Deployed to a hosted project in eu-west-1 on 2026-10-22 with `supabase functions deploy`. The CLI reported a 12 MB bundle, with the snapshot included.

| | requests | cold boots | cold boot, wall | failures | memory |
|---|---|---|---|---|---|
| snapshot (.gz) | 30 | 28 | 0.25 to 0.41 s, median 0.30 s | 0 | 103 MB |
| plain | 12 | 12 | 1.83 to 1.94 s | 5 | 67 MB |

- **Workers were rarely reused.** 28 of 30 sequential requests booted a fresh worker, so on hosted the cold boot is the normal path, not the exception.
- **Plain boots don't fit.** They sit right at the 2 s CPU budget, and the failures returned `WORKER_RESOURCE_LIMIT` (HTTP 546). The snapshot is required.
- **Hosted CPU is slower.** A 1M-iteration Python loop took 0.61 s, about 3 times longer than the Mac above. A 30M-iteration loop failed with `WORKER_RESOURCE_LIMIT`.
- **Round trips.** From a client in Europe, a cold request took about 0.7 s end to end and a warm one about 0.35 s.

MicroPython on the same project, deployed the same way. The CLI reported a 221 kB bundle.

| | requests | cold boots | cold boot, wall | cold request, end to end | memory |
|---|---|---|---|---|---|
| Pyodide, snapshot | 30 | 28 | 0.25 to 0.41 s, median 0.30 s | median 0.74 s | 103 MB |
| MicroPython | 30 | 30 | 14 to 39 ms, median 19 ms | median 0.18 s | 31 MB |

| Python loop on hosted | Pyodide | MicroPython |
|---|---|---|
| 100k iterations | | 0.19 s |
| 1M iterations | 0.61 s | 1.24 s |
| 3M iterations | | `WORKER_RESOURCE_LIMIT` |

- **MicroPython is the fast path.** Every request got a fresh worker, and the whole cold request still finished in about 0.18 s.
- **Compute is the limit.** MicroPython runs Python loops about 2 times slower than Pyodide on hosted, so around 1.5M simple iterations is the practical ceiling per request.
