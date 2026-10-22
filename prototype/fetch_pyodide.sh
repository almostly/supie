#!/bin/sh
# Vendor Pyodide into the function folder from npm, and optionally build the
# memory snapshot with Deno (run through npx, no install needed).
set -eu
cd "$(dirname "$0")"
version=314.0.7
dest=supabase/functions/py-hello/pyodide
tmp=$(mktemp -d)
npm pack "pyodide@$version" --pack-destination "$tmp" --silent >/dev/null
tar -xzf "$tmp/pyodide-$version.tgz" -C "$tmp"
mkdir -p "$dest"
for f in package.json pyodide-lock.json pyodide.asm.mjs pyodide.asm.wasm pyodide.mjs python_stdlib.zip; do
  cp "$tmp/package/$f" "$dest/"
done
echo "vendored pyodide $version into $dest"
if [ "${1:-}" = "--snapshot" ]; then
  npx -y deno@2.9.6 run -A bench/make_snapshot.ts "$tmp/snapshot.bin"
  gzip -9 -c "$tmp/snapshot.bin" > supabase/functions/py-hello/snapshot.bin.gz
  echo "wrote supabase/functions/py-hello/snapshot.bin.gz"
fi
rm -rf "$tmp"
