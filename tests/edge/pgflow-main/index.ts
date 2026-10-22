// Main service for the pgflow edge test: like ../main, but the Supabase
// variables come from the container, pointing at the gateway function.
const passThrough = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "PGRST_URL"];
const staticFiles = ["runtime/*", "snapshot.bin.gz", "snapshot.json", "dylibs/*", "packages.zip"];

Deno.serve(async (req: Request) => {
  const name = new URL(req.url).pathname.split("/").filter(Boolean)[0];
  const servicePath = `/home/deno/functions/${name}`;
  try {
    const worker = await EdgeRuntime.userWorkers.create({
      servicePath,
      memoryLimitMb: 256,
      workerTimeoutMs: 150_000,
      cpuTimeSoftLimitMs: 2000,
      cpuTimeHardLimitMs: 2000,
      noModuleCache: false,
      envVars: passThrough.map((k) => [k, Deno.env.get(k) ?? ""]),
      forceCreate: req.headers.get("x-cold") === "1",
      staticPatterns: staticFiles.map((f) => `${servicePath}/${f}`),
    });
    return await worker.fetch(req);
  } catch (err) {
    return new Response(JSON.stringify({ error: String(err) }), { status: 500 });
  }
});
