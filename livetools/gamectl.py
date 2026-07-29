"""Game window input automation via SendInput with proper focus management.

Re-Volt and most older DX9/DirectInput games read raw device state — they
ignore WM_KEYDOWN/PostMessage entirely. SendInput is required, but the game
window must be in the foreground first.

Focus strategy: attach our thread to the game's input queue via
AttachThreadInput, then SetForegroundWindow. This bypasses the Windows
foreground-lock that normally blocks background processes from stealing focus.

Window lookup supports two modes:
  --exe    <exe_name>     find window by process exe name (recommended)
  --window <title_hint>   find window by title substring fallback

Usage (CLI):
    python -m livetools gamectl --exe revolt_xbox.exe info
    python -m livetools gamectl --exe revolt_xbox.exe key RETURN
    python -m livetools gamectl --exe revolt_xbox.exe keys "DOWN DOWN RETURN"
    python -m livetools gamectl --exe revolt_xbox.exe keys "RETURN WAIT:1000 RETURN" --delay-ms 0
    python -m livetools gamectl --exe revolt_xbox.exe click 400 300
    python -m livetools gamectl --exe revolt_xbox.exe macro --macro-file patches/revolt/macros.json navigate_menu
    python -m livetools gamectl --exe revolt_xbox.exe macros --macro-file patches/revolt/macros.json

Usage (library):
    from livetools.gamectl import find_hwnd_by_exe, focus_hwnd, send_key, send_keys
    hwnd = find_hwnd_by_exe("revolt_xbox.exe")
    focus_hwnd(hwnd)
    send_key("RETURN")
    send_keys("DOWN DOWN RETURN", delay_ms=200)

Macro file format (JSON) — store at patches/<GameName>/macros.json:
    {
      "navigate_menu": {
        "description": "Navigate from title screen into a race",
        "steps": "RETURN WAIT:1000 DOWN DOWN RETURN WAIT:500 RETURN"
      }
    }
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from pathlib import Path

# ── Win32 constants ────────────────────────────────────────────────────────

INPUT_KEYBOARD       = 1
INPUT_MOUSE          = 0
KEYEVENTF_KEYUP      = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004
MOUSEEVENTF_MOVE     = 0x0001
SW_RESTORE           = 9
SW_SHOW              = 5
GW_OWNER             = 4
TH32CS_SNAPPROCESS   = 0x00000002

class _NoWin32:
    """Stands in for a Win32 DLL off Windows.

    Input and window control are Windows-only, but the macro file format, the
    key map and the token syntax are not — keeping the module importable
    everywhere means those can be tested on any platform instead of only in a
    Windows checkout.
    """

    def __getattr__(self, name: str):
        raise OSError(f"{name} requires Windows; this is {sys.platform}")


_win32 = sys.platform == "win32"
user32   = ctypes.windll.user32   if _win32 else _NoWin32()
kernel32 = ctypes.windll.kernel32 if _win32 else _NoWin32()

# ── Virtual key map ────────────────────────────────────────────────────────

VK_MAP: dict[str, int] = {
    "RETURN": 0x0D, "ENTER": 0x0D,
    "ESCAPE": 0x1B, "ESC": 0x1B,
    "SPACE": 0x20,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "TAB": 0x09, "BACKSPACE": 0x08, "DELETE": 0x2E,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "F1": 0x70, "F2": 0x71, "F3": 0x72,  "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76,  "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    # SHFT/CTL match how rtx.conf spells its hotkeys (rtx.captureHotKey =
    # CTRL, SHFT, Q), so a chord can be copied straight out of the config.
    "SHIFT": 0x10, "SHFT": 0x10, "CTRL": 0x11, "CTL": 0x11, "ALT": 0x12,
    **{c: 0x41 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    **{str(d): 0x30 + d for d in range(10)},
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69,
}

# ── SendInput structures ───────────────────────────────────────────────────

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         wt.WORD),
        ("wScan",       wt.WORD),
        ("dwFlags",     wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          wt.LONG),
        ("dy",          wt.LONG),
        ("mouseData",   wt.DWORD),
        ("dwFlags",     wt.DWORD),
        ("time",        wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUT_UNION)]

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      ctypes.c_long),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_char * 260),
    ]


def find_pids(exe_name: str) -> list[int]:
    """Return every PID whose exe matches exe_name, in enumeration order.

    Relaunching a game that did not fully exit leaves two instances running;
    only one of them owns the window, so callers that assume a single match
    end up driving the wrong process.
    """
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    pids: list[int] = []
    try:
        if not kernel32.Process32First(snap, ctypes.byref(entry)):
            return []
        while True:
            name = entry.szExeFile.decode("utf-8", errors="replace")
            if name.lower() == exe_name.lower():
                pids.append(entry.th32ProcessID)
            if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snap)
    return pids


def find_pid(exe_name: str) -> int | None:
    """Return the PID of the first process whose exe matches exe_name."""
    pids = find_pids(exe_name)
    return pids[0] if pids else None

# ── Window lookup ──────────────────────────────────────────────────────────

# WINFUNCTYPE only exists on Windows; the callback is never invoked elsewhere,
# so the calling convention is irrelevant to keeping the module importable.
WNDENUMPROC = (ctypes.WINFUNCTYPE if _win32 else ctypes.CFUNCTYPE)(
    wt.BOOL, wt.HWND, wt.LPARAM)


def find_hwnd_by_exe(exe_name: str) -> int | None:
    """Find the main visible window of a process by its exe filename.

    Args:
        exe_name: Process exe name, e.g. "revolt_xbox.exe"

    Returns:
        Window handle (int) or None if not found.
    """
    pid = find_pid(exe_name)
    if pid is None:
        return None
    result: list[int] = []

    @WNDENUMPROC
    def _cb(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER) != 0:
            return True
        proc_id = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid:
            result.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return result[0] if result else None


def find_hwnd_by_title(title_hint: str) -> int | None:
    """Find the first visible top-level window whose title contains title_hint."""
    result: list[int] = []

    @WNDENUMPROC
    def _cb(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER) != 0:
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value and title_hint.lower() in buf.value.lower():
            result.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return result[0] if result else None


def resolve_hwnd(exe: str | None, window: str | None) -> tuple[int | None, str]:
    """Resolve hwnd from --exe or --window, return (hwnd, error_msg)."""
    if exe:
        hwnd = find_hwnd_by_exe(exe)
        if not hwnd:
            return None, f"No window found for process '{exe}'"
        return hwnd, ""
    if window:
        hwnd = find_hwnd_by_title(window)
        if not hwnd:
            return None, f"No window found matching title '{window}'"
        return hwnd, ""
    return None, "Provide --exe <game.exe> or --window <title>"


def get_window_info(hwnd: int) -> dict:
    """Return title and process info for a hwnd."""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    pid = wt.DWORD(0)
    tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"hwnd": hwnd, "title": buf.value, "pid": pid.value, "tid": tid}

# ── Focus management ───────────────────────────────────────────────────────

def set_dpi_aware() -> bool:
    """Opt this process into per-monitor DPI awareness.

    Without it Windows reports virtualized coordinates on a scaled display:
    click targets read off a screenshot land in the wrong place, and captures
    come back a different size than the window. Idempotent — Windows refuses
    the second call, which is fine.

    Returns:
        True if awareness is set (whether by this call or an earlier one).
    """
    if not _win32:
        return False
    import ctypes as _c
    try:
        # -4 = PER_MONITOR_AWARE_V2, the only mode that also fixes non-client
        # scaling; older Windows falls back to the process-wide flag.
        if _c.windll.user32.SetProcessDpiAwarenessContext(-4):
            return True
    except AttributeError:
        pass
    try:
        return bool(_c.windll.shcore.SetProcessDpiAwareness(2) == 0)
    except (AttributeError, OSError):
        return bool(user32.SetProcessDPIAware())


def focus_hwnd(hwnd: int) -> bool:
    """Force hwnd to the foreground using AttachThreadInput.

    DirectInput games only process keys when their window is the foreground
    window. This attaches our thread to the game's input queue so
    SetForegroundWindow is not blocked by the Windows foreground lock.

    Returns True if the window is in the foreground after the call.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.3)

    my_tid  = kernel32.GetCurrentThreadId()
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid  = user32.GetWindowThreadProcessId(fg_hwnd, None)

    if fg_tid and fg_tid != my_tid:
        attached = user32.AttachThreadInput(my_tid, fg_tid, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            # Staying attached to another process's input queue outlives this
            # call and makes later input land in the wrong place.
            if attached:
                user32.AttachThreadInput(my_tid, fg_tid, False)
    else:
        user32.SetForegroundWindow(hwnd)

    time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd

# ── SendInput keyboard ─────────────────────────────────────────────────────

#: Keys on the extended part of the keyboard. Without KEYEVENTF_EXTENDEDKEY
#: their scancodes collide with the numpad, and games reading scancodes
#: (DirectInput, raw input — i.e. most of the games this toolkit targets)
#: either ignore the press or act on the wrong key. Menu navigation is arrows,
#: so getting this wrong breaks the whole navigation loop.
EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24,          # PAGEUP PAGEDOWN END HOME
    0x25, 0x26, 0x27, 0x28,          # LEFT UP RIGHT DOWN
    0x2D, 0x2E,                      # INSERT DELETE
    0x6F,                            # numpad divide
    0x90,                            # NUMLOCK
})


def _make_key_input(vk: int, up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk   = vk
    inp.union.ki.wScan = user32.MapVirtualKeyW(vk, 0)
    inp.union.ki.dwFlags = flags
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = None
    return inp


def _inject(*inputs: INPUT) -> int:
    """Send input events, returning how many the system actually accepted.

    SendInput silently injects nothing when a higher-integrity process owns
    the foreground (an elevated game, UIPI). Reporting zero here is what stops
    the loop from recording "sent RETURN" for an input that never arrived.
    """
    array = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def send_key(key_name: str, hold_ms: int = 50) -> dict:
    """Send a single key press via SendInput (game must be foreground).

    Args:
        key_name: Key name from VK_MAP (e.g. "RETURN", "UP", "A", "F5").
        hold_ms:  Delay between keydown and keyup in milliseconds.

    Returns:
        dict with ok, key, vk
    """
    vk = VK_MAP.get(key_name.upper())
    if vk is None:
        return {"ok": False, "error": f"Unknown key: '{key_name}'. "
                f"Valid: {', '.join(sorted(VK_MAP))}"}
    injected = _inject(_make_key_input(vk, up=False))
    time.sleep(hold_ms / 1000.0)
    injected += _inject(_make_key_input(vk, up=True))
    if injected < 2:
        return {"ok": False, "key": key_name, "vk": hex(vk),
                "injected": injected,
                "error": "SendInput was blocked — the foreground window "
                         "likely belongs to a higher-integrity process; run "
                         "this toolkit elevated"}
    return {"ok": True, "key": key_name, "vk": hex(vk), "injected": injected}


def send_chord(combo: str, hold_ms: int = 80) -> dict:
    """Send a modifier chord like "ALT+X" via SendInput (game must be foreground).

    All keys are pressed down in order, held together, then released in
    reverse order — required for hotkeys like the RTX Remix menu (ALT+X),
    which ignore sequential presses.

    Args:
        combo:   Plus-separated key names from VK_MAP (e.g. "ALT+X",
                 "CTRL+SHIFT+Q").
        hold_ms: How long the full chord is held before release.

    Returns:
        dict with ok, combo, vks
    """
    names = [n.strip().upper() for n in combo.split("+") if n.strip()]
    vks = []
    for name in names:
        vk = VK_MAP.get(name)
        if vk is None:
            return {"ok": False, "error": f"Unknown key in chord: '{name}'. "
                    f"Valid: {', '.join(sorted(VK_MAP))}"}
        vks.append(vk)
    if not vks:
        return {"ok": False, "error": "Empty chord"}

    injected = 0
    for vk in vks:
        injected += _inject(_make_key_input(vk, up=False))
        time.sleep(0.02)
    time.sleep(hold_ms / 1000.0)
    for vk in reversed(vks):
        injected += _inject(_make_key_input(vk, up=True))
        time.sleep(0.02)
    result = {"ok": injected == 2 * len(vks), "combo": combo,
              "vks": [hex(v) for v in vks], "injected": injected}
    if not result["ok"]:
        result["error"] = ("SendInput was blocked — the foreground window "
                           "likely belongs to a higher-integrity process")
    return result


def send_keys(hwnd: int, sequence: str, delay_ms: int = 200) -> dict:
    """Focus hwnd then send a space-separated key sequence via SendInput.

    Token syntax:
        KEY_NAME          — keydown + keyup
        WAIT:N            — pause N milliseconds
        HOLD:KEY_NAME:N   — hold key N ms before keyup
        CHORD:A+B         — press keys together, release in reverse (e.g. CHORD:ALT+X)

    Args:
        hwnd:      Target window handle (will be focused before sending).
        sequence:  Space-separated token string.
        delay_ms:  Default inter-key delay in milliseconds.

    Returns:
        dict with ok, count, actions
    """
    focused = focus_hwnd(hwnd)
    if not focused:
        # Still try — some games accept input even if focus check is unreliable
        pass

    actions: list[dict] = []
    for token in sequence.strip().split():
        upper = token.upper()
        if upper.startswith("WAIT:"):
            ms = int(token.split(":")[1])
            time.sleep(ms / 1000.0)
            actions.append({"action": "wait", "ms": ms})
        elif upper.startswith("HOLD:"):
            parts = token.split(":")
            key = parts[1]
            ms  = int(parts[2]) if len(parts) > 2 else 500
            r   = send_key(key, hold_ms=ms)
            actions.append({**r, "action": "hold", "hold_ms": ms})
            time.sleep(delay_ms / 1000.0)
        elif upper.startswith("CHORD:"):
            r = send_chord(token.split(":", 1)[1])
            actions.append({**r, "action": "chord"})
            time.sleep(delay_ms / 1000.0)
        else:
            r = send_key(token)
            actions.append(r)
            time.sleep(delay_ms / 1000.0)
    return {"ok": True, "focused": focused, "count": len(actions), "actions": actions}

# ── Mouse input ────────────────────────────────────────────────────────────

def click_at(hwnd: int, x: int, y: int) -> dict:
    """Focus hwnd then left-click at client-area coordinates via SendInput.

    Args:
        hwnd: Target window handle.
        x, y: Client-area coordinates.

    Returns:
        dict with ok, screen_x, screen_y
    """
    set_dpi_aware()
    focus_hwnd(hwnd)
    pt = wt.POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    user32.SetCursorPos(pt.x, pt.y)
    time.sleep(0.05)

    dn = INPUT(); dn.type = INPUT_MOUSE; dn.union.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    up = INPUT(); up.type = INPUT_MOUSE; up.union.mi.dwFlags = MOUSEEVENTF_LEFTUP
    injected = _inject(dn, up)
    return {"ok": injected == 2, "screen_x": pt.x, "screen_y": pt.y,
            "client_x": x, "client_y": y, "injected": injected}


def move_mouse(dx: int, dy: int, steps: int = 1, step_ms: int = 10) -> dict:
    """Send relative mouse motion — camera look, not cursor positioning.

    Games read mouse-look through relative deltas (DirectInput / raw input),
    not cursor position, so `SetCursorPos` turns nothing. Reaching a specific
    in-level viewpoint means turning the camera, which means this.

    Large turns are sent as several smaller deltas: games commonly clamp or
    drop a single huge delta as a glitch.

    Args:
        dx, dy:  Total relative motion in mouse units (right/down positive).
        steps:   Split the motion across this many events.
        step_ms: Delay between steps.

    Returns:
        dict with ok, dx, dy, steps and injected.
    """
    if steps < 1:
        return {"ok": False, "error": "steps must be >= 1"}
    injected = 0
    for i in range(steps):
        # Distribute by difference so the deltas sum exactly to (dx, dy).
        part_x = dx * (i + 1) // steps - dx * i // steps
        part_y = dy * (i + 1) // steps - dy * i // steps
        move = INPUT()
        move.type = INPUT_MOUSE
        move.union.mi.dx = part_x
        move.union.mi.dy = part_y
        move.union.mi.dwFlags = MOUSEEVENTF_MOVE
        injected += _inject(move)
        if step_ms:
            time.sleep(step_ms / 1000.0)
    return {"ok": injected == steps, "dx": dx, "dy": dy, "steps": steps,
            "injected": injected}

# ── Macro support ──────────────────────────────────────────────────────────

def load_macros(path: str | Path) -> dict[str, dict]:
    """Load a macro JSON file.

    Args:
        path: Path to JSON file mapping name -> {description, steps}.

    Returns:
        dict of macro definitions.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Macro file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Macro file must be a JSON object")
    return data


def save_macro(path: str | Path, name: str, steps: str,
               description: str | None = None) -> dict:
    """Record a working input sequence into a macro file.

    Menu paths are discovered one verified input at a time, which is slow.
    Saving the path the moment it works means a later restart replays it
    instead of rediscovering it — and restarts are constant, because every
    rtx.conf change needs one.

    Args:
        path:        Macro JSON file; created (with parents) if missing.
        name:        Macro key, e.g. `title_to_gameplay`.
        steps:       Space-separated token sequence in `send_keys` syntax.
        description: What this path does and where it ends up. None keeps the
            existing description when re-saving a macro with better timing.

    Returns:
        dict with `ok`, `macro`, `path` and `replaced`.

    Raises:
        ValueError: If steps is empty, or the file holds something other than
            a JSON object.
    """
    if not steps.strip():
        raise ValueError("Refusing to save a macro with no steps")
    p = Path(path)
    macros = load_macros(p) if p.exists() else {}
    replaced = name in macros
    if description is None:
        description = macros.get(name, {}).get("description", "")
    macros[name] = {"description": description, "steps": steps.strip()}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(macros, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "macro": name, "path": str(p), "replaced": replaced}


def run_macro(hwnd: int, name: str, macros: dict[str, dict],
              delay_ms: int = 200) -> dict:
    """Focus hwnd and execute a named macro.

    Args:
        hwnd:      Target window handle.
        name:      Macro name key.
        macros:    Loaded macro definitions.
        delay_ms:  Inter-key delay in milliseconds.

    Returns:
        dict with ok, macro, steps_result
    """
    if name not in macros:
        return {"ok": False,
                "error": f"Macro '{name}' not found. "
                         f"Available: {', '.join(sorted(macros))}"}
    steps = macros[name].get("steps", "")
    if not steps:
        return {"ok": False, "error": f"Macro '{name}' has no steps"}
    result = send_keys(hwnd, steps, delay_ms=delay_ms)
    return {"ok": result["ok"], "macro": name, "steps_result": result}
