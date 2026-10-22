// Supabase's Edge Runtime refuses shared WebAssembly memory. Pyodide only
// asks for one shared page, to sleep with Atomics.wait while stdin returns
// EAGAIN, and it handles Atomics.wait failing. Fall back to ordinary memory.
const NativeMemory = WebAssembly.Memory;
function Memory(desc: WebAssembly.MemoryDescriptor) {
  try {
    return new NativeMemory(desc);
  } catch (err) {
    if (!desc?.shared) throw err;
    return new NativeMemory({ ...desc, shared: false });
  }
}
Memory.prototype = NativeMemory.prototype;
WebAssembly.Memory = Memory as unknown as typeof WebAssembly.Memory;
