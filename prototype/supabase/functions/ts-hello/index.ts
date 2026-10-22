// The same handler as py-hello and mpy-hello, written in TypeScript, as the
// baseline for the Python runtimes.

const memMb = () => {
  const m = Deno.memoryUsage();
  return { heap_plus_external: +((m.heapUsed + m.external) / 2 ** 20).toFixed(1) };
};

type Body = { name?: string; xs?: number[]; n?: number };

function handle(body: Body) {
  const xs = body.xs ?? [1, 2, 3, 4, 5];
  const n = Math.trunc(body.n ?? 0);

  let acc = 0;
  for (let i = 1; i <= n; i++) {
    const p = i / (n + 1);
    acc += Math.log(p / (1 - p));
  }

  const mean = xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  const stdev = xs.length > 1 && mean !== null
    ? Math.sqrt(xs.reduce((a, x) => a + (x - mean) ** 2, 0) / xs.length)
    : null;
  return { hello: body.name ?? "world", mean, stdev, logit_sum: acc, n };
}

const t0 = performance.now();
const boot = {
  wall_ms: +(performance.now() - t0).toFixed(1),
  runtime: "typescript",
  mem_mb: memMb(),
};

let requests = 0;
const port = Number(Deno.env.get("PORT") ?? 8000);

Deno.serve({ port }, async (req: Request) => {
  const w0 = performance.now();
  const text = req.method === "POST" ? await req.text() : "";
  let status = 200;
  let out: string;
  try {
    out = JSON.stringify(handle(text ? JSON.parse(text) : {}));
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
