// Installs a function's requirements with micropip and zips the installed
// files. Run by `supie build` under plain Deno:
//   deno run -A install_packages.ts <runtime-dir> <cdn-url> <out-zip> <requirements-json>
// Packages Pyodide builds (pydantic-core, numpy, ...) come from its CDN, the
// rest from PyPI; only pure-Python wheels or Pyodide builds can install.

const [runtimeDir, cdn, out, requirementsJson] = Deno.args;
const { default: createPyodideModule } = await import(`file://${runtimeDir}/pyodide.asm.mjs`);
const { loadPyodide } = await import(`file://${runtimeDir}/pyodide.mjs`);

const py = await loadPyodide({
  indexURL: `${runtimeDir}/`,
  packageBaseUrl: cdn,
  createPyodideModule,
  lockFileContents: await Deno.readTextFile(`${runtimeDir}/pyodide-lock.json`),
  stdout: (line: string) => console.error(line),
});
await py.loadPackage("micropip", { messageCallback: () => {} });
const sitePackages = py.runPython("import site; site.getsitepackages()[0]");
const before = new Set(py.FS.readdir(sitePackages));
await py.pyimport("micropip").install(JSON.parse(requirementsJson));
py.globals.set("before", py.toPy([...before]));
py.runPython(`
import os, site, zipfile

sp = site.getsitepackages()[0]
with zipfile.ZipFile("/tmp/packages.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for name in sorted(set(os.listdir(sp)) - set(before)):
        path = os.path.join(sp, name)
        if os.path.isfile(path):
            archive.write(path, name)
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for file in files:
                full = os.path.join(root, file)
                archive.write(full, os.path.relpath(full, sp))
`);
await Deno.writeFile(out, py.FS.readFile("/tmp/packages.zip"));
