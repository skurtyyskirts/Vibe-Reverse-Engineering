# livetools — Live Dynamic Analysis

Frida-based toolkit for attaching to a running process and inspecting or modifying it at runtime. Complements `retools` (offline) — static analysis finds candidates, live tools confirm and act.

Run all commands from the repo root: `python -m livetools <command> <args>`

---

## Session Management

```bash
python -m livetools attach trl.exe        # attach to running process by name
python -m livetools attach game.exe --spawn  # launch, instrument, then resume (catches init code)
python -m livetools status                # check current connection
python -m livetools detach                # end session
```

---

## Commands

| Command | Purpose |
|---------|---------|
| `trace <VA>` | Non-blocking: log N hits with optional register/memory reads |
| `steptrace <VA>` | Instruction-level trace (Stalker) with call depth control |
| `collect <VA> [VA2...]` | Multi-address hit counting over a duration |
| `bp add/del/list <VA>` | Breakpoints (suspends target) |
| `watch` | Wait for a breakpoint hit |
| `regs` / `stack` / `bt` | Inspect registers, stack, backtrace at a breakpoint |
| `mem read <VA> <size>` | Read live process memory (supports `--as float32`) |
| `mem write <VA> <hex>` | Write live process memory |
| `disasm [VA]` | Disassemble from live process memory |
| `scan <pattern>` | Search process memory for a byte pattern |
| `modules` | List loaded modules with base addresses |
| `dipcnt on/off/read` | D3D9 `DrawIndexedPrimitive` call counter |
| `dipcnt callers [N]` | Histogram return addresses for N sampled DIP calls |
| `memwatch start/stop/read` | Memory write watchpoint with backtrace |
| `analyze <file>` | Offline analysis of collected `.jsonl` trace data |

---

## Usage Examples

```bash
# Verify a patch landed at runtime
python -m livetools disasm 0x407150

# Trace a function hit count and register state
python -m livetools trace 0x407150 --count 20 --read "eax; ecx; [esp+4]:4:uint32"

# Watch for writes to a memory address
python -m livetools memwatch start 0xEFDD64
# ... exercise game ...
python -m livetools memwatch read

# Count D3D9 draw calls per frame
python -m livetools dipcnt on
# ... wait a few frames ...
python -m livetools dipcnt read

# Patch a byte at runtime
python -m livetools mem write 0x407150 C3
```

---

## Static vs. Runtime Addresses

For 32-bit games without ASLR (most Win32 games including TRL), the PE preferred base matches the runtime load address — static addresses from `retools` work directly with `livetools`.

For DLLs or ASLR-enabled executables:
```bash
# Compare static base vs runtime base
python -m livetools modules --filter d3d9
# runtime_addr = runtime_base + (static_addr - preferred_base)
```

---

## Requirements

```bash
pip install -r requirements.txt
```

The target process must be running on the same Windows machine. Some processes require their window to be focused for traces to capture data.
