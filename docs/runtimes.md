# Runtimes and packages

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

## Packages

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
