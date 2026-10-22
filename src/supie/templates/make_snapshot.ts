// Builds a Pyodide memory snapshot with supie and the preloaded modules
// already imported. Run by `supie build` under plain Deno:
//   deno run -A make_snapshot.ts <runtime-dir> <app-dir> <out-file> <modules>

const [runtimeDir, appDir, out, modules] = Deno.args;
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
py.runPython(`
import sys
sys.path.insert(0, "/app")
import json, supie, supie.app, supie.supabase
${modules ? `import ${modules}` : ""}
`);
await Deno.writeFile(out, py.makeMemorySnapshot());
