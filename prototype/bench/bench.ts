// Cold/warm benchmark for the py-hello function under plain Deno.
// Each cold run is a fresh process (a fresh isolate, like a new edge worker).
// Usage: deno run -A bench/bench.ts [runs] [snapshot-file ...]

const deno = Deno.execPath();
const fn = new URL("../supabase/functions/py-hello/index.ts", import.meta.url).pathname;
const runs = Number(Deno.args[0] ?? 5);
const variants: (string | undefined)[] = [undefined, ...Deno.args.slice(1)];

type Boot = { wall_ms: number; cpu_ms: number; process_cpu_ms: number; mem_mb: { heap_plus_external: number; rss: number } };

async function oneRun(snapshot: string | undefined, port: number) {
  const tSpawn = performance.now();
  const env: Record<string, string> = { PORT: String(port) };
  if (snapshot) env.PY_SNAPSHOT = snapshot;
  const child = new Deno.Command(deno, {
    args: ["run", "-A", fn],
    env,
    stdout: "piped",
    stderr: "piped",
  }).spawn();

  // Wait for the boot log line.
  const reader = child.stdout.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  let boot: Boot | undefined;
  while (!boot) {
    const { value, done } = await reader.read();
    if (done) throw new Error("process exited before boot");
    buf += value;
    for (const line of buf.split("\n")) {
      if (line.includes('"event":"boot"')) boot = JSON.parse(line);
    }
  }
  // Wait for the listener.
  let first: any;
  for (let i = 0; i < 100 && !first; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/`, { method: "POST", body: '{"n":0}' });
      first = await r.json();
    } catch { await new Promise((r) => setTimeout(r, 20)); }
  }
  const tFirst = performance.now() - tSpawn;

  const warmCpu: number[] = [];
  const warmWall: number[] = [];
  for (let i = 0; i < 200; i++) {
    const r = await fetch(`http://127.0.0.1:${port}/`, { method: "POST", body: '{"name":"x","xs":[1,2,3,4],"n":100}' });
    const j = await r.json();
    warmCpu.push(j._meta.req_cpu_ms);
    warmWall.push(j._meta.req_wall_ms);
  }
  const heavy = await (await fetch(`http://127.0.0.1:${port}/`, { method: "POST", body: '{"n":1000000}' })).json();
  const memAfter = heavy._meta.mem_mb;

  child.kill();
  await child.status;
  return { boot, tFirst, firstReqCpu: first._meta.req_cpu_ms, warmCpu, warmWall, heavyCpu: heavy._meta.req_cpu_ms, heavyWall: heavy._meta.req_wall_ms, memAfter };
}

const median = (a: number[]) => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
const p95 = (a: number[]) => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length * 0.95)]; };

const results: Record<string, unknown> = {};
let port = 8100;
for (const v of variants) {
  const name = v ? `snapshot:${v.split("/").pop()}` : "plain";
  const rs = [];
  for (let i = 0; i < runs; i++) rs.push(await oneRun(v, port++));
  results[name] = {
    runs,
    boot_wall_ms_median: median(rs.map((r) => r.boot.wall_ms)),
    boot_thread_cpu_ms_median: median(rs.map((r) => r.boot.cpu_ms)),
    boot_thread_cpu_ms_max: Math.max(...rs.map((r) => r.boot.cpu_ms)),
    boot_process_cpu_ms_median: median(rs.map((r) => r.boot.process_cpu_ms)),
    spawn_to_first_response_ms_median: +median(rs.map((r) => r.tFirst)).toFixed(0),
    first_request_cpu_ms_median: median(rs.map((r) => r.firstReqCpu)),
    warm_req_cpu_ms_median: median(rs.flatMap((r) => r.warmCpu)),
    warm_req_cpu_ms_p95: p95(rs.flatMap((r) => r.warmCpu)),
    warm_req_wall_ms_median: median(rs.flatMap((r) => r.warmWall)),
    heavy_1M_loop_cpu_ms_median: median(rs.map((r) => r.heavyCpu)),
    mem_heap_plus_external_mb_max: Math.max(...rs.map((r) => Math.max(r.boot.mem_mb.heap_plus_external, r.memAfter.heap_plus_external))),
    rss_mb_max: Math.max(...rs.map((r) => Math.max(r.boot.mem_mb.rss, r.memAfter.rss))),
  };
  console.error(name, "done");
}
console.log(JSON.stringify(results, null, 2));
