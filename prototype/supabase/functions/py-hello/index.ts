// Supabase Edge Function that runs a Python handler through Pyodide.
//
// Pyodide is vendored in ./pyodide and shipped as static files, so nothing is
// fetched from a CDN at runtime. The interpreter boots once per worker at
// module scope; every request after that reuses it.

import process from "node:process";
// Must run before Pyodide is evaluated.
import "./compat.ts";
// Static import so the bundler sees the Emscripten module. Pyodide would
// otherwise import it from a computed path, which bundlers can't follow.
import createPyodideModule from "./pyodide/pyodide.asm.mjs";
import { loadPyodide } from "./pyodide/pyodide.mjs";

// edge-runtime's CPU limiter reads CLOCK_THREAD_CPUTIME_ID of the isolate
// thread, so V8's background compiler threads are not billed. On Linux we
// read the main thread's schedstat to match that; elsewhere we fall back to
// whole-process CPU, which overstates it.
const mainTid = Deno.pid;
const threadCpuMs = (): number | null => {
  try {
    const s = Deno.readTextFileSync(`/proc/self/task/${mainTid}/schedstat`);
    return Number(s.split(" ")[0]) / 1e6;
  } catch {
    return null;
  }
};
const processCpuMs = () => {
  const u = process.cpuUsage();
  return (u.user + u.system) / 1000;
};
const cpuMs = () => threadCpuMs() ?? processCpuMs();
const memMb = () => {
  const m = Deno.memoryUsage();
  return {
    // edge-runtime enforces its memory limit on used heap + external memory
    heap_plus_external: +((m.heapUsed + m.external) / 2 ** 20).toFixed(1),
    rss: +(m.rss / 2 ** 20).toFixed(1),
  };
};

// import.meta.dirname is how edge-runtime resolves bundled static files.
const here = `${import.meta.dirname ?? new URL(".", import.meta.url).pathname}/`
  .replace(/\/+$/, "/");
const indexURL = `${here}pyodide/`;
// Optional: path to a Pyodide memory snapshot (raw or .gz), relative to this
// folder or absolute. See bench/make_snapshot.ts.
const snapshotEnv = Deno.env.get("PY_SNAPSHOT");
const snapshotPath = snapshotEnv && !snapshotEnv.startsWith("/")
  ? `${here}${snapshotEnv}`
  : snapshotEnv;

async function readSnapshot(path: string): Promise<Uint8Array> {
  if (!path.endsWith(".gz")) return await Deno.readFile(path);
  // Bundled static files can be read whole but not opened as streams.
  const stream = new Blob([await Deno.readFile(path)]).stream()
    .pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

const cpu0 = cpuMs();
const pcpu0 = processCpuMs();
const t0 = performance.now();

const lockFileContents = await Deno.readTextFile(`${indexURL}pyodide-lock.json`);
const pyodide = await loadPyodide({
  indexURL,
  createPyodideModule,
  lockFileContents,
  stdout: () => {},
  ...(snapshotPath ? { _loadSnapshot: readSnapshot(snapshotPath) } : {}),
});
const tInterp = performance.now();

pyodide.runPython(await Deno.readTextFile(`${here}handler.py`));
pyodide.runPython("import json as _json");
const pyHandle = pyodide.globals.get("handle");
const pyLoads = pyodide.globals.get("_json").loads;
const pyDumps = pyodide.globals.get("_json").dumps;

const boot = {
  wall_ms: +(performance.now() - t0).toFixed(1),
  interp_wall_ms: +(tInterp - t0).toFixed(1),
  cpu_ms: +(cpuMs() - cpu0).toFixed(1),
  process_cpu_ms: +(processCpuMs() - pcpu0).toFixed(1),
  // main-thread CPU since process start, incl. runtime startup and module
  // parsing: an upper bound on what edge-runtime would bill for boot
  thread_cpu_since_start_ms: +(cpuMs()).toFixed(1),
  snapshot: !!snapshotPath,
  mem_mb: memMb(),
};
console.log(JSON.stringify({ event: "boot", ...boot }));

let requests = 0;

// PORT only matters when running under plain Deno; edge-runtime ignores it.
const port = Number(Deno.env.get("PORT") ?? 8000);

Deno.serve({ port }, async (req: Request) => {
  const c0 = cpuMs();
  const w0 = performance.now();
  const url = new URL(req.url);
  const body = req.method === "POST" ? await req.text() : "";

  const reqJson = JSON.stringify({
    method: req.method,
    path: url.pathname,
    query: Object.fromEntries(url.searchParams),
    body: body ? JSON.parse(body) : null,
  });

  let status = 200;
  let out: string;
  const pyReq = pyLoads(reqJson);
  try {
    const result = pyHandle(pyReq);
    out = pyDumps(result);
    result?.destroy?.();
  } catch (err) {
    status = 500;
    out = JSON.stringify({ error: String(err) });
  } finally {
    pyReq.destroy();
  }

  requests += 1;
  const meta = {
    request_no: requests,
    req_cpu_ms: +(cpuMs() - c0).toFixed(2),
    req_wall_ms: +(performance.now() - w0).toFixed(2),
    boot,
    mem_mb: memMb(),
  };
  return new Response(`{"result":${out},"_meta":${JSON.stringify(meta)}}`, {
    status,
    headers: { "content-type": "application/json" },
  });
});
