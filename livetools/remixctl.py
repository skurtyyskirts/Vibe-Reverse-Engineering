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
    python -m livetools remix conf set --surface dxvk d3d9.enableDialogMode True -d DIR
    python -m livetools remix preset list
    python -m livetools remix preset apply debug-geometry-hash --game-dir DIR
    python -m livetools remix preset apply windowed-capture --game-dir DIR
    python -m livetools remix menu --exe game.exe
    python -m livetools remix log --game-dir DIR --errors --tail 40
    python -m livetools remix capture trigger --game-dir DIR --exe game.exe
    python -m livetools remix capture assets --game-dir DIR

Usage (library):
    from livetools import remixctl
    remixctl.set_option(remixctl.conf_path(game_dir), "rtx.fallbackLightMode", "2")
    remixctl.apply_preset(game_dir, "debug-geometry-hash")
    remixctl.capture_assets(game_dir)["assets"]["texture"]
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

RTX_CONF = "rtx.conf"

#: A Remix install has three key=value config files, not one. The two beyond
#: rtx.conf own the settings that decide whether an unattended run can see the
#: game at all (exclusive fullscreen) or open the Remix UI (DirectInput games
#: that swallow the hotkey), so they are editable through the same commands.
#: Paths are relative to the game directory.
CONF_SURFACES = {
    "rtx": RTX_CONF,          # dxvk-remix renderer options
    "dxvk": "dxvk.conf",      # DXVK/D3D9 layer options
    "bridge": "bridge.conf",  # 32-bit bridge client/server options
}

#: bridge.conf ships inside .trex/ in some layouts and beside the exe in
#: others; whichever exists wins, and a new file is created at the primary
#: location.
_BRIDGE_ALT = Path(".trex") / "bridge.conf"

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
#
# The option reference already carries this as a column, so the authoritative
# set is derived from it; this literal is the fallback for a checkout with no
# table. Maintaining it by hand had already let one option drift out
# (rtx.rayPortalModelTextureHashes), which add-hash then refused to write.
_FALLBACK_HASH_SET_OPTIONS = frozenset({
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


def hash_set_options() -> frozenset[str]:
    """Every rtx.conf option that takes a list of asset hashes."""
    from . import rtx_options

    derived = frozenset(name for name, entry in rtx_options.load().items()
                        if "hash" in entry["type"].lower())
    return derived or _FALLBACK_HASH_SET_OPTIONS


HASH_SET_OPTIONS = hash_set_options()

# Presets: named option bundles for common compatibility/debugging setups.
# `options` maps config surface -> {option: value}, so one preset can span
# rtx.conf, dxvk.conf and bridge.conf — several fixes need more than one.
PRESETS: dict[str, dict] = {
    "devmenu": {
        "description": "Developer-friendly menu: ALT+X opens Advanced UI with cursor",
        "options": {"rtx": {
                "rtx.defaultToAdvancedUI": "True",
                "rtx.showUICursor": "True",
        }},
    },
    "debug-geometry-hash": {
        "description": "Color geometry by hash (view 277). Flicker across "
                       "identical frames = unstable hashes.",
        "options": {"rtx": {
                "rtx.debugView.debugViewIdx": "277",
        }},
    },
    "debug-off": {
        "description": "Disable the debug view (back to normal rendering)",
        "options": {"rtx": {
                "rtx.debugView.debugViewIdx": "0",
        }},
    },
    "hash-stable-anim": {
        "description": "Hash rule for games with CPU-animated/skinned vertex "
                       "data: drop per-frame-varying positions/texcoords from "
                       "generation hashing. Distinct meshes sharing one "
                       "layout+shader may collide — verify with geometry-hash "
                       "debug view.",
        "options": {"rtx": {
                "rtx.geometryGenerationHashRuleString":
                    "indices,geometrydescriptor,vertexlayout,vertexshader",
        }},
    },
    "hash-default": {
        "description": "Restore dxvk-remix default hash rules",
        "options": {"rtx": {
                "rtx.geometryGenerationHashRuleString":
                    "positions,indices,texcoords,geometrydescriptor,vertexlayout,vertexshader",
                "rtx.geometryAssetHashRuleString":
                    "positions,indices,geometrydescriptor",
        }},
    },
    "fallback-light-always": {
        "description": "Always create the camera-following fallback light "
                       "(scene visible even with no converted game lights)",
        "options": {"rtx": {
                "rtx.fallbackLightMode": "2",
                "rtx.fallbackLightType": "0",
                "rtx.fallbackLightRadiance": "1.6, 1.8, 2.0",
        }},
    },
    "fallback-light-bright": {
        "description": "Always-on fallback light at high radiance for dark scenes",
        "options": {"rtx": {
                "rtx.fallbackLightMode": "2",
                "rtx.fallbackLightType": "0",
                "rtx.fallbackLightRadiance": "8.0, 8.0, 8.0",
        }},
    },
    "fallback-light-off": {
        "description": "Never create the fallback light (production setting)",
        "options": {"rtx": {
                "rtx.fallbackLightMode": "0",
        }},
    },
    "vertex-capture": {
        "description": "Prefer vertex-shader output texcoords when a shader "
                       "game animates UVs. Vertex capture and captured normals "
                       "are already on by default — this is the one knob in "
                       "the family that is not, so it is the only one worth "
                       "writing.",
        "options": {"rtx": {
                "rtx.useVertexCapturedTexcoords": "True",
        }},
    },
    "automation": {
        "description": "Remix's own unattended-run settings: no blocking "
                       "dialogs, no per-frame memory readout in the image "
                       "(which would make every screenshot diff non-zero), no "
                       "asset-loading error popups. Apply this first on any "
                       "autonomous run.",
        "options": {"rtx": {
                "rtx.automation.disableBlockingDialogBoxes": "True",
                "rtx.automation.disableDisplayMemoryStatistics": "True",
                "rtx.automation.suppressAssetLoadingErrors": "True",
        }},
    },
    "sky-autodetect": {
        "description": "Tag sky draws by camera heuristics instead of by "
                       "hash — the one sky mechanism that needs no human "
                       "classifying textures. Try before rtx.skyBoxTextures.",
        "options": {"rtx": {
                "rtx.skyAutoDetect": "2",
                "rtx.skyForceAutoDetectedToReproject": "True",
        }},
    },
    "windowed-capture": {
        "description": "Force the game out of exclusive fullscreen so GDI "
                       "screenshots capture actual frames instead of black. "
                       "This is the fix for a black `screenshot grab`, and it "
                       "needs dxvk.conf/bridge.conf, not rtx.conf.",
        "options": {
            "dxvk": {"d3d9.enableDialogMode": "True"},
            "bridge": {"client.forceWindowed": "True"},
        },
    },
    "menu-input-force": {
        "description": "Always forward DirectInput to the Windows message "
                       "pump so the Remix UI hotkey reaches the runtime. Apply "
                       "when ALT+X does nothing — the game is swallowing all "
                       "key input.",
        "options": {
            "bridge": {
                "client.DirectInput.forward.keyboardPolicy": "3",
                "client.DirectInput.forward.mousePolicy": "2",
            },
        },
    },
    "verbose-logs": {
        "description": "Debug-level bridge logging — use when a launch fails "
                       "with nothing useful in the d3d9 log.",
        "options": {"bridge": {"logLevel": "Debug"}},
    },
    "capture-ready": {
        "description": "Make the capture hotkey (CTRL+SHFT+Q) write a USD "
                       "capture immediately instead of opening the capture "
                       "menu — the unattended source of asset hashes.",
        "options": {"rtx": {
                "rtx.captureShowMenuOnHotkey": "False",
                "rtx.captureInstances": "True",
        }},
    },
    "keep-textures": {
        "description": "Keep every texture resident so short-lived ones "
                       "(loading screens, one-frame HUD elements) still show "
                       "up for tagging. Costs VRAM — development only.",
        "options": {"rtx": {
                "rtx.keepTexturesForTagging": "True",
        }},
    },
    "anticulling-on": {
        "description": "Keep game-culled objects and lights in the ray "
                       "tracing scene so reflections and shadows include "
                       "geometry the game frustum-culled.",
        "options": {"rtx": {
                "rtx.antiCulling.object.enable": "True",
                "rtx.antiCulling.object.numObjectsToKeep": "10000",
                "rtx.antiCulling.light.enable": "True",
                "rtx.antiCulling.light.numFramesToExtendLightLifetime": "1000",
        }},
    },
    "anticulling-off": {
        "description": "Disable anti-culling (restore runtime defaults)",
        "options": {"rtx": {
                "rtx.antiCulling.object.enable": "False",
                "rtx.antiCulling.light.enable": "False",
        }},
    },
}


# ── rtx.conf editing ───────────────────────────────────────────────────────

def conf_path(game_dir: str | Path, surface: str = "rtx") -> Path:
    """Path to one of a Remix install's config files.

    Args:
        game_dir: Game directory.
        surface:  `rtx`, `dxvk` or `bridge`.

    Returns:
        The config path, whether or not it exists yet.

    Raises:
        KeyError: On an unknown surface.
    """
    root = Path(game_dir)
    if surface == "bridge" and not (root / CONF_SURFACES["bridge"]).is_file() \
            and (root / _BRIDGE_ALT).is_file():
        return root / _BRIDGE_ALT
    return root / CONF_SURFACES[surface]


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


#: rtx.conf backups land here, relative to the game directory, unless the
#: caller names somewhere better. An autonomous run rewrites rtx.conf dozens
#: of times per session; scattering that many .bak siblings through the game
#: root buries the game's own files.
BACKUP_SUBDIR = "rtx-remix-backups"


def _backup(path: Path, backup_dir: str | Path | None = None) -> Path | None:
    if not path.is_file():
        return None
    folder = Path(backup_dir) if backup_dir else path.parent / BACKUP_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = folder / f"{path.name}.{stamp}.bak"
    n = 2
    while bak.exists():
        bak = folder / f"{path.name}.{stamp}-{n}.bak"
        n += 1
    shutil.copy2(path, bak)
    return bak


def set_option(path: str | Path, key: str, value: str,
               backup: bool = True,
               backup_dir: str | Path | None = None) -> Path | None:
    """Set (or replace) one option in rtx.conf, preserving all other lines.

    The existing assignment line is rewritten in place if present (first
    occurrence; later duplicates are removed so the file stays unambiguous),
    otherwise the assignment is appended.

    Args:
        path:   rtx.conf path (created if missing).
        key:    Option name, e.g. "rtx.fallbackLightMode".
        value:  Value string written verbatim after "= ".
        backup: Write a timestamped backup before modifying.
        backup_dir: Where that backup goes. Defaults to a BACKUP_SUBDIR folder
            in the game directory; pass the project workspace's `backups/` to
            keep run history with the rest of the project.

    Returns:
        Backup path if one was created, else None.
    """
    p = Path(path)
    bak = _backup(p, backup_dir) if backup else None
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


def unset_option(path: str | Path, key: str, backup: bool = True,
                 backup_dir: str | Path | None = None) -> bool:
    """Remove an option from rtx.conf. Returns True if it was present."""
    p = Path(path)
    if not p.is_file():
        return False
    if backup:
        _backup(p, backup_dir)
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
             backup: bool = True,
             backup_dir: str | Path | None = None) -> list[str]:
    """Append a hash to a hash-set option (deduplicated, order preserved).

    Returns:
        The resulting hash list.
    """
    current = _parse_hash_list(load_conf(path).get(key, ""))
    normalized = hash_value.strip()
    if normalized.lower() not in (h.lower() for h in current):
        current.append(normalized)
        set_option(path, key, ", ".join(current), backup=backup,
                   backup_dir=backup_dir)
    return current


def remove_hash(path: str | Path, key: str, hash_value: str,
                backup: bool = True,
                backup_dir: str | Path | None = None) -> list[str]:
    """Remove a hash from a hash-set option. Returns the resulting list."""
    current = _parse_hash_list(load_conf(path).get(key, ""))
    kept = [h for h in current if h.lower() != hash_value.strip().lower()]
    if len(kept) != len(current):
        if kept:
            set_option(path, key, ", ".join(kept), backup=backup,
                       backup_dir=backup_dir)
        else:
            unset_option(path, key, backup=backup, backup_dir=backup_dir)
    return kept


def apply_preset(game_dir: str | Path, name: str, backup: bool = True,
                 backup_dir: str | Path | None = None) -> dict:
    """Apply a named preset across whichever config files it touches.

    Args:
        game_dir:   Game directory holding rtx.conf / dxvk.conf / bridge.conf.
        name:       Preset name from PRESETS.
        backup:     Back up each file before its first write.
        backup_dir: Where backups go (see `_backup`).

    Returns:
        {surface: {option: value}} as written, with the resolved path per
        surface under `paths`.

    Raises:
        KeyError: If the preset name is unknown.
    """
    preset = PRESETS[name]
    paths = {}
    for surface, options in preset["options"].items():
        path = conf_path(game_dir, surface)
        paths[surface] = str(path)
        first = True
        for key, value in options.items():
            set_option(path, key, value, backup=backup and first,
                       backup_dir=backup_dir)
            first = False
    return {"options": preset["options"], "paths": paths}


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
            configs:       per surface (rtx/dxvk/bridge): path, present, option count
            logs:          matching log files (name, size, mtime)
    """
    d = Path(game_dir)

    d3d9 = d / "d3d9.dll"
    d3d9_size = d3d9.stat().st_size if d3d9.is_file() else 0
    conf = conf_path(d)

    strong: list[str] = []
    if (d / ".trex").is_dir():
        strong.append(".trex/ directory (bridge runtime)")
    for name in ("d3d9_remix.dll", "NvRemixBridge.exe"):
        if (d / name).is_file():
            strong.append(name)
    if (d / ".trex" / "NvRemixBridge.exe").is_file():
        strong.append(".trex/NvRemixBridge.exe")

    # dll size alone can match any fat wrapper — it only counts alongside
    # a strong marker or an rtx.conf
    markers = list(strong)
    if d3d9_size >= _REMIX_D3D9_MIN_BYTES:
        markers.append(f"large d3d9.dll ({d3d9_size // (1024 * 1024)} MB)")
    is_remix = bool(strong) or (d3d9_size >= _REMIX_D3D9_MIN_BYTES
                                and conf.is_file())

    logs = []
    for pattern in LOG_PATTERNS:
        for f in sorted(d.glob(pattern)):
            st = f.stat()
            logs.append({"name": f.name, "size": st.st_size,
                         "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                time.localtime(st.st_mtime))})

    configs = {}
    for surface in CONF_SURFACES:
        path = conf_path(d, surface)
        configs[surface] = {"path": str(path), "present": path.is_file(),
                            "options": len(load_conf(path))}

    return {
        "game_dir": str(d),
        "d3d9_dll": {"present": d3d9.is_file(), "size": d3d9_size},
        "remix_markers": markers,
        "is_remix": is_remix,
        "rtx_conf": str(conf) if conf.is_file() else None,
        "configs": configs,
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


# ── USD captures ───────────────────────────────────────────────────────────

# Remix writes captures under <game_dir>/rtx-remix/captures, with assets
# exported into sibling folders named for their category.
CAPTURE_SUBDIR = Path("rtx-remix") / "captures"
CAPTURE_CHORD = "CTRL+SHFT+Q"

#: Asset folder name -> the rtx.conf hash space its filenames belong to.
CAPTURE_ASSET_DIRS = {
    "textures": "texture", "materials": "material", "meshes": "mesh",
    "lights": "light", "thumbs": "thumbnail",
}

_HASH_CHARS = set("0123456789abcdefABCDEF")


def capture_root(game_dir: str | Path) -> Path:
    """Path to a game's capture output directory (may not exist yet)."""
    return Path(game_dir) / CAPTURE_SUBDIR


def list_captures(game_dir: str | Path) -> list[dict]:
    """List USD captures in a game directory, newest last.

    Returns:
        List of {name, path, mtime, size} for each captured stage file.
    """
    root = capture_root(game_dir)
    if not root.is_dir():
        return []
    stages = [f for f in root.iterdir()
              if f.is_file() and f.suffix.lower() in (".usd", ".usda", ".usdc")]
    return [{"name": f.name, "path": str(f), "size": f.stat().st_size,
             "mtime": f.stat().st_mtime}
            for f in sorted(stages, key=lambda f: f.stat().st_mtime)]


def newest_capture(game_dir: str | Path) -> dict | None:
    """Most recently written capture, or None if there are none."""
    captures = list_captures(game_dir)
    return captures[-1] if captures else None


def _hash_from_name(stem: str) -> str | None:
    """Extract the hash from a capture asset filename.

    Capture assets are written as `<hash><suffix>.<ext>`, so the leading run
    of hex characters is the hash. Anything shorter than 8 digits is a
    generated name (thumbnails, sky probes), not an asset identity.
    """
    run = ""
    for ch in stem:
        if ch in _HASH_CHARS:
            run += ch
        else:
            break
    return run.upper() if len(run) >= 8 else None


def capture_assets(game_dir: str | Path) -> dict:
    """Collect every asset hash the Remix runtime exported into captures.

    This is the unattended path to the hashes that `rtx.uiTextures`,
    `rtx.skyBoxTextures` and the other hash-set options need. The developer
    menu shows the same hashes, but only to a human clicking through the
    texture tabs; the capture writes them to disk where a script can read
    them, each next to the exported image it identifies.

    Args:
        game_dir: Game directory containing `rtx-remix/captures`.

    Returns:
        dict with:
            root:     the capture directory
            captures: `list_captures` result
            assets:   {category: [{hash, file}]} for each populated asset dir
            counts:   {category: n}

    Tagging a hash from here still needs verification — confirm the tag took
    with a debug view or by re-capturing after the restart.
    """
    root = capture_root(game_dir)
    assets: dict[str, list[dict]] = {}
    for dirname, category in CAPTURE_ASSET_DIRS.items():
        folder = root / dirname
        if not folder.is_dir():
            continue
        entries = []
        for f in sorted(folder.iterdir()):
            if not f.is_file():
                continue
            digest = _hash_from_name(f.stem)
            if digest:
                entries.append({"hash": f"0x{digest}", "file": str(f)})
        if entries:
            assets[category] = entries
    return {"root": str(root), "captures": list_captures(game_dir),
            "assets": assets,
            "counts": {k: len(v) for k, v in assets.items()}}


def trigger_capture(game_dir: str | Path, exe: str | None = None,
                    window: str | None = None, chord: str = CAPTURE_CHORD,
                    timeout: float = 30.0) -> dict:
    """Take a USD capture and wait for the runtime to finish writing it.

    Sending the hotkey is not evidence a capture happened — the game may be
    unfocused, the hotkey may be remapped, or `rtx.captureShowMenuOnHotkey`
    may have opened the menu instead of capturing. This watches the capture
    directory and reports the stage that actually appeared.

    Apply the `capture-ready` preset first so the hotkey captures immediately
    rather than opening the capture menu.

    Args:
        game_dir: Game directory holding `rtx-remix/captures`.
        exe:      Game executable name for window lookup.
        window:   Window title substring, as an alternative to exe.
        chord:    Capture hotkey (default matches `rtx.captureHotKey`).
        timeout:  Seconds to wait for a new stage file to settle.

    Returns:
        dict with ok, capture (the new stage) or error.
    """
    from . import gamectl as gc

    before = {c["name"] for c in list_captures(game_dir)}
    hwnd, err = gc.resolve_hwnd(exe, window)
    if not hwnd:
        return {"ok": False, "error": err}
    gc.focus_hwnd(hwnd)
    sent = gc.send_chord(chord)
    if not sent.get("ok"):
        return {"ok": False, "error": sent.get("error", "chord failed")}

    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline:
        time.sleep(1.0)
        fresh = [c for c in list_captures(game_dir) if c["name"] not in before]
        if fresh:
            newest = fresh[-1]
            # A stage still growing is mid-write; wait for its size to settle.
            if newest["size"] == last_size:
                return {"ok": True, "capture": newest, "chord": chord}
            last_size = newest["size"]
    return {"ok": False, "chord": chord,
            "error": f"No new capture within {timeout:.0f}s — check that the "
                     f"game was focused and preset 'capture-ready' is applied"}
