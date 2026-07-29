"""RTX Remix runtime control: rtx.conf editing, presets, menu toggling, log parsing.

The RTX Remix runtime (dxvk-remix) reads `rtx.conf` from the game directory at
startup and exposes a developer menu in-game (default hotkey ALT+X, option
`rtx.remixMenuKeyBinds`). This module gives an agent programmatic control over
both: durable settings go through rtx.conf (survive restarts, no UI navigation
needed), and the menu hotkey is available for the settings that must be
toggled live.

Option names below are verified against dxvk-remix's generated RtxOptions.md
(github.com/NVIDIAGameWorks/dxvk-remix). The full catalog with semantics lives
in `.claude/references/remix-compat-catalog.md`.

Usage (CLI):
    python -m livetools remix status --game-dir "C:/Games/MyGame"
    python -m livetools remix conf get [KEY] --game-dir DIR
    python -m livetools remix conf set rtx.debugView.debugViewIdx 277 --game-dir DIR
    python -m livetools remix conf unset rtx.debugView.debugViewIdx --game-dir DIR
    python -m livetools remix conf add-hash rtx.uiTextures 0x1234ABCD --game-dir DIR
    python -m livetools remix conf remove-hash rtx.uiTextures 0x1234ABCD --game-dir DIR
    python -m livetools remix preset list
    python -m livetools remix preset apply debug-geometry-hash --game-dir DIR
    python -m livetools remix menu --exe game.exe
    python -m livetools remix log --game-dir DIR --errors --tail 40

Usage (library):
    from livetools import remixctl
    remixctl.set_option(conf_path, "rtx.fallbackLightMode", "2")
    remixctl.apply_preset(conf_path, "debug-geometry-hash")
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

RTX_CONF = "rtx.conf"

# Remix developer menu hotkey (rtx.remixMenuKeyBinds default: ALT,X)
MENU_CHORD = "ALT+X"

# Debug view indices from dxvk-remix debug_view_indices.h
DEBUG_VIEWS = {
    "disabled": 0,
    "primitive-index": 1,
    "position": 11,
    "texcoords": 12,
    "geometry-normal": 15,
    "shading-normal": 16,
    "virtual-shading-normal": 17,
    "vertex-color": 18,
    "albedo": 23,
    "raw-albedo": 32,
    "white-noise": 41,
    "is-emissive": 180,
    "is-particle": 181,
    "primitive-index-hash": 276,
    "geometry-hash": 277,
}

# Hash-set options (comma-separated 0x hashes). Used by add-hash/remove-hash
# validation and by the compat playbook.
HASH_SET_OPTIONS = frozenset({
    "rtx.animatedWaterTextures", "rtx.antiCulling.antiCullingTextures",
    "rtx.beamTextures", "rtx.decalTextures", "rtx.dynamicDecalTextures",
    "rtx.hairCardTextures", "rtx.hideInstanceTextures",
    "rtx.ignoreAlphaOnTextures", "rtx.ignoreBakedLightingTextures",
    "rtx.ignoreLights", "rtx.ignoreTextures",
    "rtx.ignoreTransparencyLayerTextures", "rtx.lightConverter",
    "rtx.lightmapTextures", "rtx.nonOffsetDecalTextures",
    "rtx.opacityMicromapIgnoreTextures", "rtx.particleEmitterTextures",
    "rtx.particleTextures", "rtx.playerModelBodyTextures",
    "rtx.playerModelTextures", "rtx.postfx.motionBlurMaskOutTextures",
    "rtx.raytracedRenderTargetTextures", "rtx.singleOffsetDecalTextures",
    "rtx.skyBoxGeometries", "rtx.skyBoxTextures", "rtx.smoothNormalsTextures",
    "rtx.terrainTextures", "rtx.uiTextures",
    "rtx.worldSpaceUiBackgroundTextures", "rtx.worldSpaceUiTextures",
})

# Presets: named option bundles for common compatibility/debugging setups.
# Each maps option -> value; apply_preset writes them all.
PRESETS: dict[str, dict] = {
    "devmenu": {
        "description": "Developer-friendly menu: ALT+X opens Advanced UI with cursor",
        "options": {
            "rtx.defaultToAdvancedUI": "True",
            "rtx.showUICursor": "True",
        },
    },
    "debug-geometry-hash": {
        "description": "Color geometry by hash (view 277). Flicker across "
                       "identical frames = unstable hashes.",
        "options": {
            "rtx.debugView.debugViewIdx": "277",
        },
    },
    "debug-off": {
        "description": "Disable the debug view (back to normal rendering)",
        "options": {
            "rtx.debugView.debugViewIdx": "0",
        },
    },
    "hash-stable-anim": {
        "description": "Hash rule for games with CPU-animated/skinned vertex "
                       "data: drop per-frame-varying positions/texcoords from "
                       "generation hashing. Distinct meshes sharing one "
                       "layout+shader may collide — verify with geometry-hash "
                       "debug view.",
        "options": {
            "rtx.geometryGenerationHashRuleString":
                "indices,geometrydescriptor,vertexlayout,vertexshader",
        },
    },
    "hash-default": {
        "description": "Restore dxvk-remix default hash rules",
        "options": {
            "rtx.geometryGenerationHashRuleString":
                "positions,indices,texcoords,geometrydescriptor,vertexlayout,vertexshader",
            "rtx.geometryAssetHashRuleString":
                "positions,indices,geometrydescriptor",
        },
    },
    "fallback-light-always": {
        "description": "Always create the camera-following fallback light "
                       "(scene visible even with no converted game lights)",
        "options": {
            "rtx.fallbackLightMode": "2",
            "rtx.fallbackLightType": "0",
            "rtx.fallbackLightRadiance": "1.6, 1.8, 2.0",
        },
    },
    "fallback-light-bright": {
        "description": "Always-on fallback light at high radiance for dark scenes",
        "options": {
            "rtx.fallbackLightMode": "2",
            "rtx.fallbackLightType": "0",
            "rtx.fallbackLightRadiance": "8.0, 8.0, 8.0",
        },
    },
    "fallback-light-off": {
        "description": "Never create the fallback light (production setting)",
        "options": {
            "rtx.fallbackLightMode": "0",
        },
    },
    "vertex-capture": {
        "description": "Capture shader-transformed vertices for simple "
                       "vertex-shader games that still set FFP matrices",
        "options": {
            "rtx.useVertexCapture": "True",
            "rtx.useVertexCapturedNormals": "True",
        },
    },
}


# ── rtx.conf editing ───────────────────────────────────────────────────────

def conf_path(game_dir: str | Path) -> Path:
    return Path(game_dir) / RTX_CONF


def parse_conf(text: str) -> dict[str, str]:
    """Parse rtx.conf text into {option: value}, last assignment wins."""
    options: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        options[key.strip()] = value.strip()
    return options


def load_conf(path: str | Path) -> dict[str, str]:
    """Load {option: value} from an rtx.conf file (empty dict if missing)."""
    p = Path(path)
    if not p.is_file():
        return {}
    return parse_conf(p.read_text(encoding="utf-8", errors="replace"))


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = path.with_name(f"{path.name}.{stamp}.bak")
    if not bak.exists():
        shutil.copy2(path, bak)
    return bak


def set_option(path: str | Path, key: str, value: str,
               backup: bool = True) -> Path | None:
    """Set (or replace) one option in rtx.conf, preserving all other lines.

    The existing assignment line is rewritten in place if present (first
    occurrence; later duplicates are removed so the file stays unambiguous),
    otherwise the assignment is appended.

    Args:
        path:   rtx.conf path (created if missing).
        key:    Option name, e.g. "rtx.fallbackLightMode".
        value:  Value string written verbatim after "= ".
        backup: Write a timestamped .bak sibling before modifying.

    Returns:
        Backup path if one was created, else None.
    """
    p = Path(path)
    bak = _backup(p) if backup else None
    lines = (p.read_text(encoding="utf-8", errors="replace").splitlines()
             if p.is_file() else [])
    new_line = f"{key} = {value}"
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if (not stripped.startswith("#") and "=" in stripped
                and stripped.partition("=")[0].strip() == key):
            if not replaced:
                out.append(new_line)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return bak


def unset_option(path: str | Path, key: str, backup: bool = True) -> bool:
    """Remove an option from rtx.conf. Returns True if it was present."""
    p = Path(path)
    if not p.is_file():
        return False
    if backup:
        _backup(p)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    removed = False
    for line in lines:
        stripped = line.strip()
        if (not stripped.startswith("#") and "=" in stripped
                and stripped.partition("=")[0].strip() == key):
            removed = True
            continue
        out.append(line)
    if removed:
        p.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return removed


def _parse_hash_list(value: str) -> list[str]:
    return [h.strip() for h in value.split(",") if h.strip()]


def add_hash(path: str | Path, key: str, hash_value: str,
             backup: bool = True) -> list[str]:
    """Append a hash to a hash-set option (deduplicated, order preserved).

    Returns:
        The resulting hash list.
    """
    current = _parse_hash_list(load_conf(path).get(key, ""))
    normalized = hash_value.strip()
    if normalized.lower() not in (h.lower() for h in current):
        current.append(normalized)
        set_option(path, key, ", ".join(current), backup=backup)
    return current


def remove_hash(path: str | Path, key: str, hash_value: str,
                backup: bool = True) -> list[str]:
    """Remove a hash from a hash-set option. Returns the resulting list."""
    current = _parse_hash_list(load_conf(path).get(key, ""))
    kept = [h for h in current if h.lower() != hash_value.strip().lower()]
    if len(kept) != len(current):
        if kept:
            set_option(path, key, ", ".join(kept), backup=backup)
        else:
            unset_option(path, key, backup=backup)
    return kept


def apply_preset(path: str | Path, name: str, backup: bool = True) -> dict:
    """Apply a named preset's options to rtx.conf.

    Returns:
        The preset's option dict.

    Raises:
        KeyError: If the preset name is unknown.
    """
    preset = PRESETS[name]
    first = True
    for key, value in preset["options"].items():
        set_option(path, key, value, backup=backup and first)
        first = False
    return preset["options"]


# ── Runtime detection and logs ─────────────────────────────────────────────

# dxvk-remix ships a large d3d9.dll; a plain proxy or stock wrapper is far
# smaller. Used only as a hint alongside stronger markers.
_REMIX_D3D9_MIN_BYTES = 8 * 1024 * 1024

LOG_PATTERNS = ("*_d3d9.log", "*_dxvk.log", "d3d9.log",
                "NvRemixBridge*.log", "bridge*.log")


def detect_runtime(game_dir: str | Path) -> dict:
    """Inspect a game directory for an RTX Remix runtime installation.

    Returns:
        dict with:
            game_dir:      resolved directory
            d3d9_dll:      present / size
            remix_markers: which installation markers were found
            is_remix:      overall verdict
            rtx_conf:      path if present
            logs:          matching log files (name, size, mtime)
    """
    d = Path(game_dir)
    markers: list[str] = []

    d3d9 = d / "d3d9.dll"
    d3d9_size = d3d9.stat().st_size if d3d9.is_file() else 0

    if (d / ".trex").is_dir():
        markers.append(".trex/ directory (bridge runtime)")
    for name in ("d3d9_remix.dll", "NvRemixBridge.exe"):
        if (d / name).is_file():
            markers.append(name)
    if (d / ".trex" / "NvRemixBridge.exe").is_file():
        markers.append(".trex/NvRemixBridge.exe")
    if d3d9_size >= _REMIX_D3D9_MIN_BYTES:
        markers.append(f"large d3d9.dll ({d3d9_size // (1024 * 1024)} MB)")

    logs = []
    for pattern in LOG_PATTERNS:
        for f in sorted(d.glob(pattern)):
            st = f.stat()
            logs.append({"name": f.name, "size": st.st_size,
                         "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(st.st_mtime))})

    conf = conf_path(d)
    return {
        "game_dir": str(d),
        "d3d9_dll": {"present": d3d9.is_file(), "size": d3d9_size},
        "remix_markers": markers,
        "is_remix": bool(markers),
        "rtx_conf": str(conf) if conf.is_file() else None,
        "logs": logs,
    }


def read_logs(game_dir: str | Path, tail: int = 40,
              errors_only: bool = False) -> dict[str, list[str]]:
    """Read the tail of each Remix/dxvk log in the game directory.

    Args:
        game_dir:    Game directory to scan.
        tail:        Lines to keep from the end of each log.
        errors_only: Keep only lines containing err/warn markers.

    Returns:
        {log_name: [lines]}
    """
    d = Path(game_dir)
    result: dict[str, list[str]] = {}
    for pattern in LOG_PATTERNS:
        for f in sorted(d.glob(pattern)):
            try:
                lines = f.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
            except OSError:
                continue
            if errors_only:
                lines = [ln for ln in lines
                         if "err" in ln.lower() or "warn" in ln.lower()]
            result[f.name] = lines[-tail:]
    return result


# ── Menu toggle (Windows only, uses gamectl) ───────────────────────────────

def toggle_menu(exe: str | None = None, window: str | None = None,
                chord: str = MENU_CHORD) -> dict:
    """Focus the game window and send the Remix menu chord (default ALT+X).

    The default matches rtx.remixMenuKeyBinds's default; pass chord if the
    game's rtx.conf overrides it.

    Returns:
        dict with ok, focused, combo (or error).
    """
    from . import gamectl as gc
    hwnd, err = gc.resolve_hwnd(exe, window)
    if not hwnd:
        return {"ok": False, "error": err}
    focused = gc.focus_hwnd(hwnd)
    result = gc.send_chord(chord)
    result["focused"] = focused
    return result
