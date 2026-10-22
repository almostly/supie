// A stand-in for the Supabase APIs: answers with what it received.
Deno.serve(async (req: Request) => {
  const url = new URL(req.url);
  const body = req.body ? new Uint8Array(await req.arrayBuffer()) : new Uint8Array();
  if (url.pathname.endsWith(".onnx") && req.method === "GET") {
    // the model the ONNX test wrote next to this file
    return new Response(await Deno.readFile(`${import.meta.dirname}/model.onnx`));
  }
  if (url.pathname.includes("/storage/v1/object/") && req.method === "GET") {
    return new Response(new Uint8Array([0, 1, 254, 255]), {
      headers: { "content-type": "application/octet-stream" },
    });
  }
  return Response.json(
    {
      method: req.method,
      path: url.pathname,
      query: url.search,
      authorization: req.headers.get("authorization"),
      prefer: req.headers.get("prefer"),
      body: Array.from(body),
    },
    { headers: { "content-range": "0-0/3" } },
  );
});
