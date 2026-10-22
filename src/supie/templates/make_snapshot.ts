// Builds a Pyodide memory snapshot with supie and the preloaded modules
// already imported. Run by `supie build` under plain Deno:
//   deno run -A make_snapshot.ts <runtime-dir> <app-dir> <out-dir> <modules> [packages-zip]
// Writes <out-dir>/snapshot.bin, plus snapshot.json and dylibs/<i>.so for any
// compiled extensions the imports loaded: the patched runtime records where
// each one landed, so python.ts can load it there again before restoring.

const [runtimeDir, appDir, outDir, modules, packagesZip] = Deno.args;
const { default: createPyodideModule } = await import(`file://${runtimeDir}/pyodide.asm.mjs`);
const { loadPyodide } = await import(`file://${runtimeDir}/pyodide.mjs`);

// deno-lint-ignore no-explicit-any
let Module: any;
const py = await loadPyodide({
  indexURL: `${runtimeDir}/`,
  // deno-lint-ignore no-explicit-any
  createPyodideModule: (settings: any) => {
    Module = settings;
    return createPyodideModule(settings);
  },
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
const before = (Module.supieLoadedDylibs ?? []).length;
py.runPython(`
import sys
sys.path.insert(0, "/app")
import json, supie, supie.app, supie.supabase, supie.flow, supie.onnx
${modules ? `import ${modules}` : ""}
`);
const dylibs = (Module.supieLoadedDylibs ?? []).slice(before);
await Deno.mkdir(`${outDir}/dylibs`, { recursive: true });
for (const [i, dylib] of dylibs.entries()) {
  await Deno.writeFile(`${outDir}/dylibs/${i}.so`, py.FS.readFile(dylib.name));
}
await Deno.writeTextFile(
  `${outDir}/snapshot.json`,
  JSON.stringify({
    dylibs: dylibs.map(({ name, memoryBase, tableBase }: Record<string, unknown>) => ({
      name,
      memoryBase,
      tableBase,
    })),
  }),
);
await Deno.writeFile(`${outDir}/snapshot.bin`, py.makeMemorySnapshot());
