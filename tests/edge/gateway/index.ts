// Stands in for Supabase's API gateway in the pgflow edge test: forwards
// /gateway/rest/v1/... to PostgREST, which serves at its root.
const target = Deno.env.get("PGRST_URL")!;

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);
  const path = url.pathname.replace(/^\/gateway\/rest\/v1/, "") || "/";
  const headers = new Headers(req.headers);
  headers.delete("host");
  return await fetch(target + path + url.search, {
    method: req.method,
    headers,
    body: req.body ? await req.arrayBuffer() : undefined,
  });
});
