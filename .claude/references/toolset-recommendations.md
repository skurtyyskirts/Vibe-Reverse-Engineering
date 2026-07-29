# Toolset Recommendations

Tools worth adding to this repo, ranked by how much closer each gets an
unattended port to succeeding without a human. Every entry names the actual
project, its license, and where it would live here.

This is a shopping list, not a plan of record. Nothing below is installed.

## Already closed (for reference)

These were gaps; they are now covered in-repo, so don't re-solve them:

- **Remix config surfaces** — `dxvk.conf` and `bridge.conf` are editable via
  `remix conf --surface` and the `windowed-capture` / `menu-input-force`
  presets.
- **DPI awareness** — `gamectl.set_dpi_aware()` runs before clicks and captures.
- **rtx.conf option ground truth** — all ~1000 options ship offline
  (`remix options`), regenerated with `remix options sync`.
- **Texture hashes without the dev menu** — `remix capture trigger|assets`
  reads the hashes out of a USD capture's exported asset filenames.

## Tier 1 — fixes a failure mode that currently ends the run

### DXcam — non-GDI screen capture
`pip install dxcam` · [ra1nty/DXcam](https://github.com/ra1nty/DXcam) · MIT

Capture is GDI-only (`PrintWindow`/`BitBlt`), which is why "run the game
windowed" is a prerequisite and why a black frame is the most common way an
unattended run dies. DXcam wraps DXGI Desktop Duplication with a Windows
Graphics Capture backend and captures **exclusive-fullscreen D3D** apps.

Integration: `livetools/capture.py` with the same `(width, height, rgb)`
contract `screenshot.capture_window` returns, selected automatically when the
GDI path classifies a frame as `black`. Everything downstream — `frame_stats`,
`classify_frame`, `tiled_diff`, `health` — is unchanged.

Why it matters most: it removes a hard prerequisite rather than working around
one, and turns `not-rendering` into a real diagnosis instead of an ambiguity
between "capture broke" and "the renderer broke".

### Detect It Easy — packer / DRM / anti-tamper detection
[horsicq/Detect-It-Easy](https://github.com/horsicq/Detect-It-Easy) · MIT ·
console build `diec` with `-j` JSON output

Nothing assesses whether a binary is portable *before* the analysis budget is
spent. A Themida/VMProtect/Denuvo-wrapped executable makes bootstrap produce
nothing useful, and the failure looks like a tooling problem for hours.

Integration: `retools/protection.py`, run as the first bootstrap stage.
Combine `diec` signatures with a cheap section-entropy check (pefile is already
a dependency) and a Steam-stub import check. A "packed, expect no useful static
analysis" verdict at minute one is worth more than any later tool.

### Windows Error Reporting LocalDumps — make the crash analyzer reachable
Built into Windows · no dependency

`retools/dumpinfo.py` is a full minidump analyzer with nothing to analyze:
nothing configures Windows to produce a dump. One registry key under
`HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\<exe>`
pointing `DumpFolder` at `patches/<Game>/dumps` turns every crash the watchdog
already detects into evidence.

Integration: a `livetools health --enable-dumps` / preflight step. Needs
elevation, so it is a one-time setup action that reports clearly when it cannot
run.

## Tier 2 — meaningfully better decisions

### RapidOCR + OpenCV template matching — read the menu instead of guessing
`pip install rapidocr-onnxruntime` (Apache 2.0) · `opencv-python` (Apache 2.0)

Navigation reads a screenshot with a vision model each step: accurate but slow
and non-deterministic, and it cannot say *where* a button is. OCR gives menu
text with bounding boxes (click targets, no coordinate guessing), and template
matching re-finds a known button after a resolution change.

Integration: `livetools/vision.py` with both as optional imports —
`screenshot text <png>` and `screenshot find <png> <template.png>`. Keeps the
vision model for judgement ("is this scene lit correctly") and hands it the
mechanical parts.

### capa — semantic capability labels during bootstrap
`pip install flare-capa` · [mandiant/capa](https://github.com/mandiant/capa) ·
Apache 2.0

Bootstrap classifies functions by callee patterns. capa labels them by
behaviour ("initializes DirectX", "reads registry", "decompresses data") and
extracts features through PyGhidra — already a dependency, so it reuses the
per-game Ghidra project rather than adding a backend.

Integration: a bootstrap stage writing labels into `index.db` and seeding kb.h
comments.

### usd-core — parse captures properly, not just their filenames
`pip install usd-core` · Apache 2.0 · the `pxr` API without Omniverse

`remix capture assets` reads hashes out of exported asset filenames, which is
enough to tag but not to reason: it cannot say which mesh a texture belongs to,
how many instances use it, or where it sits in the scene. Parsing the USD stage
gives the mesh↔material↔texture graph, so "this texture is on 400 instances
covering the whole screen" becomes answerable — which is most of what deciding
`uiTextures` vs `skyBoxTextures` actually needs.

### Community rtx.conf corpus
Published per-game configs (ModDB, Nexus, the Remix Discord/GitHub)

Every port starts from defaults and rediscovers settings someone already
published. A cached corpus plus `remix corpus search "<game>"` and a diff
against the game's current conf turns a solved port into a starting point.
Licensing is per-config; cache locally, never vendor.

### Typed value and pointer scanning in the Frida agent
No new dependency — extend `livetools/agent.js`

`livetools scan` is byte-pattern only. Typed scans (i32/f32/string) with
successive refinement and pointer-path resolution are how you find a camera
matrix or a culling flag from its observed value. Cheat Engine has no clean
headless interface and its scriptable parts are GPL, so extending the existing
agent is both simpler and cleaner.

## Tier 3 — widens what can be ported, or saves real time

### d3d8to9 and DirectDraw frontends — more games in scope
[crosire/d3d8to9](https://github.com/crosire/d3d8to9) · BSD-2

Intake is D3D9-only. A managed, documented D3D8/DDraw translation stage brings
in a large slice of the games Remix explicitly targets. Belongs in
`rtx_remix_tools/dx/frontends/` as a deployable stage with its known side
effects written down, not as an invisible prerequisite.

### apitrace — deterministic D3D9 trace and replay
[apitrace/apitrace](https://github.com/apitrace/apitrace) · MIT ·
D3D9 is among its best-supported APIs

Every rtx.conf iteration currently costs a relaunch and a menu replay. Replaying
a captured frame instead makes A/B comparisons deterministic and fast. Large
effort, but it attacks the loop's dominant cost. (RenderDoc is **not** the
D3D9 answer — it does not capture D3D9 at all; its use here is inspecting the
Vulkan stream of a plain-DXVK run.)

### Remix-compatible hashing, computed offline
Extend the tracer + port dxvk-remix's hash rules

The tracer records texture *pointers*, never bytes, so hashes can only come
from a running Remix. Capturing buffer/texture contents behind a `--content`
flag and reimplementing the documented hash rules would let hash stability be
analyzed from a trace — before ever launching under Remix. Large, and it must
track upstream rule changes.

### vgamepad — controller-required games
`pip install vgamepad` · [yannbouteiller/vgamepad](https://github.com/yannbouteiller/vgamepad) · MIT

Console ports that ignore keyboard input cannot be navigated at all today.
Virtual XInput plus `PAD:` macro tokens alongside the existing key tokens.

### ghidriff — cross-version binary diffing
`pip install ghidriff` · [clearbluejar/ghidriff](https://github.com/clearbluejar/ghidriff) · MIT

Built on Ghidra headless, which this repo already runs. Carries kb.h findings
across a game patch instead of redoing them, and diffs a packed against an
unpacked dump.

### PDB symbol acquisition
No new dependency — pefile reads the CodeView debug directory

Fetch from the Microsoft symbol server by GUID+age. Middleware and CRT code
stop being reverse engineered from scratch on every game.

### Host preflight for unattended GPU runs
No new dependency

A locked workstation, a disconnected RDP session, or session 0 silently kills
Remix rendering and input delivery — and looks exactly like a broken port.
Check for a DXR-capable adapter and an interactive session, and say so up
front.

### Optional commercial backends — idalib / Binary Ninja
Licensed, user-supplied

A third `--backend` for users who own them. Strictly optional; the free path
stays the default and stays supported.

### Forced-windowed ladder
Special K, ReShade-style wrappers · closed-source freeware / permissive

For the games where `d3d9.enableDialogMode` and `client.forceWindowed` are not
enough. Worth documenting as a ranked fallback ladder even where the tool
cannot be redistributed.
