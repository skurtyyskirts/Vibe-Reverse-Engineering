"""CLI entry point for livetools -- Frida-based live process analysis toolkit.

Usage:  python -m livetools <command> [args]

Session management:
    python -m livetools attach <process_name_or_pid>
    python -m livetools detach
    python -m livetools status

Breakpoints:
    python -m livetools bp add <addr>
    python -m livetools bp del <addr>
    python -m livetools bp list

Execution control:
    python -m livetools watch [--timeout 60]
    python -m livetools step [over|into|out]
    python -m livetools resume

Inspection:
    python -m livetools regs
    python -m livetools stack [count]
    python -m livetools disasm [addr] [-n 16]
    python -m livetools bt
    python -m livetools mem read <addr> <size> [--as float32]
    python -m livetools mem write <addr> <hex_bytes>

Non-blocking tracing:
    python -m livetools trace <addr> [--count N] [--read SPEC] [--filter EXPR]
    python -m livetools steptrace <addr> [--max-insn N] [--call-depth D]
    python -m livetools collect <addr> [addr2 ...] [--duration N] [--fence ADDR]
    python -m livetools modules [--filter PATTERN]

Offline analysis:
    python -m livetools analyze <file.jsonl> [--summary] [--group-by FIELD]

Scanning:
    python -m livetools scan <hex_pattern> [--range START:SIZE]

Memory watchpoint:
    python -m livetools memwatch start <addr> [--size N] [--max-hits N]
    python -m livetools memwatch read
    python -m livetools memwatch stop

Game window (no Frida needed):
    python -m livetools gamectl --exe game.exe keys "DOWN DOWN RETURN"
    python -m livetools screenshot grab --exe game.exe --out shot.png
    python -m livetools screenshot diff before.png after.png

RTX Remix runtime (no Frida needed):
    python -m livetools remix status --game-dir DIR
    python -m livetools remix conf set KEY VALUE --game-dir DIR
    python -m livetools remix preset apply <name> --game-dir DIR
    python -m livetools remix menu --exe game.exe
    python -m livetools remix log --game-dir DIR --errors

Workflow:  attach -> (bp/trace/collect/steptrace/modules) -> analyze -> detach

NOTE: Some games only run rendering/logic when their window is focused.
If trace/steptrace/collect time out with 0 results, alt-tab to the game first.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import client

#: Exit codes. Handlers an unattended loop branches on return these instead of
#: only printing prose: 0 succeeded, EXIT_FAILED the command could not run,
#: EXIT_NEGATIVE it ran and the answer was no (game unhealthy, frame black,
#: nothing changed).
EXIT_FAILED = 1
EXIT_NEGATIVE = 3


def _parse_addr(addr_str: str) -> str:
    val = int(addr_str, 16) if addr_str.startswith("0x") else int(addr_str, 16)
    return f"0x{val:08X}"


def _require_attached() -> bool:
    if not client.is_daemon_alive():
        print("[not attached]")
        return False
    return True


# ── session commands ───────────────────────────────────────────────────────

def cmd_attach(args: argparse.Namespace) -> None:
    spawn = getattr(args, "spawn", False)
    if spawn:
        target_path = Path(args.target).resolve()
        if not target_path.is_file():
            print(f"[error] --spawn target not found: {target_path}", file=sys.stderr)
            sys.exit(1)

    if client.is_daemon_alive():
        try:
            resp = client.send_command({"cmd": "status"})
            if resp.get("state") in ("RUNNING", "FROZEN"):
                print(client.format_status_line(resp))
                print("Already attached. Use 'detach' first to release.")
                return
        except Exception:
            pass
        print("Stale daemon detected, cleaning up...")
        _force_cleanup()

    _spawn_daemon(args.target, spawn=spawn)


def _force_cleanup() -> None:
    state = client.read_state()
    if state:
        client._kill_stale_daemon(state)
    else:
        client.STATE_FILE.unlink(missing_ok=True)
    time.sleep(0.5)


def _spawn_daemon(target: str, *, spawn: bool = False) -> None:
    daemon_cmd = [sys.executable, "-m", "livetools.server", target]
    if spawn:
        daemon_cmd.append("--spawn")
    kwargs: dict = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    kwargs["stdout"] = subprocess.DEVNULL
    log_fh = client.DAEMON_LOG.open("w")
    kwargs["stderr"] = log_fh
    subprocess.Popen(daemon_cmd, **kwargs)
    log_fh.close()

    deadline = time.time() + 15
    while time.time() < deadline:
        if client.is_daemon_alive():
            resp = client.send_command({"cmd": "status"})
            print(client.format_status_line(resp))
            print(f"Attached to {target}.")
            client.DAEMON_LOG.unlink(missing_ok=True)
            return
        time.sleep(0.3)

    print("[error] Daemon did not start within 15 seconds.", file=sys.stderr)
    log_text = ""
    try:
        log_text = client.DAEMON_LOG.read_text().strip()
    except OSError:
        pass
    if log_text:
        print(f"[error] Daemon log:\n{log_text}", file=sys.stderr)
    client.DAEMON_LOG.unlink(missing_ok=True)
    sys.exit(1)


def cmd_detach(_args: argparse.Namespace) -> None:
    if not client.is_daemon_alive():
        client.STATE_FILE.unlink(missing_ok=True)
        print("Detached (daemon was already gone).")
        return
    try:
        resp = client.send_command({"cmd": "detach"})
        print(client.format_status_line(resp))
    except Exception:
        pass
    print("Detached.")
    time.sleep(0.5)
    client.STATE_FILE.unlink(missing_ok=True)


def cmd_status(_args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "status"})
    print(client.format_status_line(resp))


# ── breakpoint commands ────────────────────────────────────────────────────

def cmd_bp(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    action = args.action
    if action == "add":
        addr = _parse_addr(args.addr)
        resp = client.send_command({"cmd": "bp_add", "addr": addr})
        print(client.format_status_line(resp))
        if resp.get("ok"):
            msg = resp.get("msg", "")
            extra = f" ({msg})" if msg else ""
            print(f"Breakpoint #{resp.get('bpId')} set at {addr}{extra}")
        else:
            print(f"[error] {resp.get('error', 'unknown')}")
    elif action == "del":
        addr = _parse_addr(args.addr)
        resp = client.send_command({"cmd": "bp_del", "addr": addr})
        print(client.format_status_line(resp))
        if resp.get("ok"):
            print(f"Breakpoint at {addr} removed.")
        else:
            print(f"[error] {resp.get('error', resp.get('msg', 'unknown'))}")
    elif action == "list":
        resp = client.send_command({"cmd": "bp_list"})
        print(client.format_status_line(resp))
        bps = resp.get("breakpoints", [])
        if not bps:
            print("No breakpoints set.")
        else:
            for bp in bps:
                print(f"  bp#{bp['id']}  {bp['addr']}  hits: {bp['hitCount']}")


# ── execution control commands ─────────────────────────────────────────────

def cmd_watch(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    timeout = args.timeout
    resp = client.send_command({"cmd": "watch", "timeout": timeout}, timeout=timeout)
    print(client.format_status_line(resp))
    if resp.get("timeout"):
        print(f"[TIMEOUT] No breakpoint hit within {timeout}s")
        return
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    snap = resp.get("snapshot")
    if snap:
        print(client.format_snapshot(snap))


def cmd_regs(_args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "regs"})
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    regs = resp["regs"]
    arch = regs.get("_arch", "x86")
    print("Registers:")
    if arch == "x64":
        w = 16
        print(f"  RAX={regs.get('rax','?'):>{w}s}  RBX={regs.get('rbx','?'):>{w}s}"
              f"  RCX={regs.get('rcx','?'):>{w}s}  RDX={regs.get('rdx','?'):>{w}s}")
        print(f"  RSI={regs.get('rsi','?'):>{w}s}  RDI={regs.get('rdi','?'):>{w}s}"
              f"  RBP={regs.get('rbp','?'):>{w}s}  RSP={regs.get('rsp','?'):>{w}s}")
        print(f"  R8 ={regs.get('r8','?'):>{w}s}  R9 ={regs.get('r9','?'):>{w}s}"
              f"  R10={regs.get('r10','?'):>{w}s}  R11={regs.get('r11','?'):>{w}s}")
        print(f"  R12={regs.get('r12','?'):>{w}s}  R13={regs.get('r13','?'):>{w}s}"
              f"  R14={regs.get('r14','?'):>{w}s}  R15={regs.get('r15','?'):>{w}s}")
        print(f"  RIP={regs.get('rip','?'):>{w}s}")
    else:
        print(f"  EAX={regs.get('eax','?'):>8s}  EBX={regs.get('ebx','?'):>8s}"
              f"  ECX={regs.get('ecx','?'):>8s}  EDX={regs.get('edx','?'):>8s}")
        print(f"  ESI={regs.get('esi','?'):>8s}  EDI={regs.get('edi','?'):>8s}"
              f"  EBP={regs.get('ebp','?'):>8s}  ESP={regs.get('esp','?'):>8s}")
        print(f"  EIP={regs.get('eip','?'):>8s}")


def cmd_stack(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "stack", "count": args.count})
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    entries = resp["stack"]
    print("Stack [ESP]:")
    row = []
    for i, val in enumerate(entries):
        row.append(f"+{i*4:02X}: {val}")
        if len(row) == 4:
            print("  " + "  ".join(row))
            row = []
    if row:
        print("  " + "  ".join(row))


def cmd_mem_read(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    addr = _parse_addr(args.addr)
    resp = client.send_command({"cmd": "mem_read", "addr": addr, "size": args.size})
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    raw = bytes.fromhex(resp["hex"])
    print(client.format_mem_read(int(addr, 16), raw, as_type=args.type))


def cmd_mem_write(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    addr = _parse_addr(args.addr)
    hex_bytes = args.hex_bytes.replace(" ", "")
    resp = client.send_command({"cmd": "mem_write", "addr": addr, "hex": hex_bytes})
    print(client.format_status_line(resp))
    if resp.get("ok"):
        print(f"Wrote {len(hex_bytes)//2} bytes to {addr}.")
    else:
        print(f"[error] {resp.get('error', resp.get('msg', 'unknown'))}")


def cmd_mem_alloc(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "mem_alloc", "size": args.size})
    print(client.format_status_line(resp))
    if resp.get("ok"):
        print(f"Allocated {args.size} bytes at {resp['addr']} (rwx)")
    else:
        print(f"[error] {resp.get('error', resp.get('msg', 'unknown'))}")


def cmd_disasm(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    cmd: dict = {"cmd": "disasm", "count": args.count}
    if args.addr:
        cmd["addr"] = _parse_addr(args.addr)
    resp = client.send_command(cmd)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    for i, insn in enumerate(resp["disasm"]):
        marker = ">" if i == 0 and not args.addr else " "
        print(f"{marker} {insn['addr']}  {insn.get('str', '??')}")


def cmd_bt(_args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "bt"})
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    for i, frame in enumerate(resp["frames"]):
        print(f"  #{i}  {frame}")


def cmd_step(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "step", "mode": args.mode}, timeout=15)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    snap = resp.get("snapshot")
    if snap:
        print(client.format_snapshot(snap, header="STEP COMPLETE"))


def cmd_resume(_args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    resp = client.send_command({"cmd": "resume"})
    print(client.format_status_line(resp))
    if resp.get("ok"):
        print("Resumed.")
    else:
        print(f"[error] {resp.get('error', 'unknown')}")


def cmd_scan(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    cmd: dict = {"cmd": "scan", "pattern": args.pattern}
    if args.range:
        parts = args.range.split(":")
        if len(parts) == 2:
            cmd["start"] = _parse_addr(parts[0])
            cmd["size"] = int(parts[1], 0)
    resp = client.send_command(cmd)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    results = resp.get("results", [])
    if not results:
        print("No matches.")
    else:
        for m in results:
            print(f"  {m['addr']}  ({m['size']} bytes)")


# ── NEW: trace command ─────────────────────────────────────────────────────

def cmd_trace(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    addr = _parse_addr(args.addr)
    cmd: dict = {
        "cmd": "trace", "addr": addr,
        "count": args.count,
        "read": args.read or "",
        "readLeave": args.read_leave or "",
        "filter": args.filter or "",
        "timeout": args.timeout,
    }
    if args.output:
        cmd["output"] = args.output
    resp = client.send_command(cmd, timeout=args.timeout)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    print(client.format_trace(resp))


# ── NEW: steptrace command ─────────────────────────────────────────────────

def cmd_steptrace(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    addr = _parse_addr(args.addr)
    cmd: dict = {
        "cmd": "steptrace", "addr": addr,
        "maxInsn": args.max_insn,
        "callDepth": args.call_depth,
        "detail": args.detail,
        "timeout": args.timeout,
    }
    if args.output:
        cmd["output"] = args.output
    resp = client.send_command(cmd, timeout=args.timeout)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    print(client.format_steptrace(resp))


# ── NEW: collect command ───────────────────────────────────────────────────

def cmd_collect(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    addrs = [_parse_addr(a) for a in args.addrs]

    read_specs = {}
    if args.read_at:
        for spec in args.read_at:
            parts = spec.split("=", 1)
            if len(parts) == 2:
                read_specs[_parse_addr(parts[0])] = parts[1]

    labels = {}
    if args.label:
        for lbl in args.label:
            parts = lbl.split("=", 1)
            if len(parts) == 2:
                labels[_parse_addr(parts[0])] = parts[1]

    cmd: dict = {
        "cmd": "collect",
        "addrs": addrs,
        "duration": args.duration,
        "maxRecords": args.max_records,
        "read": args.read or "",
        "readSpecs": read_specs,
        "labels": labels,
    }
    if args.output:
        cmd["output"] = args.output
    if args.fence:
        cmd["fence"] = _parse_addr(args.fence)
    if args.fence_every:
        cmd["fenceEvery"] = args.fence_every

    resp = client.send_command(cmd, timeout=args.duration + 30)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    print(client.format_collect(resp))


# ── NEW: modules command ───────────────────────────────────────────────────

def cmd_modules(args: argparse.Namespace) -> None:
    if not _require_attached():
        return
    cmd: dict = {"cmd": "modules"}
    if args.filter:
        cmd["filter"] = args.filter
    resp = client.send_command(cmd)
    print(client.format_status_line(resp))
    if not resp.get("ok"):
        print(f"[error] {resp.get('error', 'unknown')}")
        return
    print(client.format_modules(resp))


# ── NEW: dipcnt command ───────────────────────────────────────────────────

def cmd_dipcnt(args: argparse.Namespace) -> None:
    action = getattr(args, "action", None)
    if action == "on":
        dev_ptr = getattr(args, "dev_ptr")
        resp = client.send_command({"cmd": "dipcnt_on", "devPtrAddr": dev_ptr})
        print(client.format_status_line(resp))
        print("DIP counter ON." if resp.get("ok") else f"[error] {resp.get('msg', '?')}")
    elif action == "off":
        resp = client.send_command({"cmd": "dipcnt_off"})
        print(client.format_status_line(resp))
        print("DIP counter OFF." if resp.get("ok") else f"[error] {resp.get('msg', '?')}")
    elif action == "read":
        resp = client.send_command({"cmd": "dipcnt_read"})
        print(client.format_status_line(resp))
        if resp.get("installed"):
            print(f"  Total DIP calls: {resp.get('total', 0)}")
            print(f"  Delta (since last read): {resp.get('delta', 0)}")
        else:
            print("  Not installed.")
    elif action == "callers":
        count = getattr(args, "count", 200)
        resp = client.send_command({"cmd": "dipcnt_callers", "count": count})
        print(client.format_status_line(resp))
        if resp.get("ok"):
            callers = resp.get("callers", [])
            print(f"  Sampled {resp.get('sampled', '?')} DIP calls, {len(callers)} unique callers:")
            for c in callers:
                print(f"    {c['addr']}  x{c['count']}")
        else:
            print(f"  [error] {resp.get('msg', '?')}")
    else:
        print("Usage: python -m livetools dipcnt [on|off|read|callers]")


# ── NEW: analyze command ───────────────────────────────────────────────────

def cmd_analyze(args: argparse.Namespace) -> None:
    from .analyze import run_analyze
    run_analyze(args)


# ── NEW: vishook command ──────────────────────────────────────────────────

def cmd_vishook(args: argparse.Namespace) -> None:
    action = getattr(args, "action", None)
    if action == "on":
        threshold = getattr(args, "threshold")
        jmp_site = getattr(args, "jmp_site")
        orig_target = getattr(args, "orig_target")
        resp = client.send_command({
            "cmd": "vishook_on", "threshold": threshold,
            "jmpSite": jmp_site, "origTarget": orig_target,
        })
        print(client.format_status_line(resp))
        if resp.get("ok"):
            cave = resp.get("cave", "?")
            thr = resp.get("threshold", "?")
            print(f"Visibility override ON.  code-cave @ 0x{cave}")
            print(f"  Callers >= 0x{thr:X}: force visible")
            print(f"  Callers <  0x{thr:X}: call original")
        else:
            print(f"[error] {resp.get('msg', resp.get('error', 'unknown'))}")
    elif action == "off":
        resp = client.send_command({"cmd": "vishook_off"})
        print(client.format_status_line(resp))
        if resp.get("ok"):
            print("Visibility override OFF.  Original jmp restored.")
        else:
            print(f"[error] {resp.get('msg', resp.get('error', 'unknown'))}")
    elif action == "stats":
        resp = client.send_command({"cmd": "vishook_stats"})
        print(client.format_status_line(resp))
        if resp.get("installed"):
            print(f"  Override calls:    {resp.get('overrideCount', 0)}")
            print(f"  Passthrough calls: {resp.get('passthroughCount', 0)}")
        else:
            print("  Not installed.")
    else:
        print("Usage: python -m livetools vishook [on|off|stats]")


# ── NEW: memwatch command ─────────────────────────────────────────────────

def cmd_memwatch(args: argparse.Namespace) -> None:
    action = getattr(args, "action", None)
    if action == "start":
        addr = getattr(args, "addr")
        size = getattr(args, "size", 4)
        max_hits = getattr(args, "max_hits", 20)
        resp = client.send_command({
            "cmd": "memwatch_start", "addr": addr, "size": size, "maxHits": max_hits,
        })
        print(client.format_status_line(resp))
        if resp.get("ok"):
            print(f"Memory write watchpoint active: {resp.get('watching')} ({resp.get('size')} bytes)")
            print(f"  Will capture up to {resp.get('maxHits')} hits, then auto-stop.")
            print("  Use 'python -m livetools memwatch read' to check hits.")
        else:
            print(f"[error] {resp.get('error', '?')}")
    elif action == "stop":
        resp = client.send_command({"cmd": "memwatch_stop"})
        print(client.format_status_line(resp))
        if resp.get("ok"):
            print(f"Watchpoint stopped. {resp.get('hits', 0)} hits captured.")
        else:
            print(f"[error] {resp.get('error', '?')}")
    elif action == "read":
        resp = client.send_command({"cmd": "memwatch_read"})
        print(client.format_status_line(resp))
        if resp.get("ok"):
            hits = resp.get("hits", [])
            print(f"  {len(hits)} write hit(s):")
            for i, h in enumerate(hits):
                print(f"\n  [{i}] Write to {h.get('addr', '?')} from {h.get('from', '?')}")
                bt = h.get("backtrace", [])
                if bt:
                    for j, frame in enumerate(bt):
                        print(f"       bt[{j}]: {frame}")
        else:
            print(f"[error] {resp.get('error', '?')}")
    else:
        print("Usage: python -m livetools memwatch [start|stop|read]")


# ── gamectl command ───────────────────────────────────────────────────────

def cmd_gamectl(args: argparse.Namespace) -> int:
    from . import gamectl as gc
    action = args.gc_action

    # Reading and writing the macro file is bookkeeping — it must work whether
    # or not the game is up (it often is not, right after a crash).
    if action == "macros":
        macros = gc.load_macros(args.macro_file)
        print(f"Macros in {args.macro_file}:")
        for name, defn in sorted(macros.items()):
            print(f"  {name:<24s}  {defn.get('description', '')}")
            print(f"    steps: {defn.get('steps', '')}")
        return 0

    if action == "macro-save":
        try:
            r = gc.save_macro(args.macro_file, args.macro_name, args.steps,
                              description=args.description)
        except (OSError, ValueError) as e:
            print(f"[error] {e}")
            return EXIT_FAILED
        verb = "Replaced" if r["replaced"] else "Saved"
        print(f"{verb} macro '{r['macro']}' in {r['path']}")
        return 0

    hwnd, err = gc.resolve_hwnd(getattr(args, "exe", None),
                                getattr(args, "window", None))
    if action == "info":
        # info doesn't need a valid hwnd to report the error clearly
        if not hwnd:
            print(f"[error] {err}")
            return EXIT_FAILED
        info = gc.get_window_info(hwnd)
        print(f"hwnd:  {info['hwnd']}")
        print(f"title: {info['title']}")
        print(f"pid:   {info['pid']}")
        print(f"tid:   {info['tid']}")
        return 0

    if not hwnd:
        print(f"[error] {err}")
        return EXIT_FAILED

    if action == "key":
        focused = gc.focus_hwnd(hwnd)
        if "+" in args.key_name:
            r = gc.send_chord(args.key_name, hold_ms=args.hold_ms)
        else:
            r = gc.send_key(args.key_name, hold_ms=args.hold_ms)
        print(f"focused={focused} {r}")

    elif action == "keys":
        r = gc.send_keys(hwnd, args.sequence, delay_ms=args.delay_ms)
        print(f"focused={r['focused']} sent={r['count']} ok={r['ok']}")
        for a in r["actions"]:
            if not a.get("ok", True):
                print(f"  [error] {a}")

    elif action == "click":
        r = gc.click_at(hwnd, args.x, args.y)
        print(r)
        if not r["ok"]:
            return EXIT_FAILED

    elif action == "mousemove":
        gc.focus_hwnd(hwnd)
        r = gc.move_mouse(args.dx, args.dy, steps=args.steps,
                          step_ms=args.step_ms)
        print(r)
        if not r["ok"]:
            return EXIT_FAILED

    elif action == "macro":
        macros = gc.load_macros(args.macro_file)
        r = gc.run_macro(hwnd, args.macro_name, macros, delay_ms=args.delay_ms)
        if r["ok"]:
            print(f"Macro '{args.macro_name}' done. "
                  f"{r['steps_result']['count']} actions sent.")
        else:
            print(f"[error] {r.get('error', r)}")
            return EXIT_FAILED

    else:
        print("Usage: python -m livetools gamectl "
              "[info|key|keys|click|mousemove|macro|macros|macro-save]")
        return 2
    return 0


# ── screenshot command ────────────────────────────────────────────────────

def cmd_screenshot(args: argparse.Namespace) -> int:
    from . import screenshot as ss
    action = getattr(args, "ss_action", None)

    if action == "grab":
        from . import gamectl as gc
        hwnd, err = gc.resolve_hwnd(getattr(args, "exe", None),
                                    getattr(args, "window", None))
        if not hwnd:
            print(f"[error] {err}")
            return EXIT_FAILED
        out = Path(args.out) if args.out else ss.default_output_path()
        try:
            path = ss.capture_window_png(hwnd, out,
                                         client_only=not args.full_window)
        except OSError as e:
            print(f"[error] capture failed: {e}")
            return EXIT_FAILED
        w, h, rgb = ss.decode_png(path.read_bytes())
        print(f"Saved {path} ({w}x{h})")
        # An unusable capture is the single most common silent failure of an
        # unattended run — say so at capture time, not three steps later.
        verdict = ss.classify_frame(ss.frame_stats(w, h, rgb))
        print(f"  frame: {verdict['verdict']} ({verdict['reason']})")
        if not verdict["usable"]:
            print("  [warn] nothing rendered — check exclusive fullscreen, "
                  "a crashed device, or remix log --errors")
            return EXIT_NEGATIVE
        return 0

    if action == "diff":
        try:
            wa, ha, rgb_a = ss.decode_png(Path(args.file_a).read_bytes())
            wb, hb, rgb_b = ss.decode_png(Path(args.file_b).read_bytes())
            if (wa, ha) != (wb, hb):
                raise ValueError(f"Dimension mismatch: {wa}x{ha} vs {wb}x{hb}")
            r = ss.diff_rgb(wa, ha, rgb_a, rgb_b, tolerance=args.tolerance)
        except (OSError, ValueError) as e:
            print(f"[error] {e}")
            return EXIT_FAILED
        changed = r["ratio"] >= args.threshold
        verdict = "CHANGED" if changed else "same"
        print(f"{r['changed']}/{r['total']} pixels differ "
              f"(ratio {r['ratio']:.4f}, threshold {args.threshold}) -> {verdict}"
              f"  (expected {args.expect})")
        if r["bbox"]:
            x0, y0, x1, y1 = r["bbox"]
            print(f"  changed region: ({x0},{y0})-({x1},{y1}) "
                  f"[{x1 - x0 + 1}x{y1 - y0 + 1}]")
        if args.tiles:
            try:
                cols, rows = (int(v) for v in args.tiles.lower().split("x"))
            except ValueError:
                print(f"[error] --tiles wants COLSxROWS, got {args.tiles!r}")
                return EXIT_FAILED
            grid = ss.tiled_diff(wa, ha, rgb_a, rgb_b, cols=cols, rows=rows,
                                 tolerance=args.tolerance)
            print(f"  tiles ({cols}x{rows}), ratio per cell:")
            for row in range(rows):
                cells = [t for t in grid["tiles"] if t["row"] == row]
                print("    " + " ".join(f"{t['ratio']:6.3f}" for t in cells))
            hot = grid["hottest"]
            print(f"  hottest: col {hot['col']} row {hot['row']} "
                  f"(ratio {hot['ratio']:.4f})")
        return 0 if changed == (args.expect == "changed") else EXIT_NEGATIVE

    if action == "stats":
        try:
            r = ss.stats_png(args.file, stride=args.stride)
        except (OSError, ValueError) as e:
            print(f"[error] {e}")
            return EXIT_FAILED
        print(f"{args.file}: {r['width']}x{r['height']} -> "
              f"{r['verdict'].upper()} ({r['reason']})")
        print(f"  luma {r['luma_mean']} +/- {r['luma_stdev']}, "
              f"black {r['black_ratio']:.3f}, white {r['white_ratio']:.3f}")
        print(f"  colors {r['color_count']}, edges {r['edge_density']:.4f}, "
              f"saturation {r['saturation_mean']}")
        return 0 if r["usable"] else EXIT_NEGATIVE

    print("Usage: python -m livetools screenshot [grab|diff|stats]")
    return 2


# ── health command ────────────────────────────────────────────────────────

def cmd_health(args: argparse.Namespace) -> int:
    from . import health as hl

    if args.wait:
        hwnd = hl.wait_for_window(args.exe, timeout=args.wait)
        if not hwnd:
            print(f"[error] no window for {args.exe} within {args.wait}s")
            return EXIT_NEGATIVE
        print(f"Window {hwnd} appeared for {args.exe}")

    try:
        state = hl.check(args.exe, game_dir=args.game_dir,
                         frozen_check=args.frozen_check,
                         dismiss_dialogs=args.dismiss_dialogs,
                         frozen_ratio=(args.frozen_ratio
                                       if args.frozen_ratio is not None
                                       else hl.FROZEN_RATIO))
    except OSError as e:
        print(f"[error] {e}")
        return EXIT_FAILED

    print(f"{args.exe}: {state['verdict'].upper()} — {state['reason']}")
    print(f"  pid={state['pid']} hwnd={state['hwnd']} "
          f"responding={state['responding']}")
    if state["frame"]:
        print(f"  frame: {state['frame']['verdict']} "
              f"({state['frame']['reason']})")
    if state.get("freeze_ratio") is not None:
        print(f"  freeze check: {state['freeze_ratio']:.4f} changed ratio")
    for window in state["error_windows"]:
        print(f"  [dialog] {window['class_name']}: {window['title']}")
    for window in state.get("dismissed", []):
        print(f"  [dismissed] {window['title'] or window['class_name']}")
    for line in state["fatal_log_lines"]:
        print(f"  [log] {line}")
    return 0 if state["verdict"] == "ok" else EXIT_NEGATIVE


# ── proc command ──────────────────────────────────────────────────────────

def cmd_proc(args: argparse.Namespace) -> int:
    from . import procctl as pc
    action = getattr(args, "proc_action", None)

    try:
        if action == "status":
            info = pc.status(args.exe)
            print(f"{info['exe']}: {info['count']} instance(s) {info['pids']}")
            print(f"  window hwnd={info['hwnd']} pid={info['window_pid']} "
                  f"title={info['title']!r}")
            if info["count"] > 1:
                print("  [warn] more than one instance — stop them all before "
                      "relaunching, window lookup picks the first match")
            return 0 if info["count"] else EXIT_NEGATIVE

        if action == "stop":
            r = pc.stop(args.exe, timeout=args.timeout, force=not args.no_force)
            print(f"closed={r['closed']} terminated={r['terminated']} "
                  f"survivors={r['survivors']}")
            if not r["ok"]:
                print(f"[error] still running: {r['survivors']}")
            return 0 if r["ok"] else EXIT_FAILED

        if action == "start":
            r = pc.start(args.exe_path, wait=args.wait)
            print(f"Launched pid={r['pid']} hwnd={r['hwnd']}")
            if not r["ok"]:
                print(f"[error] no window within {args.wait}s")
            return 0 if r["ok"] else EXIT_NEGATIVE

        if action == "restart":
            r = pc.restart(args.exe_path, wait=args.wait,
                           stop_timeout=args.timeout)
            print(f"stop: {r['stopped']['ok']}  start: "
                  f"{r['started']['ok'] if r['started'] else 'skipped'}")
            if not r["ok"]:
                print(f"[error] {r.get('error', 'window did not appear')}")
            return 0 if r["ok"] else EXIT_FAILED

        if action == "keep-awake":
            print(f"Holding sleep/display off for {args.duration:.0f}s "
                  "(run in background)")
            pc.keep_awake(args.duration)
            print("Released.")
            return 0

        print("Usage: python -m livetools proc "
              "[status|stop|start|restart|keep-awake]")
        return 2
    except (OSError, FileNotFoundError) as e:
        print(f"[error] {e}")
        return EXIT_FAILED


# ── remix command ─────────────────────────────────────────────────────────

def cmd_remix(args: argparse.Namespace) -> int:
    from . import remixctl as rx
    action = getattr(args, "rx_action", None)

    if action == "status":
        info = rx.detect_runtime(args.game_dir)
        print(f"Game dir:  {info['game_dir']}")
        d3d9 = info["d3d9_dll"]
        print(f"d3d9.dll:  {'present, %d bytes' % d3d9['size'] if d3d9['present'] else 'MISSING'}")
        if info["remix_markers"]:
            print("Remix markers:")
            for m in info["remix_markers"]:
                print(f"  - {m}")
        else:
            print("Remix markers: none found (runtime not detected)")
        print("Config surfaces:")
        for surface, cfg in info["configs"].items():
            state = (f"{cfg['options']} option(s)" if cfg["present"]
                     else "not present")
            print(f"  {surface:<7s} {Path(cfg['path']).name:<12s} {state}")
        print(f"rtx.conf:  {info['rtx_conf'] or 'not present'}")
        if info["rtx_conf"]:
            options = rx.load_conf(info["rtx_conf"])
            print(f"  {len(options)} option(s) set")
            for k in sorted(options):
                print(f"    {k} = {options[k]}")
        if info["logs"]:
            print("Logs:")
            for lg in info["logs"]:
                print(f"  {lg['name']:<32s} {lg['size']:>10d} B  {lg['mtime']}")

    elif action == "conf":
        sub = args.conf_action
        if not sub:
            print("Usage: python -m livetools remix conf "
                  "[get|set|unset|add-hash|remove-hash]")
            return 2
        conf = rx.conf_path(args.game_dir, args.surface)
        if sub == "get":
            options = rx.load_conf(conf)
            if args.key:
                val = options.get(args.key)
                print(f"{args.key} = {val}" if val is not None
                      else f"{args.key} is not set")
            else:
                if not options:
                    print(f"No options set ({conf} "
                          f"{'is empty' if conf.is_file() else 'does not exist'})")
                for k in sorted(options):
                    print(f"{k} = {options[k]}")
        elif sub == "set":
            from . import rtx_options as ro
            if args.surface != "rtx":
                # Only rtx.conf has a generated option reference upstream.
                bak = rx.set_option(conf, args.key, args.value,
                                    backup=not args.no_backup,
                                    backup_dir=args.backup_dir)
                print(f"{args.key} = {args.value}  written to {conf}")
                if bak:
                    print(f"  backup: {bak}")
                print("  (takes effect on next game launch)")
                return 0
            if not ro.is_known(args.key) and not args.force:
                print(f"[error] {args.key} is not a known dxvk-remix option — "
                      "the runtime would ignore it silently.")
                near = ro.suggest(args.key)
                if near:
                    print(f"  did you mean: {', '.join(near)}")
                print("  search with: python -m livetools remix options "
                      f"search {args.key.split('.')[-1]}")
                print("  write it anyway with --force")
                return EXIT_FAILED
            problem = ro.validate_value(args.key, args.value)
            if problem and not args.force:
                entry = ro.lookup(args.key)
                print(f"[error] {args.key} {problem}")
                print(f"  type={entry['type']} default={entry['default'] or '-'}")
                print("  write it anyway with --force")
                return EXIT_FAILED
            bak = rx.set_option(conf, args.key, args.value,
                                backup=not args.no_backup,
                                backup_dir=args.backup_dir)
            print(f"{args.key} = {args.value}  written to {conf}")
            if bak:
                print(f"  backup: {bak}")
            print("  (takes effect on next game launch)")
        elif sub == "unset":
            removed = rx.unset_option(conf, args.key,
                                      backup=not args.no_backup,
                                      backup_dir=args.backup_dir)
            print(f"{args.key} {'removed from' if removed else 'was not set in'} {conf}")
        elif sub == "add-hash":
            from . import rtx_options as ro
            if args.key not in rx.HASH_SET_OPTIONS and not args.force:
                print(f"[error] {args.key} is not a hash-set option — a hash "
                      "written to a non-hash option is ignored silently.")
                print(f"  hash-set options: {', '.join(sorted(rx.HASH_SET_OPTIONS))}")
                print("  write it anyway with --force")
                return EXIT_FAILED
            problem = ro.validate_value(args.key, args.hash)
            if problem and not args.force:
                print(f"[error] {args.key} {problem}")
                return EXIT_FAILED
            hashes = rx.add_hash(conf, args.key, args.hash,
                                 backup=not args.no_backup,
                                 backup_dir=args.backup_dir)
            print(f"{args.key} = {', '.join(hashes)}")
        elif sub == "remove-hash":
            hashes = rx.remove_hash(conf, args.key, args.hash,
                                    backup=not args.no_backup,
                                    backup_dir=args.backup_dir)
            print(f"{args.key} = {', '.join(hashes) if hashes else '(empty, removed)'}")
        else:
            print("Usage: python -m livetools remix conf "
                  "[get|set|unset|add-hash|remove-hash]")
            return 2

    elif action == "preset":
        sub = args.preset_action
        if sub == "list":
            for name in sorted(rx.PRESETS):
                surfaces = "+".join(sorted(rx.PRESETS[name]["options"]))
                print(f"  {name:<20s} [{surfaces:<11s}] "
                      f"{rx.PRESETS[name]['description']}")
        elif sub == "apply":
            if args.name not in rx.PRESETS:
                print(f"[error] Unknown preset '{args.name}'. "
                      f"Available: {', '.join(sorted(rx.PRESETS))}")
                return EXIT_FAILED
            applied = rx.apply_preset(args.game_dir, args.name,
                                      backup=not args.no_backup,
                                      backup_dir=args.backup_dir)
            print(f"Applied preset '{args.name}':")
            for surface, options in applied["options"].items():
                print(f"  {applied['paths'][surface]}")
                for k, v in options.items():
                    print(f"    {k} = {v}")
            print("  (takes effect on next game launch)")
        else:
            print("Usage: python -m livetools remix preset [list|apply]")
            return 2

    elif action == "menu":
        try:
            r = rx.toggle_menu(getattr(args, "exe", None),
                               getattr(args, "window", None),
                               chord=args.chord)
        except OSError as e:
            print(f"[error] {e}")
            return EXIT_FAILED
        if r.get("ok"):
            print(f"Sent {r['combo']} (focused={r.get('focused')}). "
                  "Screenshot to verify the menu state.")
        else:
            print(f"[error] {r.get('error', '?')}")
            return EXIT_FAILED

    elif action == "log":
        logs = rx.read_logs(args.game_dir, tail=args.tail,
                            errors_only=args.errors)
        if not logs:
            print("No Remix/dxvk logs found.")
        for name, lines in logs.items():
            print(f"== {name} (last {len(lines)} line(s)) ==")
            for ln in lines:
                print(f"  {ln}")

    elif action == "capture":
        sub = args.capture_action
        if sub == "list":
            captures = rx.list_captures(args.game_dir)
            if not captures:
                print(f"No captures in {rx.capture_root(args.game_dir)}")
            for c in captures:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(c["mtime"]))
                print(f"  {c['name']:<48s} {c['size']:>12d} B  {stamp}")
        elif sub == "trigger":
            try:
                r = rx.trigger_capture(args.game_dir,
                                       exe=getattr(args, "exe", None),
                                       window=getattr(args, "window", None),
                                       chord=args.chord, timeout=args.timeout)
            except OSError as e:
                print(f"[error] {e}")
                return EXIT_FAILED
            if r.get("ok"):
                print(f"Captured {r['capture']['name']} "
                      f"({r['capture']['size']} B) via {r['chord']}")
                print("  Read the exported assets with: "
                      "python -m livetools remix capture assets "
                      f"-d {args.game_dir}")
            else:
                print(f"[error] {r.get('error', '?')}")
                return EXIT_NEGATIVE
        elif sub == "assets":
            found = rx.capture_assets(args.game_dir)
            print(f"Capture root: {found['root']}")
            print(f"  {len(found['captures'])} capture(s)")
            if not found["assets"]:
                print("  No exported assets — take a capture first "
                      "(remix capture trigger)")
            for category, entries in sorted(found["assets"].items()):
                print(f"== {category} ({len(entries)}) ==")
                for e in entries[:args.limit]:
                    print(f"  {e['hash']}  {Path(e['file']).name}")
                if len(entries) > args.limit:
                    print(f"  ... {len(entries) - args.limit} more "
                          f"(raise --limit)")
        else:
            print("Usage: python -m livetools remix capture "
                  "[list|trigger|assets]")
            return 2

    elif action == "options":
        from . import rtx_options as ro
        sub = args.options_action
        if sub == "search":
            hits = ro.search(args.term, limit=args.limit)
            if not hits:
                print(f"No option matches {args.term!r}")
            for entry in hits:
                bounds = "".join(f" {label}={entry[label]}"
                                 for label in ("min", "max") if entry[label])
                print(f"{entry['name']}  [{entry['type']}] "
                      f"default={entry['default'] or '-'}{bounds}")
                if entry["description"]:
                    print(f"    {entry['description'][:300]}")
        elif sub == "show":
            entry = ro.lookup(args.name)
            if not entry:
                print(f"[error] {args.name} is not a known option")
                near = ro.suggest(args.name)
                if near:
                    print(f"  did you mean: {', '.join(near)}")
                return EXIT_FAILED
            for field in ("name", "type", "default", "min", "max"):
                if entry[field]:
                    print(f"{field:>8s}: {entry[field]}")
            if entry["description"]:
                print(f"\n{entry['description']}")
        elif sub == "sync":
            try:
                count = ro.sync(args.source or ro.RTX_OPTIONS_URL)
            except (OSError, ValueError) as e:
                print(f"[error] option sync failed: {e}")
                return EXIT_FAILED
            print(f"Wrote {count} options to {ro.DATA_FILE}")
        else:
            print("Usage: python -m livetools remix options "
                  "[search|show|sync]")
            return 2

    elif action == "debugviews":
        from .remixctl import DEBUG_VIEWS
        for name, idx in sorted(DEBUG_VIEWS.items(), key=lambda kv: kv[1]):
            print(f"  {idx:>4d}  {name}")

    else:
        print("Usage: python -m livetools remix "
              "[status|conf|preset|menu|log|capture|options|debugviews]")
        return 2
    return 0


# ── argument parser ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m livetools",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")

    # -- session --
    sp = sub.add_parser("attach",
        help="Attach to a running process (starts background daemon)",
        description="Attach to a running process by name or PID. "
                    "Starts a background Frida daemon that stays connected.\n\n"
                    "Use --spawn to launch the executable instead of attaching\n"
                    "to an already-running process. The process starts suspended,\n"
                    "Frida instruments it, then resumes -- catching all init code.\n\n"
                    "Example:\n"
                    "  python -m livetools attach game.exe\n"
                    "  python -m livetools attach 12345\n"
                    "  python -m livetools attach \"C:/Games/game.exe\" --spawn")
    sp.add_argument("target",
        help="Process name (e.g. game.exe), PID, or full path with --spawn")
    sp.add_argument("--spawn", action="store_true",
        help="Launch the executable with Frida (spawn mode) instead of "
             "attaching to an already-running process")

    sub.add_parser("detach",
        help="Detach from the process and stop the daemon")
    sub.add_parser("status",
        help="Show current state: attached process, frozen status, bp count")

    # -- breakpoints --
    sp = sub.add_parser("bp",
        help="Manage breakpoints (add / del / list)")
    bp_sub = sp.add_subparsers(dest="action")
    bp_add = bp_sub.add_parser("add", help="Set a breakpoint at address")
    bp_add.add_argument("addr", help="Code address in hex (e.g. 0x401000)")
    bp_del = bp_sub.add_parser("del", help="Remove a breakpoint")
    bp_del.add_argument("addr", help="Address of breakpoint to remove (hex)")
    bp_sub.add_parser("list", help="List all active breakpoints with hit counts")

    # -- watch --
    sp = sub.add_parser("watch",
        help="Block until a breakpoint is hit, then print snapshot",
        description=(
            "Wait for a breakpoint to be hit, then print a full snapshot.\n\n"
            "NOTE: Some games only execute rendering/logic when their window\n"
            "is focused. If watch times out, make sure the game window is in\n"
            "the foreground."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--timeout", type=int, default=60,
        help="Seconds to wait before giving up (default: 60)")

    # -- inspection --
    sub.add_parser("regs", help="Print all registers (x86/x64)")
    sp = sub.add_parser("stack", help="Dump stack slots from ESP/RSP")
    sp.add_argument("count", nargs="?", type=int, default=16,
        help="Number of pointer-sized slots (default: 16)")

    sp = sub.add_parser("mem", help="Read or write live process memory")
    mem_sub = sp.add_subparsers(dest="mem_action")
    mr = mem_sub.add_parser("read",
        help="Read N bytes at address (hex dump + type interpretation)")
    mr.add_argument("addr", help="Start address in hex (e.g. 0x401000)")
    mr.add_argument("size", type=int, help="Number of bytes to read")
    mr.add_argument("--as", dest="type", default=None,
        choices=["float32", "float64", "half", "uint8", "int8", "uint16",
                 "int16", "uint32", "int32", "ptr", "ascii", "utf16"],
        help="Interpret bytes as a specific type")
    mw = mem_sub.add_parser("write", help="Write hex bytes to address")
    mw.add_argument("addr", help="Target address in hex")
    mw.add_argument("hex_bytes", help="Hex bytes (e.g. '90 90 90' or 'B001C3')")
    ma = mem_sub.add_parser("alloc", help="Allocate rwx memory in target process")
    ma.add_argument("size", type=int, help="Number of bytes to allocate")

    sp = sub.add_parser("disasm",
        help="Disassemble instructions at address (default: current EIP/RIP)")
    sp.add_argument("addr", nargs="?", default=None, help="Start address in hex")
    sp.add_argument("-n", "--count", type=int, default=16,
        help="Number of instructions (default: 16)")

    sub.add_parser("bt", help="Print stack backtrace")

    # -- control --
    sp = sub.add_parser("step",
        help="Single-step one instruction (must be frozen at a bp)")
    sp.add_argument("mode", nargs="?", default="over",
        choices=["over", "into", "out"],
        help="'over' skips calls, 'into' enters, 'out' runs to return (default: over)")

    sub.add_parser("resume", help="Resume execution (unfreeze from breakpoint)")

    # -- scan --
    sp = sub.add_parser("scan", help="Scan process memory for a byte pattern")
    sp.add_argument("pattern", help="Hex byte pattern (e.g. '00 00 80 3F')")
    sp.add_argument("--range", default=None,
        help="Restrict scan to START:SIZE (e.g. 0x400000:0x100000)")

    # -- trace --
    sp = sub.add_parser("trace",
        help="Non-blocking function enter/leave tracing with data capture",
        description=(
            "Hook a function's entry and exit without freezing the target.\n"
            "Reads specified data at each call, returns structured results.\n\n"
            "Read spec format (semicolon-separated):\n"
            "  register:       ecx, eax, ebp, ...\n"
            "  memory:         [reg+OFFSET]:SIZE:TYPE\n"
            "  double-deref:   *[reg+OFFSET]:SIZE:TYPE\n"
            "  Types: hex, float32, float64, uint32, int32, uint16, int16,\n"
            "         uint8, int8, ascii, utf16, ptr\n\n"
            "Filter format:\n"
            "  [esp+8]==0x2  |  eax!=0  |  [ecx+0x54]:4:float32>0.5\n\n"
            "Examples:\n"
            "  python -m livetools trace 0x401000 --count 10 "
            '--read "ecx; [esp+4]:12:float32"\n'
            "  python -m livetools trace 0x402000 --count 5 "
            '--filter "[esp+8]==0x2" --read "[esp+c]:64:float32"\n'
            "  python -m livetools trace 0x403000 --count 20 "
            '--read "ecx; [esp+4]:12:float32" --read-leave "eax"\n\n'
            "NOTE: Some games only execute rendering/logic when their window\n"
            "is focused. If trace times out with 0 samples, alt-tab to the\n"
            "game before running."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("addr", help="Function address to hook (hex)")
    sp.add_argument("--count", type=int, default=10,
        help="Number of calls to capture (default: 10)")
    sp.add_argument("--read", default=None,
        help='Read spec for function entry (e.g. "ecx; [esp+4]:12:float32")')
    sp.add_argument("--read-leave", default=None,
        help='Read spec for function exit (e.g. "eax; st0")')
    sp.add_argument("--filter", default=None,
        help='Filter expression (e.g. "[esp+8]==0x2")')
    sp.add_argument("--timeout", type=int, default=30,
        help="Max seconds to wait for all samples (default: 30)")
    sp.add_argument("--output", default=None,
        help="Write samples to JSONL file (default: stdout only)")

    # -- steptrace --
    sp = sub.add_parser("steptrace",
        help="Instruction-level execution recording via Stalker",
        description=(
            "Record every instruction executed from function entry through\n"
            "return (or a configurable limit). Uses Frida Stalker for real-time\n"
            "instruction-level tracing.\n\n"
            "Detail levels:\n"
            "  full      Every instruction + register snapshots at calls/rets\n"
            "  branches  All instructions logged, regs at branches (default)\n"
            "  blocks    Only instruction addresses, cheapest\n\n"
            "Examples:\n"
            "  python -m livetools steptrace 0x403000 "
            "--max-insn 500 --call-depth 1 --detail full\n"
            "  python -m livetools steptrace 0x401000 "
            "--max-insn 1000 --detail branches\n"
            "  python -m livetools steptrace 0x402000 "
            "--max-insn 5000 --detail blocks\n\n"
            "NOTE: Some games only execute rendering/logic when their window\n"
            "is focused. If steptrace times out with 0 instructions, alt-tab\n"
            "to the game before running."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("addr", help="Function address to trace (hex)")
    sp.add_argument("--max-insn", type=int, default=1000,
        help="Max instructions to record before stopping (default: 1000)")
    sp.add_argument("--call-depth", type=int, default=0,
        help="How many call levels to follow into (default: 0 = entry only)")
    sp.add_argument("--detail", default="branches",
        choices=["full", "branches", "blocks"],
        help="Detail level: full|branches|blocks (default: branches)")
    sp.add_argument("--timeout", type=int, default=30,
        help="Max seconds to wait (default: 30)")
    sp.add_argument("--output", default=None,
        help="Write trace to JSONL file")

    # -- collect --
    sp = sub.add_parser("collect",
        help="Long-running multi-function data collection with intervals",
        description=(
            "Collect data from one or more functions over a duration.\n"
            "Optionally partition records into intervals via fence hooks.\n\n"
            "The fence concept:\n"
            "  --fence ADDR   Hook a boundary function (e.g. DX Present) that\n"
            "                 increments an interval counter. Every trace record\n"
            "                 includes the current interval ID, enabling per-frame\n"
            "                 or per-N-calls analysis.\n\n"
            "Output: JSONL file in patches/<exe>/traces/ by default.\n\n"
            "Examples:\n"
            "  python -m livetools collect 0x401000 0x402000 "
            "--duration 30 --output trace.jsonl "
            '--read "ecx; [esp+4]:12:float32" '
            "--fence 0x403000 "
            "--label 0x401000=FuncA --label 0x402000=FuncB\n\n"
            "  python -m livetools collect 0x401000 "
            "--duration 20 --fence-every 100 --output output.jsonl\n\n"
            "  python -m livetools collect 0x401000 0x402000 "
            '--read@0x401000="ecx; [esp+4]:12:float32" '
            '--read@0x402000="ecx; [esp+4]:28:hex" '
            "--duration 15 --output multi.jsonl\n\n"
            "NOTE: Some games only execute rendering/logic when their window\n"
            "is focused. If collect finishes with 0 records, alt-tab to the\n"
            "game before running."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("addrs", nargs="+",
        help="One or more function addresses to hook (hex)")
    sp.add_argument("--duration", type=int, default=10,
        help="Collection duration in seconds (default: 10)")
    sp.add_argument("--max-records", type=int, default=0,
        help="Stop after N total records (default: unlimited)")
    sp.add_argument("--output", default=None,
        help="Output JSONL file path (default: auto-generated)")
    sp.add_argument("--read", default=None,
        help='Read spec applied to all hooks (e.g. "ecx; [esp+4]:12:float32")')
    sp.add_argument("--read@", dest="read_at", action="append", default=None,
        help='Per-address read spec: ADDR=SPEC (e.g. 0x401000="ecx; [esp+4]:12:float32")')
    sp.add_argument("--fence", default=None,
        help="Address of fence function for interval marking (e.g. DX Present)")
    sp.add_argument("--fence-every", type=int, default=0,
        help="Mark interval every N calls to first traced addr")
    sp.add_argument("--label", action="append", default=None,
        help="Human label: ADDR=NAME (e.g. 0x401000=FuncA)")

    # -- modules --
    sp = sub.add_parser("modules",
        help="List loaded DLLs with base addresses and sizes",
        description=(
            "Enumerate all loaded modules (DLLs) in the target process.\n"
            "Useful for finding DLL bases for vtable hooks.\n\n"
            "Examples:\n"
            "  python -m livetools modules\n"
            "  python -m livetools modules --filter d3d\n"
            "  python -m livetools modules --filter kernel"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--filter", default=None,
        help="Case-insensitive substring filter on module name/path")

    # -- vishook --
    sp = sub.add_parser("vishook",
        help="Selective visibility override via code cave on a jmp trampoline",
        description=(
            "Patches a jmp trampoline to route through a code cave that\n"
            "selectively forces 'visible' for callers above a threshold\n"
            "address while letting callers below run the original function.\n\n"
            "Designed for __thiscall functions that return float on st(0)\n"
            "with ret 0x10 (4 stack args) and an optional byte output in\n"
            "[esp+0x10].\n\n"
            "Uses a code cave that checks the return address:\n"
            "  >= threshold  → force visible (float=102400.0, byte=1)\n"
            "  <  threshold  → call original function\n\n"
            "Examples:\n"
            "  python -m livetools vishook on 0x401000 0x402000\n"
            "  python -m livetools vishook on 0x401000 0x402000 --threshold 560000\n"
            "  python -m livetools vishook stats\n"
            "  python -m livetools vishook off"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    vhsub = sp.add_subparsers(dest="action")
    vhon = vhsub.add_parser("on", help="Enable selective visibility override")
    vhon.add_argument("jmp_site",
        help="Address of the jmp trampoline to patch (hex)")
    vhon.add_argument("orig_target",
        help="Address of the original target function (hex)")
    vhon.add_argument("--threshold", default="500000",
        help="Hex address threshold (default: 500000). "
             "Callers >= this get forced visible.")
    vhsub.add_parser("off", help="Disable override, restore original jmp")
    vhsub.add_parser("stats", help="Show override/passthrough call counts")

    # -- dipcnt --
    sp = sub.add_parser("dipcnt",
        help="Count DrawIndexedPrimitive calls (D3D9 vtable hook)")
    dc_sub = sp.add_subparsers(dest="action")
    dc_on = dc_sub.add_parser("on", help="Start counting DIP calls")
    dc_on.add_argument("dev_ptr",
        help="Address of the global IDirect3DDevice9* pointer (hex)")
    dc_sub.add_parser("off", help="Stop counting")
    dc_sub.add_parser("read", help="Read current count + delta since last read")
    cal_p = dc_sub.add_parser("callers", help="Sample N DIP calls and show caller histogram")
    cal_p.add_argument("count", nargs="?", type=int, default=200, help="Number of calls to sample (default 200)")

    # -- memwatch --
    sp = sub.add_parser("memwatch",
        help="Hardware memory write watchpoint (catch who writes to an address)",
        description=(
            "Set a memory write watchpoint on a specific address range.\n"
            "Uses Frida's MemoryAccessMonitor to detect writes, capturing\n"
            "the instruction pointer and backtrace for each hit.\n\n"
            "Examples:\n"
            "  python -m livetools memwatch start 0x7A0000 --size 48\n"
            "  python -m livetools memwatch read\n"
            "  python -m livetools memwatch stop"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mw_sub = sp.add_subparsers(dest="action")
    mw_start = mw_sub.add_parser("start", help="Start watching an address for writes")
    mw_start.add_argument("addr", help="Address to watch (hex, e.g. 0x7A0000)")
    mw_start.add_argument("--size", type=int, default=4,
        help="Number of bytes to watch (default: 4)")
    mw_start.add_argument("--max-hits", type=int, default=20,
        help="Auto-stop after N hits (default: 20)")
    mw_sub.add_parser("stop", help="Stop the active watchpoint")
    mw_sub.add_parser("read", help="Read captured hits")

    # -- analyze --
    sp = sub.add_parser("analyze",
        help="Offline JSONL aggregation and query (no Frida needed)",
        description=(
            "Pure Python offline analysis of JSONL files produced by\n"
            "'collect' or 'trace --output'. Provides deterministic,\n"
            "non-hallucinated aggregation and filtering.\n\n"
            "Field path syntax: dot-separated with array indices.\n"
            "  enter.reads.0.value.0  = first read spec's first value\n"
            "  leave.eax              = EAX at function exit\n"
            "  addr                   = hooked address\n"
            "  interval               = fence counter value\n\n"
            "Examples:\n"
            "  python -m livetools analyze trace.jsonl --summary\n"
            "  python -m livetools analyze trace.jsonl --group-by addr\n"
            "  python -m livetools analyze trace.jsonl "
            '--filter "addr==00401000" --group-by "leave.eax"\n'
            "  python -m livetools analyze trace.jsonl "
            '--filter "addr==00401000" --cross-tab caller leave.eax\n'
            "  python -m livetools analyze trace.jsonl "
            "--group-by interval --top 5\n"
            "  python -m livetools analyze trace.jsonl --interval 47\n"
            "  python -m livetools analyze trace.jsonl "
            "--compare-intervals 10 50\n"
            "  python -m livetools analyze trace.jsonl "
            '--filter "addr==00401000" --histogram "enter.reads.0.value.0"\n'
            "  python -m livetools analyze trace.jsonl "
            '--filter "addr==00401000" --export-csv output.csv'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("file", help="Path to JSONL file")
    sp.add_argument("--summary", action="store_true",
        help="Show record count, unique addrs, interval count, time span")
    sp.add_argument("--group-by", default=None,
        help="Group records by a field path and show distribution")
    sp.add_argument("--filter", default=None,
        help='Keep only records matching expression (e.g. "addr==005E72E0")')
    sp.add_argument("--cross-tab", nargs=2, default=None, metavar=("F1", "F2"),
        help="Cross-tabulate two fields")
    sp.add_argument("--top", type=int, default=20,
        help="Show top N groups (default: 20)")
    sp.add_argument("--interval", type=int, default=None,
        help="Show detail for a specific interval number")
    sp.add_argument("--intervals", default=None,
        help="Show records for interval range N:M")
    sp.add_argument("--compare-intervals", nargs=2, type=int, default=None,
        metavar=("A", "B"),
        help="Diff two intervals side by side")
    sp.add_argument("--histogram", default=None,
        help="Show value distribution histogram for a field path")
    sp.add_argument("--export-csv", default=None,
        help="Export filtered/grouped data as CSV to file")

    # -- gamectl --
    sp = sub.add_parser("gamectl",
        help="Send keystrokes/mouse clicks directly to a game window (no Frida, no focus needed)",
        description=(
            "Posts WM_KEYDOWN/WM_KEYUP directly to the target window handle.\n"
            "No focus stealing — works even when the game is in the background.\n\n"
            "Window lookup (pick one):\n"
            "  --exe game.exe      find window by process exe name (recommended)\n"
            "  --window <hint>     find window by title substring\n\n"
            "Key names: RETURN, ESCAPE, SPACE, UP, DOWN, LEFT, RIGHT,\n"
            "           TAB, F1-F12, A-Z, 0-9, NUMPAD0-9, SHIFT, CTRL, ALT\n\n"
            "Sequence token syntax:\n"
            "  KEY_NAME          — keydown + keyup\n"
            "  WAIT:N            — pause N milliseconds\n"
            "  HOLD:KEY_NAME:N   — hold key N ms before keyup\n"
            "  CHORD:A+B         — press together, release reverse (CHORD:ALT+X)\n\n"
            "Examples:\n"
            "  python -m livetools gamectl --exe revolt_xbox.exe info\n"
            "  python -m livetools gamectl --exe revolt_xbox.exe key RETURN\n"
            "  python -m livetools gamectl --exe revolt_xbox.exe keys \"DOWN DOWN RETURN\"\n"
            "  python -m livetools gamectl --exe revolt_xbox.exe keys "
            "\"RETURN WAIT:1000 RETURN WAIT:1000 RETURN\" --delay-ms 0\n"
            "  python -m livetools gamectl --exe revolt_xbox.exe click 400 300\n"
            "  python -m livetools gamectl --exe revolt_xbox.exe macro "
            "--macro-file patches/revolt/macros.json navigate_menu"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--exe", "-e", default=None,
        help="Target process exe name (e.g. revolt_xbox.exe) — preferred over --window")
    sp.add_argument("--window", "-w", default=None,
        help="Window title substring fallback (case-insensitive)")
    gc_sub = sp.add_subparsers(dest="gc_action")

    gc_sub.add_parser("info", help="Show hwnd, title, pid for the matched window")

    gc_key = gc_sub.add_parser("key", help="Send a single key press or chord")
    gc_key.add_argument("key_name",
        help="Key name (e.g. RETURN, UP, F5, A) or chord (e.g. ALT+X)")
    gc_key.add_argument("--hold-ms", type=int, default=50,
        help="Hold duration in ms (default: 50)")

    gc_keys = gc_sub.add_parser("keys", help="Send a space-separated key sequence")
    gc_keys.add_argument("sequence",
        help='e.g. "DOWN DOWN RETURN" or "RETURN WAIT:1000 RETURN"')
    gc_keys.add_argument("--delay-ms", type=int, default=200,
        help="Delay between keys in ms (default: 200)")

    gc_click = gc_sub.add_parser("click", help="Post left-click at client-area coordinates")
    gc_click.add_argument("x", type=int, help="Client X coordinate")
    gc_click.add_argument("y", type=int, help="Client Y coordinate")

    gc_move = gc_sub.add_parser("mousemove",
        help="Relative mouse motion — turns the camera (mouse-look)")
    gc_move.add_argument("dx", type=int, help="Horizontal delta (right positive)")
    gc_move.add_argument("dy", type=int, help="Vertical delta (down positive)")
    gc_move.add_argument("--steps", type=int, default=8,
        help="Split the motion across N events (default: 8) — games clamp "
             "or drop a single huge delta")
    gc_move.add_argument("--step-ms", type=int, default=10,
        help="Delay between steps in ms (default: 10)")

    gc_macro = gc_sub.add_parser("macro", help="Run a named macro from a JSON file")
    gc_macro.add_argument("macro_name", help="Macro name to execute")
    gc_macro.add_argument("--macro-file", default="macros.json",
        help="Path to macro JSON file (default: macros.json)")
    gc_macro.add_argument("--delay-ms", type=int, default=200,
        help="Delay between keys in ms (default: 200)")

    gc_macros = gc_sub.add_parser("macros", help="List all macros in a JSON file")
    gc_macros.add_argument("--macro-file", default="macros.json",
        help="Path to macro JSON file (default: macros.json)")

    gc_save = gc_sub.add_parser("macro-save",
        help="Record a working input sequence so restarts can replay it",
        description=(
            "Save a menu path the moment it is known to work. Every rtx.conf\n"
            "change costs a game restart, so an unrecorded path gets\n"
            "rediscovered input by input every time.\n\n"
            "Example:\n"
            "  python -m livetools gamectl macro-save title_to_gameplay \\\n"
            "    --steps \"RETURN WAIT:1500 DOWN DOWN RETURN WAIT:3000\" \\\n"
            "    --description \"title -> first level, camera at spawn\" \\\n"
            "    --macro-file patches/MyGame/macros.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    gc_save.add_argument("macro_name", help="Macro key to write")
    gc_save.add_argument("--steps", required=True,
        help="Token sequence in `keys` syntax (KEY, WAIT:ms, HOLD:KEY:ms, CHORD:A+B)")
    gc_save.add_argument("--description", default=None,
        help="What this path does and where it ends up")
    gc_save.add_argument("--macro-file", default="macros.json",
        help="Path to macro JSON file (default: macros.json)")

    # -- screenshot --
    sp = sub.add_parser("screenshot",
        help="Capture the game window to PNG, or diff two captures",
        description=(
            "Capture a window's pixels (PrintWindow, BitBlt fallback) or\n"
            "compare two PNG captures pixel-by-pixel.\n\n"
            "Capture works for windowed/borderless games; exclusive\n"
            "fullscreen bypasses GDI and captures black — run windowed.\n\n"
            "Diff prints the changed-pixel ratio and bounding box. Use it to\n"
            "verify a menu opened, a debug view engaged, or to detect\n"
            "geometry-hash debug view flicker across identical frames\n"
            "(= unstable Remix hashes).\n\n"
            "Examples:\n"
            "  python -m livetools screenshot grab --exe game.exe --out shot.png\n"
            "  python -m livetools screenshot grab --window \"Game\" --full-window\n"
            "  python -m livetools screenshot diff before.png after.png\n"
            "  python -m livetools screenshot diff f1.png f2.png --threshold 0.02\n"
            "  python -m livetools screenshot diff f1.png f2.png --tiles 4x3\n"
            "  python -m livetools screenshot stats shot.png"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ss_sub = sp.add_subparsers(dest="ss_action")

    ss_grab = ss_sub.add_parser("grab", help="Capture the game window to a PNG")
    ss_grab.add_argument("--exe", "-e", default=None,
        help="Target process exe name (e.g. game.exe)")
    ss_grab.add_argument("--window", "-w", default=None,
        help="Window title substring fallback (case-insensitive)")
    ss_grab.add_argument("--out", "-o", default=None,
        help="Output PNG path (default: screenshot_<timestamp>.png)")
    ss_grab.add_argument("--full-window", action="store_true",
        help="Capture the full window incl. title bar (default: client area)")

    ss_diff = ss_sub.add_parser("diff", help="Compare two PNG captures")
    ss_diff.add_argument("file_a", help="First PNG")
    ss_diff.add_argument("file_b", help="Second PNG")
    ss_diff.add_argument("--threshold", type=float, default=0.01,
        help="Changed-pixel ratio at/above which verdict is CHANGED (default: 0.01)")
    ss_diff.add_argument("--tolerance", type=int, default=4,
        help="Per-channel delta still counted as same (default: 4)")
    ss_diff.add_argument("--expect", choices=("changed", "unchanged"),
        default="changed",
        help="What a pass looks like here. Navigation expects `changed` (the "
             "input did something); a hash-flicker or regression check expects "
             "`unchanged`. Exit 3 means the observation did not match "
             "(default: changed)")
    ss_diff.add_argument("--tiles", metavar="COLSxROWS", default=None,
        help="Also report per-region ratios on a grid, e.g. 4x3 — localizes "
             "change to HUD vs world")

    ss_stats = ss_sub.add_parser("stats",
        help="Classify one capture: black / blank / flat / content")
    ss_stats.add_argument("file", help="PNG to analyze")
    ss_stats.add_argument("--stride", type=int, default=4,
        help="Sample every Nth pixel per axis (default: 4)")

    # -- health --
    sp = sub.add_parser("health",
        help="Is the game running, responding, rendering, or crashed?",
        description=(
            "One probe, one verdict: not-running / crashed / no-window /\n"
            "hung / not-rendering / frozen / ok.\n\n"
            "This is the watchdog an unattended run calls at the top of every\n"
            "iteration — each verdict maps to a different recovery, and they\n"
            "are indistinguishable from a screenshot alone.\n\n"
            "Examples:\n"
            "  python -m livetools health --exe game.exe\n"
            "  python -m livetools health --exe game.exe -d \"C:/Games/MyGame\"\n"
            "  python -m livetools health --exe game.exe --wait 60\n"
            "  python -m livetools health --exe game.exe --frozen-check 2.0\n"
            "  python -m livetools health --exe game.exe --dismiss-dialogs"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("--exe", "-e", required=True,
        help="Game executable name (e.g. game.exe)")
    sp.add_argument("--game-dir", "-d", default=None,
        help="Game directory — enables Remix/dxvk log scanning for fatals")
    sp.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
        help="First wait up to N seconds for the game window to appear")
    sp.add_argument("--frozen-check", type=float, default=0.0, metavar="SECONDS",
        help="Capture two frames N seconds apart; matching frames = frozen")
    sp.add_argument("--frozen-ratio", type=float, default=None, metavar="RATIO",
        help="Changed-pixel ratio under which those frames count as the same "
             "(default: the renderer noise floor)")
    sp.add_argument("--dismiss-dialogs", action="store_true",
        help="Close any error dialogs found (they block all later input)")

    # -- proc --
    sp = sub.add_parser("proc",
        help="Game process lifecycle: start, stop, restart, keep-awake",
        description=(
            "rtx.conf is read at launch, so every Remix setting change costs a\n"
            "restart. restart stops the old instance (gracefully, then hard),\n"
            "launches the exe, and waits for its window before returning.\n\n"
            "keep-awake blocks Windows sleep and display blanking for the\n"
            "length of an overnight run — a slept machine stops delivering\n"
            "input and captures black.\n\n"
            "Examples:\n"
            "  python -m livetools proc status --exe game.exe\n"
            "  python -m livetools proc stop --exe game.exe\n"
            "  python -m livetools proc restart \"C:/Games/MyGame/game.exe\"\n"
            "  python -m livetools proc keep-awake --duration 43200"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    pc_sub = sp.add_subparsers(dest="proc_action")

    pc_status = pc_sub.add_parser("status", help="List instances and the windowed one")
    pc_status.add_argument("--exe", "-e", required=True)

    pc_stop = pc_sub.add_parser("stop", help="Close every instance of the game")
    pc_stop.add_argument("--exe", "-e", required=True)
    pc_stop.add_argument("--timeout", type=float, default=15.0,
        help="Seconds to wait for a graceful exit (default: 15)")
    pc_stop.add_argument("--no-force", action="store_true",
        help="Report survivors instead of terminating them")

    pc_start = pc_sub.add_parser("start", help="Launch the game and wait for its window")
    pc_start.add_argument("exe_path", help="Full path to the executable")
    pc_start.add_argument("--wait", type=float, default=60.0,
        help="Seconds to wait for the window (default: 60)")

    pc_restart = pc_sub.add_parser("restart", help="Stop, relaunch, verify")
    pc_restart.add_argument("exe_path", help="Full path to the executable")
    pc_restart.add_argument("--wait", type=float, default=90.0,
        help="Seconds to wait for the new window (default: 90)")
    pc_restart.add_argument("--timeout", type=float, default=15.0,
        help="Seconds to wait for a graceful exit (default: 15)")

    pc_awake = pc_sub.add_parser("keep-awake",
        help="Block sleep/display blanking (blocks; run in background)")
    pc_awake.add_argument("--duration", type=float, default=43200.0,
        help="Seconds to hold the request (default: 12 hours)")

    # -- remix --
    sp = sub.add_parser("remix",
        help="RTX Remix runtime control: rtx.conf, presets, dev menu, logs",
        description=(
            "Control the RTX Remix runtime (dxvk-remix) in a game directory.\n"
            "Durable settings go through rtx.conf (read at game launch); the\n"
            "dev menu hotkey (default ALT+X) is available for live toggles.\n\n"
            "Option catalog + compatibility playbook:\n"
            "  .claude/references/remix-compat-catalog.md\n\n"
            "Examples:\n"
            "  python -m livetools remix status --game-dir C:/Games/MyGame\n"
            "  python -m livetools remix conf set rtx.debugView.debugViewIdx 277 "
            "--game-dir C:/Games/MyGame\n"
            "  python -m livetools remix conf add-hash rtx.uiTextures 0x1234ABCD "
            "--game-dir C:/Games/MyGame\n"
            "  python -m livetools remix preset apply debug-geometry-hash "
            "--game-dir C:/Games/MyGame\n"
            "  python -m livetools remix menu --exe game.exe\n"
            "  python -m livetools remix log --game-dir C:/Games/MyGame --errors"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    rx_sub = sp.add_subparsers(dest="rx_action")

    rx_status = rx_sub.add_parser("status",
        help="Detect Remix runtime, list rtx.conf overrides and logs")
    rx_status.add_argument("--game-dir", "-d", required=True,
        help="Game directory containing d3d9.dll / rtx.conf")

    rx_conf = rx_sub.add_parser("conf", help="Read or edit rtx.conf options")
    rxc_sub = rx_conf.add_subparsers(dest="conf_action")

    def _conf_leaf(name: str, help_text: str) -> argparse.ArgumentParser:
        leaf = rxc_sub.add_parser(name, help=help_text)
        leaf.add_argument("--game-dir", "-d", required=True,
            help="Game directory containing the config files")
        leaf.add_argument("--surface", choices=("rtx", "dxvk", "bridge"),
            default="rtx",
            help="Which config file to edit: rtx.conf (renderer, default), "
                 "dxvk.conf (D3D9 layer — exclusive fullscreen), or "
                 "bridge.conf (32-bit bridge — forced windowed, DirectInput "
                 "forwarding)")
        leaf.add_argument("--no-backup", action="store_true",
            help="Skip the timestamped rtx.conf backup before writing")
        leaf.add_argument("--backup-dir", default=None,
            help="Where backups go (default: rtx-remix-backups/ in the game "
                 "directory; point it at patches/<Game>/backups to keep run "
                 "history with the project)")
        return leaf

    rxc_get = _conf_leaf("get", "Print one option or all options")
    rxc_get.add_argument("key", nargs="?", default=None,
        help="Option name (omit to list all)")
    rxc_set = _conf_leaf("set", "Set an option (idempotent)")
    rxc_set.add_argument("key", help="Option name, e.g. rtx.fallbackLightMode")
    rxc_set.add_argument("value", help="Value written verbatim")
    rxc_set.add_argument("--force", action="store_true",
        help="Write a key the option reference does not know (a newer runtime "
             "may have added it — run 'remix options sync' first)")
    rxc_unset = _conf_leaf("unset", "Remove an option")
    rxc_unset.add_argument("key", help="Option name to remove")
    rxc_add = _conf_leaf("add-hash",
        "Append a hash to a hash-set option (e.g. rtx.uiTextures)")
    rxc_add.add_argument("key", help="Hash-set option name")
    rxc_add.add_argument("hash", help="Hash value, e.g. 0x1234ABCD")
    rxc_add.add_argument("--force", action="store_true",
        help="Write to an option the reference does not list as a hash set")
    rxc_rem = _conf_leaf("remove-hash",
        "Remove a hash from a hash-set option")
    rxc_rem.add_argument("key", help="Hash-set option name")
    rxc_rem.add_argument("hash", help="Hash value to remove")

    rx_preset = rx_sub.add_parser("preset",
        help="List or apply named option bundles (debug views, hash rules, fallback light)")
    rxp_sub = rx_preset.add_subparsers(dest="preset_action")
    rxp_sub.add_parser("list", help="List available presets")
    rxp_apply = rxp_sub.add_parser("apply", help="Apply a preset to rtx.conf")
    rxp_apply.add_argument("name", help="Preset name (see 'preset list')")
    rxp_apply.add_argument("--game-dir", "-d", required=True,
        help="Game directory containing rtx.conf")
    rxp_apply.add_argument("--no-backup", action="store_true",
        help="Skip the timestamped rtx.conf backup before writing")
    rxp_apply.add_argument("--backup-dir", default=None,
        help="Where backups go (default: rtx-remix-backups/ in the game dir)")

    rx_menu = rx_sub.add_parser("menu",
        help="Toggle the Remix developer menu (sends ALT+X chord)")
    rx_menu.add_argument("--exe", "-e", default=None,
        help="Target process exe name")
    rx_menu.add_argument("--window", "-w", default=None,
        help="Window title substring fallback")
    rx_menu.add_argument("--chord", default="ALT+X",
        help="Key chord to send (default: ALT+X, matches rtx.remixMenuKeyBinds)")

    rx_log = rx_sub.add_parser("log",
        help="Tail Remix/dxvk logs in the game directory")
    rx_log.add_argument("--game-dir", "-d", required=True,
        help="Game directory to scan for logs")
    rx_log.add_argument("--tail", type=int, default=40,
        help="Lines from the end of each log (default: 40)")
    rx_log.add_argument("--errors", action="store_true",
        help="Only lines containing err/warn")

    rx_capture = rx_sub.add_parser("capture",
        help="Take USD captures and read the asset hashes they export",
        description=(
            "A capture is the unattended way to get the texture, material and\n"
            "mesh hashes that rtx.conf hash-set options need — the developer\n"
            "menu shows the same hashes but only to a human clicking through\n"
            "the texture tabs.\n\n"
            "  remix preset apply capture-ready -d DIR   (then restart)\n"
            "  remix capture trigger -d DIR --exe game.exe\n"
            "  remix capture assets -d DIR\n"
            "  remix conf add-hash rtx.uiTextures 0x<hash> -d DIR"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    rxc_sub = rx_capture.add_subparsers(dest="capture_action")

    rxc_list = rxc_sub.add_parser("list", help="List captures, newest last")
    rxc_list.add_argument("--game-dir", "-d", required=True)

    rxc_trigger = rxc_sub.add_parser("trigger",
        help="Send the capture hotkey and wait for the stage to be written")
    rxc_trigger.add_argument("--game-dir", "-d", required=True)
    rxc_trigger.add_argument("--exe", "-e", default=None,
        help="Target process exe name")
    rxc_trigger.add_argument("--window", "-w", default=None,
        help="Window title substring fallback")
    rxc_trigger.add_argument("--chord", default="CTRL+SHFT+Q",
        help="Capture hotkey (default: CTRL+SHFT+Q, matches rtx.captureHotKey)")
    rxc_trigger.add_argument("--timeout", type=float, default=30.0,
        help="Seconds to wait for the capture to finish writing (default: 30)")

    rxc_assets = rxc_sub.add_parser("assets",
        help="List asset hashes exported by captures, ready to tag")
    rxc_assets.add_argument("--game-dir", "-d", required=True)
    rxc_assets.add_argument("--limit", type=int, default=40,
        help="Entries printed per category (default: 40)")

    rx_options = rx_sub.add_parser("options",
        help="Search the full dxvk-remix option reference (offline)",
        description=(
            "Remix exposes ~1000 rtx.conf options and ignores unknown keys\n"
            "silently. This is the offline table used to validate a key before\n"
            "writing it and to find settings the playbook does not cover.\n\n"
            "Examples:\n"
            "  python -m livetools remix options search terrain\n"
            "  python -m livetools remix options search \"ghost\"\n"
            "  python -m livetools remix options show rtx.uniqueObjectDistance\n"
            "  python -m livetools remix options sync   (refresh from upstream)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    rxo_sub = rx_options.add_subparsers(dest="options_action")

    rxo_search = rxo_sub.add_parser("search",
        help="Find options by name or description")
    rxo_search.add_argument("term")
    rxo_search.add_argument("--limit", type=int, default=20,
        help="Maximum results (default: 20)")

    rxo_show = rxo_sub.add_parser("show", help="Full detail for one option")
    rxo_show.add_argument("name")

    rxo_sync = rxo_sub.add_parser("sync",
        help="Regenerate the table from dxvk-remix RtxOptions.md")
    rxo_sync.add_argument("--source", default=None,
        help="URL or local path to RtxOptions.md (default: dxvk-remix main)")

    rx_sub.add_parser("debugviews",
        help="List known debug view indices (rtx.debugView.debugViewIdx values)")

    return p


# ── main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """Dispatch a subcommand.

    Args:
        argv: Argument list; defaults to sys.argv[1:].

    Returns:
        The handler's exit code. Handlers that report a failure return
        non-zero so an unattended loop can branch on it instead of parsing
        prose out of stdout; handlers that return None exit 0.
    """
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    dispatch = {
        "attach": cmd_attach,
        "detach": cmd_detach,
        "status": cmd_status,
        "bp": cmd_bp,
        "watch": cmd_watch,
        "regs": cmd_regs,
        "stack": cmd_stack,
        "mem": lambda a: cmd_mem_read(a) if getattr(a, "mem_action", None) == "read"
                         else cmd_mem_write(a) if getattr(a, "mem_action", None) == "write"
                         else cmd_mem_alloc(a) if getattr(a, "mem_action", None) == "alloc"
                         else print("Usage: python -m livetools mem [read|write|alloc]"),
        "disasm": cmd_disasm,
        "bt": cmd_bt,
        "step": cmd_step,
        "resume": cmd_resume,
        "scan": cmd_scan,
        "trace": cmd_trace,
        "steptrace": cmd_steptrace,
        "collect": cmd_collect,
        "modules": cmd_modules,
        "vishook": cmd_vishook,
        "dipcnt": cmd_dipcnt,
        "memwatch": cmd_memwatch,
        "analyze": cmd_analyze,
        "gamectl": cmd_gamectl,
        "screenshot": cmd_screenshot,
        "health": cmd_health,
        "proc": cmd_proc,
        "remix": cmd_remix,
    }

    handler = dispatch.get(args.command)
    if not handler:
        parser.print_help()
        return 2
    return handler(args) or 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
