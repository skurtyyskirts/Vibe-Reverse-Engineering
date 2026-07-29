"""Game process health: is it running, responding, rendering, or crashed?

An unattended porting run spends most of its time waiting on a game it cannot
see. Without a health probe every failure mode looks identical — inputs go
nowhere and screenshots stop changing whether the game hung, crashed to a
Windows error dialog, dropped to a black screen, or is simply still loading.
Each needs a different response, so each needs to be distinguishable.

`check` collapses the probes into one verdict:

    session-locked the desktop is locked or on the secure desktop — input and
                  capture cannot work at all until it is unlocked
    not-running   no process by that name — launch or relaunch it
    crashed       a crash reporter or an error dialog is up — read it, dismiss it
    no-window     process alive but no top-level window yet — still starting
    hung          window exists but the message loop is not answering
    runtime-error the runtime logged a fatal condition (device lost, out of
                  memory) — relaunching reproduces it, so it is a finding
    not-rendering window answers but the frame is black/blank — capture path
                  or renderer is broken, navigation logic will not help
    frozen        frames answer and are identical across the freeze window
    ok            alive, responding, rendering

Every probe is Windows-only; the verdict logic is not, so it stays testable.

Usage (CLI):
    python -m livetools health --exe game.exe
    python -m livetools health --exe game.exe --game-dir "C:/Games/MyGame"
    python -m livetools health --exe game.exe --frozen-check 2.0
    python -m livetools health --exe game.exe --wait 60
    python -m livetools health --exe game.exe --dismiss-dialogs

Usage (library):
    from livetools import health
    state = health.check("game.exe", game_dir="C:/Games/MyGame")
    if state["verdict"] != "ok":
        ...
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Window classes Windows uses for standard dialog boxes (crash/assert popups).
DIALOG_CLASS = "#32770"

#: Title/text fragments that mark a window as an error popup rather than a
#: normal child window of the game.
ERROR_TITLE_MARKERS = ("error", "exception", "crash", "failed", "failure",
                       "assert", "fatal", "not responding", "has stopped")

#: Processes Windows starts when an application dies.
CRASH_REPORTER_EXES = ("WerFault.exe", "WerFaultSecure.exe", "dwwin.exe")

#: Log fragments that mean the runtime gave up, as opposed to routine warnings.
FATAL_LOG_MARKERS = ("device lost", "device removed", "failed to create",
                     "out of memory", "unrecoverable", "fatal",
                     "unsupported adapter", "d3derr", "vk_error")

#: Changed-pixel ratio below which two captures a moment apart are the same
#: frame. Exact equality never fires on a real renderer — dithering, temporal
#: accumulation and upscaler jitter move pixels every frame — so testing for
#: zero meant `frozen` could not be reported at all.
FROZEN_RATIO = 1e-4

SMTO_ABORTIFHUNG = 0x0002
WM_NULL = 0x0000
WM_CLOSE = 0x0010
DESKTOP_READOBJECTS = 0x0001


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("Process health probes require Windows")


def window_responding(hwnd: int, timeout_ms: int = 2000) -> bool:
    """Test whether a window is pumping messages.

    Sends WM_NULL with SMTO_ABORTIFHUNG: a window whose message loop is
    blocked (deadlock, stuck load, a modal it never shows) fails to answer
    while still existing and still looking alive to every other probe.

    Args:
        hwnd:       Target window.
        timeout_ms: How long to wait for the reply.

    Returns:
        True if the window answered within the timeout.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    if user32.IsHungAppWindow(hwnd):
        return False
    result = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG,
                                    wt.UINT(timeout_ms), ctypes.byref(result))
    return bool(ok)


def error_windows(pid: int) -> list[dict]:
    """List a process's visible windows that look like error popups.

    Args:
        pid: Process whose windows to inspect.

    Returns:
        List of {hwnd, title, class_name, is_dialog} for windows that are
        either a standard dialog class or carry an error-shaped title.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    found: list[dict] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        is_dialog = cls.value == DIALOG_CLASS
        looks_bad = any(m in title.value.lower() for m in ERROR_TITLE_MARKERS)
        if is_dialog or looks_bad:
            found.append({"hwnd": hwnd, "title": title.value,
                          "class_name": cls.value, "is_dialog": is_dialog})
        return True

    user32.EnumWindows(_cb, 0)
    return found


def desktop_available() -> bool:
    """Whether an interactive desktop is present to receive input.

    A locked workstation or an active secure desktop (UAC) silently swallows
    every keystroke and captures black — which looks exactly like a broken
    port, and is the one failure no amount of relaunching fixes.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    import ctypes

    user32 = ctypes.windll.user32
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return False
    user32.CloseDesktop(handle)
    return True


def crash_reporters() -> list[dict]:
    """List running Windows crash-reporter processes.

    A WerFault window is the clearest signal a game died rather than hung —
    it outlives the game process, so it is often the only evidence left. It is
    not proof on its own: an unrelated application crashing elsewhere on the
    machine starts one too, which is why `verdict_for` only trusts it once the
    game itself looks absent or unresponsive.

    Returns:
        List of {pid, exe}.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    from . import gamectl as gc

    return [{"pid": pid, "exe": name}
            for name in CRASH_REPORTER_EXES
            for pid in [gc.find_pid(name)] if pid]


def dismiss(hwnd: int) -> bool:
    """Ask a window to close (WM_CLOSE).

    Used to clear crash/error dialogs that would otherwise block every later
    input for the rest of an unattended run.

    Returns:
        True if the window was gone afterwards.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    import ctypes

    user32 = ctypes.windll.user32
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(0.3)
    return not bool(user32.IsWindow(hwnd))


def wait_for_window(exe: str, timeout: float = 60.0,
                    poll: float = 1.0) -> int | None:
    """Block until the game has a top-level window, or the timeout expires.

    Args:
        exe:     Executable name to look for.
        timeout: Seconds to wait.
        poll:    Seconds between checks.

    Returns:
        The window handle, or None if it never appeared.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    from . import gamectl as gc

    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = gc.find_hwnd_by_exe(exe)
        if hwnd:
            return hwnd
        time.sleep(poll)
    return None


def fatal_log_lines(game_dir: str | Path, tail: int = 200) -> list[str]:
    """Pull lines from the Remix/dxvk logs that indicate a fatal condition.

    Args:
        game_dir: Directory holding the runtime logs.
        tail:     Lines to scan from the end of each log.

    Returns:
        Deduplicated "log_name: line" strings matching FATAL_LOG_MARKERS.
    """
    from . import remixctl

    hits: list[str] = []
    for name, lines in remixctl.read_logs(game_dir, tail=tail).items():
        for line in lines:
            low = line.lower()
            if any(marker in low for marker in FATAL_LOG_MARKERS):
                entry = f"{name}: {line.strip()}"
                if entry not in hits:
                    hits.append(entry)
    return hits


def verdict_for(probes: dict) -> tuple[str, str]:
    """Reduce raw probe results to a verdict and the reason for it.

    Ordered most-fatal first: a crashed process is still "crashed" even though
    it also has no window, and a black frame is worth reporting even though
    the window is responding.

    A crash reporter is only evidence once the game itself looks gone or
    unresponsive: WerFault is machine-wide, so an unrelated application dying
    elsewhere would otherwise make a perfectly healthy game verdict `crashed`
    and get it killed and relaunched.

    Args:
        probes: dict with keys `pid`, `hwnd`, `responding`, `error_windows`,
            `crash_reporters`, `desktop`, `fatal_log_lines`, `frame` (a
            `classify_frame` result or None) and `frozen` (bool or None).

    Returns:
        (verdict, reason)
    """
    if probes.get("desktop") is False:
        return ("session-locked",
                "no interactive desktop — input and capture cannot work until "
                "the session is unlocked")
    if probes.get("error_windows"):
        titles = "; ".join(w["title"] or w["class_name"]
                           for w in probes["error_windows"])
        return "crashed", f"error dialog up: {titles}"

    reporters = probes.get("crash_reporters")
    if not probes.get("pid"):
        if reporters:
            exes = ", ".join(c["exe"] for c in reporters)
            return "crashed", f"process gone, crash reporter running ({exes})"
        return "not-running", "no process with that executable name"
    if not probes.get("hwnd"):
        return "no-window", f"pid {probes['pid']} alive but no top-level window"
    if probes.get("responding") is False:
        if reporters:
            exes = ", ".join(c["exe"] for c in reporters)
            return "crashed", f"window unresponsive, crash reporter up ({exes})"
        return "hung", "window exists but did not answer WM_NULL"

    fatal = probes.get("fatal_log_lines")
    if fatal:
        # The runtime already said what went wrong. Relaunching reproduces it,
        # so this is a finding about the port, not something to recover from.
        return "runtime-error", fatal[0]

    frame = probes.get("frame")
    if frame and not frame.get("usable", True):
        return "not-rendering", f"frame is {frame['verdict']}: {frame['reason']}"
    if probes.get("frozen"):
        return "frozen", "frames identical across the freeze-check window"
    return "ok", "process alive, window responding, frame has content"


def check(exe: str, game_dir: str | Path | None = None,
          frozen_check: float = 0.0, dismiss_dialogs: bool = False,
          frozen_ratio: float = FROZEN_RATIO) -> dict:
    """Probe a game's health and reduce it to one actionable verdict.

    Args:
        exe:             Executable name, e.g. `game.exe`.
        game_dir:        Directory holding rtx.conf/logs; enables log scanning.
        frozen_check:    If > 0, capture two frames this many seconds apart and
                         report whether they are the same frame.
        dismiss_dialogs: Close any error dialogs found, and re-probe after.
        frozen_ratio:    Changed-pixel ratio under which those two captures
                         count as the same frame.

    Returns:
        dict with `verdict`, `reason`, and every raw probe result.

    Raises:
        OSError: If not on Windows.
    """
    _require_windows()
    from . import gamectl as gc
    from . import screenshot as ss

    pid = gc.find_pid(exe)
    hwnd = gc.find_hwnd_by_exe(exe) if pid else None
    probes: dict = {
        "exe": exe, "pid": pid, "hwnd": hwnd, "desktop": desktop_available(),
        "responding": window_responding(hwnd) if hwnd else None,
        "error_windows": error_windows(pid) if pid else [],
        "crash_reporters": crash_reporters(),
        "frame": None, "frozen": None,
        "fatal_log_lines": fatal_log_lines(game_dir) if game_dir else [],
    }

    if dismiss_dialogs and probes["error_windows"]:
        dismissed = [w for w in probes["error_windows"] if dismiss(w["hwnd"])]
        probes["dismissed"] = dismissed
        probes["error_windows"] = error_windows(pid) if pid else []

    if hwnd and probes["responding"]:
        try:
            width, height, rgb = ss.capture_window(hwnd)
            probes["frame"] = ss.classify_frame(ss.frame_stats(width, height, rgb))
            if frozen_check > 0:
                time.sleep(frozen_check)
                w2, h2, rgb2 = ss.capture_window(hwnd)
                if (w2, h2) == (width, height):
                    delta = ss.diff_rgb(width, height, rgb, rgb2)
                    probes["freeze_ratio"] = delta["ratio"]
                    probes["frozen"] = delta["ratio"] < frozen_ratio
        except OSError as e:
            probes["capture_error"] = str(e)

    probes["verdict"], probes["reason"] = verdict_for(probes)
    return probes
