// Builds a Pyodide memory snapshot with supie and the preloaded modules
// already imported. Run by `supie build` under plain Deno:
//   deno run -A make_snapshot.ts <runtime-dir> <app-dir> <out-file> <modules> [packages-zip]
// Snapshots can't hold compiled extensions, so preloading one is an error.

const [runtimeDir, appDir, out, modules, packagesZip] = Deno.args;
const { default: createPyodideModule } = await import(`file://${runtimeDir}/pyodide.asm.mjs`);
const { loadPyodide } = await import(`file://${runtimeDir}/pyodide.mjs`);

const py = await loadPyodide({
  indexURL: `${runtimeDir}/`,
  createPyodideModule,
  lockFileContents: await Deno.readTextFile(`${runtimeDir}/pyodide-lock.json`),
  _makeSnapshot: true,
});

const walk = async (dir: string, prefix = ""): Promise<void> => {
  for await (const entry of Deno.readDir(dir)) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory) {
      py.FS.mkdirTree(`/app/${rel}`);
      await walk(`${dir}/${entry.name}`, rel);
    } else {
      py.FS.writeFile(`/app/${rel}`, await Deno.readFile(`${dir}/${entry.name}`));
    }
  }
};
py.FS.mkdirTree("/app");
await walk(appDir);
if (packagesZip) {
  const sitePackages = py.runPython("import site; site.getsitepackages()[0]");
  py.unpackArchive(await Deno.readFile(packagesZip), "zip", { extractDir: sitePackages });
}
const libraries = () => Object.keys(py._module.LDSO.loadedLibsByName).length;
const baseline = libraries();
py.runPython(`
import sys
sys.path.insert(0, "/app")
import json, supie, supie.app, supie.supabase, supie.flow
${modules ? `import ${modules}` : ""}
`);
if (libraries() > baseline) {
  console.error(`preload loads compiled code, which a snapshot can't hold: ${modules}`);
  Deno.exit(3);
}
await Deno.writeFile(out, py.makeMemorySnapshot());
