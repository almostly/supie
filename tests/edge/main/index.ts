// Main service for the Edge Runtime image in the tests: starts each function
// as a user worker with Supabase's hosted limits. The fake Supabase project is
// the echo function in the same container. Send x-cold: 1 for a fresh worker.
const staticFiles = ["runtime/*", "snapshot.bin.gz"];

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
      envVars: [
        ["SUPABASE_URL", "http://localhost:9000/echo"],
        ["SUPABASE_ANON_KEY", "anon-key"],
        ["SUPABASE_SERVICE_ROLE_KEY", "service-role-key"],
        ["GREETING", "hej"],
      ],
      forceCreate: req.headers.get("x-cold") === "1",
      staticPatterns: staticFiles.map((f) => `${servicePath}/${f}`),
    });
    return await worker.fetch(req);
  } catch (err) {
    return new Response(JSON.stringify({ error: String(err) }), { status: 500 });
  }
});
