"""Game process lifecycle: launch, stop, restart, and keep the session awake.

rtx.conf is read once at launch, so every Remix setting the loop changes costs
a restart — the single most repeated action in an unattended port. Doing that
reliably needs more than starting the exe: the previous instance has to be
gone first (a game that did not fully exit leaves a second process that owns
no window and swallows the port), and the new one has to be up before any
input is worth sending.

`restart` is the whole cycle with those checks in place. `keep_awake` covers
the other way an overnight run dies quietly: Windows sleeping the machine or
blanking the display, which stops input delivery and turns every capture
black.

Usage (CLI):
    python -m livetools proc status --exe game.exe
    python -m livetools proc stop --exe game.exe
    python -m livetools proc start "C:/Games/MyGame/game.exe"
    python -m livetools proc restart "C:/Games/MyGame/game.exe" --wait 90
    python -m livetools proc keep-awake --duration 43200

Usage (library):
    from livetools import procctl
    procctl.restart("C:/Games/MyGame/game.exe", wait=90)
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x0
WM_CLOSE = 0x0010

# SetThreadExecutionState flags.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("Process control requires Windows")


def status(exe: str) -> dict:
    """Report every instance of a game and which one owns a window.

    Returns:
        dict with `exe`, `pids`, `count`, `hwnd` and `window_pid`.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    from . import gamectl as gc

    pids = gc.find_pids(exe)
    hwnd = gc.find_hwnd_by_exe(exe) if pids else None
    info = gc.get_window_info(hwnd) if hwnd else {}
    return {"exe": exe, "pids": pids, "count": len(pids), "hwnd": hwnd,
            "window_pid": info.get("pid"), "title": info.get("title")}


def stop(exe: str, timeout: float = 15.0, force: bool = True) -> dict:
    """Close every instance of a game, escalating to a kill if needed.

    Asks each window to close first so the game saves its own config, then
    terminates whatever is still alive when the timeout expires. Leaving a
    stale instance behind is worse than a hard kill: the next launch produces
    two processes and the port lookup starts picking the wrong one.

    Args:
        exe:     Executable name, e.g. `game.exe`.
        timeout: Seconds to wait for a graceful exit before terminating.
        force:   Terminate survivors. With force=False, report them instead.

    Returns:
        dict with `closed` (pids that exited on their own), `terminated`,
        `survivors` and `ok`.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    import ctypes

    from . import gamectl as gc

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    initial = gc.find_pids(exe)
    if not initial:
        return {"ok": True, "closed": [], "terminated": [], "survivors": [],
                "note": "no instances running"}

    hwnd = gc.find_hwnd_by_exe(exe)
    while hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        next_hwnd = gc.find_hwnd_by_exe(exe)
        if next_hwnd == hwnd:
            break
        hwnd = next_hwnd

    deadline = time.time() + timeout
    while time.time() < deadline and gc.find_pids(exe):
        time.sleep(0.5)

    survivors = gc.find_pids(exe)
    closed = [p for p in initial if p not in survivors]
    terminated: list[int] = []
    if survivors and force:
        for pid in survivors:
            handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE,
                                          False, pid)
            if handle:
                if kernel32.TerminateProcess(handle, 1):
                    terminated.append(pid)
                kernel32.WaitForSingleObject(handle, 5000)
                kernel32.CloseHandle(handle)
        time.sleep(0.5)
        survivors = gc.find_pids(exe)

    return {"ok": not survivors, "closed": closed, "terminated": terminated,
            "survivors": survivors}


def start(exe_path: str | Path, args: list[str] | None = None,
          wait: float = 60.0) -> dict:
    """Launch a game and wait for its window.

    Args:
        exe_path: Full path to the executable.
        args:     Extra command-line arguments.
        wait:     Seconds to wait for a top-level window (0 to skip waiting).

    Returns:
        dict with `ok`, `pid`, `hwnd` and `exe`.

    Raises:
        OSError: If not on Windows.
        FileNotFoundError: If the executable does not exist.
    """
    _require_windows()
    from . import health

    path = Path(exe_path)
    if not path.is_file():
        raise FileNotFoundError(f"No executable at {path}")
    # Games resolve their data paths relative to the working directory.
    proc = subprocess.Popen([str(path), *(args or [])], cwd=str(path.parent))
    hwnd = health.wait_for_window(path.name, timeout=wait) if wait else None
    return {"ok": bool(hwnd) or not wait, "pid": proc.pid, "hwnd": hwnd,
            "exe": path.name}


def restart(exe_path: str | Path, args: list[str] | None = None,
            wait: float = 90.0, stop_timeout: float = 15.0) -> dict:
    """Stop a game and launch it again, verifying both halves.

    This is what applying any rtx.conf change costs, so it is worth doing
    exactly once per change rather than hoping the game picked it up.

    Args:
        exe_path:     Full path to the executable.
        args:         Extra command-line arguments.
        wait:         Seconds to wait for the new window.
        stop_timeout: Seconds to wait for a graceful exit.

    Returns:
        dict with `ok`, `stopped` (the `stop` result) and `started` (the
        `start` result).

    Raises:
        OSError: If not on Windows.
        FileNotFoundError: If the executable does not exist.
    """
    path = Path(exe_path)
    stopped = stop(path.name, timeout=stop_timeout)
    if not stopped["ok"]:
        return {"ok": False, "stopped": stopped, "started": None,
                "error": f"could not stop pids {stopped['survivors']}"}
    started = start(path, args=args, wait=wait)
    return {"ok": started["ok"], "stopped": stopped, "started": started}


def keep_awake(duration: float) -> dict:
    """Block sleep and display blanking for a while, then release.

    Windows power management does not care that a port is halfway through:
    a slept machine stops delivering input and captures black. The request
    only holds while this call is running, so an unattended run starts it as
    a background process for the length of the session.

    Args:
        duration: Seconds to hold the request.

    Returns:
        dict with `ok` and `duration`.

    Raises:
        OSError: If not on Windows, or if the request was refused.
    """
    _require_windows()
    import ctypes

    kernel32 = ctypes.windll.kernel32
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    if not kernel32.SetThreadExecutionState(flags):
        raise OSError("SetThreadExecutionState refused the wake request")
    try:
        time.sleep(duration)
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    return {"ok": True, "duration": duration}
