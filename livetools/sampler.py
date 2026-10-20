"""Sampling CPU profiler for a live process: no Frida, no injection.

Each sample suspends one thread, reads its instruction pointer and a slice of its stack, and resumes it
(Win32 SuspendThread/GetThreadContext; the Wow64 variants for 32-bit targets). Threads are sampled
round-robin; by default the busiest ones, measured over a short CPU pre-pass, so a 100-thread renderer
costs the same as a 1-thread game.

The recording is self-contained for offline work: a gzip JSONL whose header carries the process, its
module list (name, base, size, on-disk path) and the sampled threads (tid, start address, CPU share);
each sample line carries the thread, time, IP, SP and frame pointer, and the stack words that point into a
loaded module ("st") with their word index from SP ("si"): enough for an exact unwind offline.
`livetools.profile_report` symbolizes and aggregates it later, with or without the target alive.
"""
from __future__ import annotations

import bisect
import ctypes
import ctypes.wintypes as wt
import gzip
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
THREAD_SUSPEND_RESUME = 0x0002
THREAD_GET_CONTEXT = 0x0008
THREAD_QUERY_INFORMATION = 0x0040
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPTHREAD = 0x4
LIST_MODULES_ALL = 0x3
THREAD_QUERY_SET_WIN32_START_ADDRESS = 9

CONTEXT_AMD64_CONTROL_INTEGER = 0x00100003
CONTEXT_AMD64_SIZE = 0x4D0
CONTEXT_AMD64_FLAGS_OFF = 0x30
CONTEXT_AMD64_RSP_OFF = 0x98
CONTEXT_AMD64_RBP_OFF = 0xA0
CONTEXT_AMD64_RIP_OFF = 0xF8

WOW64_CONTEXT_CONTROL_INTEGER = 0x00010003
WOW64_CONTEXT_SIZE = 0x2CC
WOW64_CONTEXT_EBP_OFF = 0xB4
WOW64_CONTEXT_EIP_OFF = 0xB8
WOW64_CONTEXT_ESP_OFF = 0xC4

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
for _fn in (k32.OpenProcess, k32.OpenThread, k32.CreateToolhelp32Snapshot):
    _fn.restype = wt.HANDLE
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.SuspendThread.argtypes = k32.ResumeThread.argtypes = k32.Wow64SuspendThread.argtypes = [wt.HANDLE]
k32.SuspendThread.restype = k32.ResumeThread.restype = k32.Wow64SuspendThread.restype = wt.DWORD
k32.GetThreadContext.argtypes = k32.Wow64GetThreadContext.argtypes = [wt.HANDLE, ctypes.c_void_p]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE), wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
psapi.GetModuleInformation.argtypes = [wt.HANDLE, wt.HMODULE, ctypes.c_void_p, wt.DWORD]
ntdll.NtQueryInformationThread.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                           ctypes.POINTER(wt.ULONG)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG),
                ("dwFlags", wt.DWORD)]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wt.DWORD), ("EntryPoint", ctypes.c_void_p)]


def find_pid(target: str) -> int:
    """PID of `target` (a decimal pid or an exe name, case-insensitive); raises when absent."""
    if target.isdigit():
        return int(target)
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    try:
        pe = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == target.lower():
                return pe.th32ProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    raise LookupError(f"no process named {target}")


def thread_ids(pid: int) -> list[int]:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    try:
        out = []
        te = THREADENTRY32(dwSize=ctypes.sizeof(THREADENTRY32))
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                out.append(te.th32ThreadID)
            ok = k32.Thread32Next(snap, ctypes.byref(te))
        return out
    finally:
        k32.CloseHandle(snap)


def modules(hproc: int) -> list[dict]:
    """Every loaded module (32- and 64-bit views) as {name, base, size, path}."""
    needed = wt.DWORD()
    arr = (wt.HMODULE * 2048)()
    if not psapi.EnumProcessModulesEx(hproc, arr, ctypes.sizeof(arr), ctypes.byref(needed), LIST_MODULES_ALL):
        raise ctypes.WinError(ctypes.get_last_error())
    out = []
    for i in range(needed.value // ctypes.sizeof(wt.HMODULE)):
        info = MODULEINFO()
        path = ctypes.create_unicode_buffer(1024)
        psapi.GetModuleInformation(hproc, arr[i], ctypes.byref(info), ctypes.sizeof(info))
        psapi.GetModuleFileNameExW(hproc, arr[i], path, 1024)
        out.append({"name": Path(path.value).name, "base": info.lpBaseOfDll or 0, "size": info.SizeOfImage,
                    "path": path.value})
    return sorted(out, key=lambda m: m["base"])


def thread_cpu_100ns(tid: int) -> int | None:
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return None
    try:
        t = [ctypes.c_ulonglong() for _ in range(4)]
        if not k32.GetThreadTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return t[2].value + t[3].value
    finally:
        k32.CloseHandle(h)


def thread_start_address(tid: int) -> int:
    h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
    if not h:
        return 0
    try:
        addr = ctypes.c_void_p()
        ntdll.NtQueryInformationThread(h, THREAD_QUERY_SET_WIN32_START_ADDRESS, ctypes.byref(addr),
                                       ctypes.sizeof(addr), None)
        return addr.value or 0
    finally:
        k32.CloseHandle(h)


def busiest_threads(pid: int, window_s: float) -> list[tuple[int, float]]:
    """(tid, CPU share of one core) over `window_s`, busiest first."""
    before = {t: thread_cpu_100ns(t) for t in thread_ids(pid)}
    time.sleep(window_s)
    shares = []
    for t, b in before.items():
        a = thread_cpu_100ns(t)
        if a is not None and b is not None:
            shares.append((t, (a - b) / (window_s * 1e7)))
    return sorted(shares, key=lambda x: -x[1])


@dataclass
class _Thread:
    tid: int
    handle: int


class Sampler:
    """Suspend / read context / read stack / resume, for one process."""

    def __init__(self, pid: int, stack_words: int):
        self.pid = pid
        self.hproc = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.hproc:
            raise ctypes.WinError(ctypes.get_last_error())
        wow = wt.BOOL()
        k32.IsWow64Process(self.hproc, ctypes.byref(wow))
        self.wow64 = bool(wow.value)
        self.ptr = 4 if self.wow64 else 8
        self.stack_bytes = stack_words * self.ptr
        self.stack_buf = ctypes.create_string_buffer(self.stack_bytes)
        size = WOW64_CONTEXT_SIZE if self.wow64 else CONTEXT_AMD64_SIZE
        self.ctx_raw = ctypes.create_string_buffer(size + 16)
        self.ctx_addr = (ctypes.addressof(self.ctx_raw) + 15) & ~15   # CONTEXT must be 16-byte aligned
        self.modules = modules(self.hproc)

    def open_thread(self, tid: int) -> _Thread | None:
        h = k32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, tid)
        return _Thread(tid, h) if h else None

    def sample(self, th: _Thread) -> tuple[int, int, int, bytes] | None:
        """(ip, sp, frame pointer, raw stack bytes from sp) of a suspended snapshot of `th`; None if it is gone."""
        suspend = k32.Wow64SuspendThread if self.wow64 else k32.SuspendThread
        if suspend(th.handle) == 0xFFFFFFFF:
            return None
        try:
            if self.wow64:
                ctypes.c_uint32.from_address(self.ctx_addr).value = WOW64_CONTEXT_CONTROL_INTEGER
                if not k32.Wow64GetThreadContext(th.handle, self.ctx_addr):
                    return None
                ip = ctypes.c_uint32.from_address(self.ctx_addr + WOW64_CONTEXT_EIP_OFF).value
                sp = ctypes.c_uint32.from_address(self.ctx_addr + WOW64_CONTEXT_ESP_OFF).value
                bp = ctypes.c_uint32.from_address(self.ctx_addr + WOW64_CONTEXT_EBP_OFF).value
            else:
                ctypes.c_uint32.from_address(self.ctx_addr + CONTEXT_AMD64_FLAGS_OFF).value = \
                    CONTEXT_AMD64_CONTROL_INTEGER
                if not k32.GetThreadContext(th.handle, self.ctx_addr):
                    return None
                ip = ctypes.c_uint64.from_address(self.ctx_addr + CONTEXT_AMD64_RIP_OFF).value
                sp = ctypes.c_uint64.from_address(self.ctx_addr + CONTEXT_AMD64_RSP_OFF).value
                bp = ctypes.c_uint64.from_address(self.ctx_addr + CONTEXT_AMD64_RBP_OFF).value
            got = ctypes.c_size_t()
            k32.ReadProcessMemory(self.hproc, sp, self.stack_buf, self.stack_bytes, ctypes.byref(got))
            return ip, sp, bp, self.stack_buf.raw[:got.value]
        finally:
            k32.ResumeThread(th.handle)


def _module_ranges(mods: list[dict]) -> tuple[list[int], list[int]]:
    return [m["base"] for m in mods], [m["base"] + m["size"] for m in mods]


def record(target: str, out: Path, duration_s: float, hz: float, top: int, tids: list[int] | None,
           stack_words: int, prepass_s: float) -> dict:
    """Sample `target` for `duration_s` and write the recording to `out` (.jsonl.gz). Returns the header."""
    pid = find_pid(target)
    s = Sampler(pid, stack_words)
    if tids:
        chosen = [(t, None) for t in tids]
    else:
        chosen = [x for x in busiest_threads(pid, prepass_s)[:top]]
    threads = [th for th in (s.open_thread(t) for t, _ in chosen) if th]
    lo, hi = _module_ranges(s.modules)
    header = {
        "type": "header", "version": 1, "pid": pid, "target": target, "wow64": s.wow64,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"), "hz_per_thread": hz, "stack_words": stack_words,
        "modules": s.modules,
        "threads": [{"tid": t, "cpu": round(c, 3) if c is not None else None,
                     "start": thread_start_address(t)} for t, c in chosen],
    }
    period = 1.0 / (hz * max(1, len(threads)))
    n = 0
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        t0 = time.perf_counter()
        nxt = t0
        i = 0
        while threads and time.perf_counter() - t0 < duration_s:
            th = threads[i % len(threads)]
            i += 1
            got = s.sample(th)
            if got is None:
                continue
            ip, sp, bp, raw = got
            words = struct.unpack_from("<%d%s" % (len(raw) // s.ptr, "I" if s.wow64 else "Q"), raw)
            idx = [j for j, w in enumerate(words) if (k := bisect.bisect_right(lo, w) - 1) >= 0 and w < hi[k]]
            f.write(json.dumps({"t": round((time.perf_counter() - t0) * 1000, 2), "tid": th.tid, "ip": ip,
                                "sp": sp, "bp": bp, "st": [words[j] for j in idx], "si": idx},
                               separators=(",", ":")) + "\n")
            n += 1
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()
    for th in threads:
        k32.CloseHandle(th.handle)
    header["samples"] = n
    return header
