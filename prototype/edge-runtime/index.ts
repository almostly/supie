// Main service for running the Edge Runtime image directly, without the
// Supabase CLI. Each function runs as a user worker with the hosted limits.
// Request headers: x-cold: 1 forces a fresh worker, x-snapshot: 1 boots
// Pyodide from snapshot.bin.gz.
const staticFiles = ["pyodide/*", "handler.py", "snapshot.bin.gz"];

Deno.serve(async (req: Request) => {
  const name = new URL(req.url).pathname.split("/").filter(Boolean)[0];
  const servicePath = `/home/deno/functions/${name}`;
  const snapshot = req.headers.get("x-snapshot") === "1";
  try {
    const worker = await EdgeRuntime.userWorkers.create({
      servicePath,
      memoryLimitMb: 256,
      workerTimeoutMs: 150_000,
      cpuTimeSoftLimitMs: 2000,
      cpuTimeHardLimitMs: 2000,
      noModuleCache: false,
      envVars: snapshot ? [["PY_SNAPSHOT", "snapshot.bin.gz"]] : [],
      forceCreate: req.headers.get("x-cold") === "1",
      staticPatterns: staticFiles.map((f) => `${servicePath}/${f}`),
    });
    return await worker.fetch(req);
  } catch (err) {
    return new Response(JSON.stringify({ error: String(err) }), { status: 500 });
  }
});
