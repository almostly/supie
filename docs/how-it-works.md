# How it works

## Architecture

`supie build` writes a small TypeScript shim, the runtime and your Python files into the function folder. On a cold worker the shim boots Python, loads your files and imports `main`. For each request, it passes the method, path, query, headers and body to supie as JSON, and turns the answer back into a `Response`.

- **Fetching runtimes.** They come from npm at pinned versions, are checked against npm's integrity hashes, and are cached in `~/.cache/supie`.
- **Pyodide snapshot.** Pyodide boots from a memory snapshot of an already-initialized interpreter, built with Deno and cached.
- **Calls out.** Python reaches Supabase through the shim's `fetch`, since WebAssembly has no sockets.

Two workarounds are built in: Supabase's runtime refuses shared WebAssembly memory, and it doesn't define `setImmediate`. The [prototype README](../prototype/README.md) explains both. For snapshots that hold compiled extensions, supie also patches where Pyodide's loader places them, as described under [Packages](runtimes.md#packages).


## Limitations

- **Bundle size caps Pyodide stacks.** Pyodide plus pydantic, preloaded, is about 18 MB of the 20 MB limit.
- **No direct Postgres connection.** Use the Supabase client, or an RPC for raw SQL.
- **Text request bodies only.** Binary request bodies are not supported yet. Responses and Storage handle bytes.
- **Inline flows run inside one worker.** A worker that stops mid-run leaves the run for `resume-stale`. Use durable runs on pgflow for runs that must outlive a worker.
