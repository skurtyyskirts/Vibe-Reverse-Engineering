# retools — Static Analysis Toolkit

Offline PE binary analysis tools for reverse engineering. Works on `.exe` and `.dll` files without requiring the target process to be running.

Run all tools from the repo root: `python -m retools.<tool> <args>`

---

## Tools

| Tool | Purpose |
|------|---------|
| `disasm` | Disassemble N instructions at an address |
| `decompiler` | Ghidra-quality C decompilation with a knowledge base |
| `funcinfo` | Function start/end, calling convention, callees |
| `cfg` | Control flow graph (text or Mermaid) |
| `callgraph` | Caller/callee tree (multi-level, up or down) |
| `xrefs` | All calls/jumps to an address |
| `datarefs` | Instructions referencing a global address |
| `structrefs` | `[reg+offset]` struct field usage and reconstruction |
| `vtable` | C++ vtable dump and indirect call site finder |
| `rtti` | MSVC RTTI — class name and inheritance chain from vtable |
| `search` | Strings, byte patterns, imports, exports, instructions |
| `dataflow` | Forward constant propagation and backward register slicing |
| `context` | Assemble full analysis context for a function |
| `sigdb` | Signature database — identify compiler, scan for known functions |
| `bootstrap` | Auto-seed a knowledge base (RTTI, CRT IDs, compiler info, propagated labels) |
| `asi_patcher` | Generate a patching `.asi` DLL from a JSON patch spec |
| `readmem` | Read typed data from a PE file at a given address |
| `throwmap` | Map MSVC C++ `_CxxThrowException` call sites to error strings |
| `dumpinfo` | Minidump analysis — exception, threads, stack, memory |

---

## Usage Examples

```bash
# Decompile a function (always include --types for richer output)
python -m retools.decompiler trl.exe 0x407150 --types patches/TombRaiderLegend/kb.h

# Find all callers of an address
python -m retools.callgraph trl.exe 0x407150 --up 3

# Search for strings and the code that references them
python -m retools.search trl.exe strings -f "render" --xrefs

# Identify compiler version
python -m retools.sigdb fingerprint trl.exe

# Bootstrap a new binary (seeds KB with RTTI, signatures, compiler info)
python -m retools.bootstrap trl.exe --project TombRaiderLegend
```

---

## Knowledge Base (`kb.h`)

All discoveries accumulate in `patches/<project>/kb.h`. Passing `--types kb.h` to `decompiler` makes every subsequent decompilation richer.

```c
// Format:
struct Foo { int x; float y; };               // struct definitions
@ 0x407150 void __cdecl CullAndSubmit(void);  // named functions
$ 0xEFDD64 float g_frustumThreshold;          // named globals
```

---

## Requirements

```bash
pip install -r requirements.txt
python verify_install.py        # confirm Ghidra backend is ready
python verify_install.py --setup  # first-time setup (~600MB Ghidra download)
```

See [`.claude/rules/tool-catalog.md`](.claude/rules/tool-catalog.md) for the full reference with all flags and caveats.
