// Build a Pyodide memory snapshot (initialized interpreter state), the same
// trick Cloudflare Python Workers use to skip interpreter boot.
// Usage: deno run -A bench/make_snapshot.ts <out-file>

import createPyodideModule from "../supabase/functions/py-hello/pyodide/pyodide.asm.mjs";
import { loadPyodide } from "../supabase/functions/py-hello/pyodide/pyodide.mjs";

const out = Deno.args[0] ?? "snapshot.bin";
const indexURL = new URL("../supabase/functions/py-hello/pyodide/", import.meta.url).pathname;

const py = await loadPyodide({
  indexURL,
  createPyodideModule,
  lockFileContents: await Deno.readTextFile(`${indexURL}pyodide-lock.json`),
  _makeSnapshot: true,
});
// Pre-import the stdlib modules the handler uses so they live in the snapshot.
py.runPython("import json, math, statistics");
const snap: Uint8Array = py.makeMemorySnapshot();
await Deno.writeFile(out, snap);
console.log(`snapshot: ${(snap.length / 2 ** 20).toFixed(1)} MB -> ${out}`);
