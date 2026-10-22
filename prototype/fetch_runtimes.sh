#!/bin/sh
# Vendor Pyodide and MicroPython into the function folders from npm, and
# optionally build the Pyodide memory snapshot with Deno (run through npx).
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

mpy=1.29.0-6
mpy_dest=supabase/functions/mpy-hello/micropython
npm pack "@micropython/micropython-webassembly-pyscript@$mpy" --pack-destination "$tmp" --silent >/dev/null
mkdir -p "$tmp/mpy" "$mpy_dest"
tar -xzf "$tmp/micropython-micropython-webassembly-pyscript-$mpy.tgz" -C "$tmp/mpy"
cp "$tmp/mpy/package/package.json" "$tmp/mpy/package/micropython.mjs" "$tmp/mpy/package/micropython.wasm" "$mpy_dest/"
echo "vendored micropython $mpy into $mpy_dest"
if [ "${1:-}" = "--snapshot" ]; then
  npx -y deno@2.9.6 run -A bench/make_snapshot.ts "$tmp/snapshot.bin"
  gzip -9 -c "$tmp/snapshot.bin" > supabase/functions/py-hello/snapshot.bin.gz
  echo "wrote supabase/functions/py-hello/snapshot.bin.gz"
fi
rm -rf "$tmp"
