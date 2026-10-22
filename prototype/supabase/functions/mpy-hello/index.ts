// Supabase Edge Function that runs a Python handler through MicroPython.
//
// MicroPython's WebAssembly build is under 0.5 MB, so a fresh worker has
// little to load. The interpreter boots once per worker at module scope.

import { Buffer } from "node:buffer";
import { loadMicroPython } from "./micropython/micropython.mjs";

// The loader takes its Node path here and reads the wasm with Node's fs,
// which expects a global Buffer that the Edge Runtime doesn't define.
(globalThis as { Buffer?: typeof Buffer }).Buffer ??= Buffer;

const memMb = () => {
  const m = Deno.memoryUsage();
  return { heap_plus_external: +((m.heapUsed + m.external) / 2 ** 20).toFixed(1) };
};

const here = `${import.meta.dirname ?? new URL(".", import.meta.url).pathname}/`
  .replace(/\/+$/, "/");

const t0 = performance.now();
const mp = await loadMicroPython({
  url: `${here}micropython/micropython.wasm`,
  heapsize: 16 * 2 ** 20,
  stdout: () => {},
});
const tInterp = performance.now();

mp.runPython(await Deno.readTextFile(`${here}handler.py`));
mp.runPython(`
import json
def _dispatch(s):
    return json.dumps(handle(json.loads(s)))
`);
const dispatch = mp.globals.get("_dispatch");

const boot = {
  wall_ms: +(performance.now() - t0).toFixed(1),
  interp_wall_ms: +(tInterp - t0).toFixed(1),
  runtime: "micropython",
  mem_mb: memMb(),
};
console.log(JSON.stringify({ event: "boot", ...boot }));

let requests = 0;
const port = Number(Deno.env.get("PORT") ?? 8000);

Deno.serve({ port }, async (req: Request) => {
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
  try {
    out = dispatch(reqJson);
  } catch (err) {
    status = 500;
    out = JSON.stringify({ error: String(err) });
  }

  requests += 1;
  const meta = {
    request_no: requests,
    req_wall_ms: +(performance.now() - w0).toFixed(2),
    boot,
    mem_mb: memMb(),
  };
  return new Response(`{"result":${out},"_meta":${JSON.stringify(meta)}}`, {
    status,
    headers: { "content-type": "application/json" },
  });
});
