---
name: autonomous-remix-port
description: Use when asked to reverse engineer or port a game for RTX Remix autonomously / unattended / "while I'm away" — driving the game with inputs and screenshots, controlling the Remix runtime (rtx.conf, debug views, fallback light, dev menu), stabilizing geometry hashes, and iterating until the game renders correctly under Remix. Also use for setting up an overnight/looped porting session or resuming one from its state file.
---

# Autonomous RTX Remix Porting

Run the full reverse-engineering → Remix-compatibility loop without a human:
launch and drive the game, see it through screenshots, control the Remix
runtime, stabilize geometry hashes, and converge on correct rendering. Every
iteration leaves durable state so the session survives interruption and the
user returns to evidence, not just claims.

**Core loop: act → screenshot → Read the image → decide → record.** Never
chain blind inputs. Never claim a render state you have not screenshotted.

## Prerequisites (verify before starting, report anything missing)

1. Repo tools: `python verify_install.py` from repo root.
2. Game directory with the Remix runtime installed:
   `python -m livetools remix status --game-dir <GAMEDIR>` — needs Remix
   markers and ideally the remix-comp-proxy already deployed (if the game is
   shader-based and unported, that becomes Phase 6 work via `dx9-ffp-port`).
3. Game must run **windowed or borderless** — exclusive fullscreen captures
   black. If screenshots come back black/empty, fix this first (game config
   file or `d3d9.ForceWindowed`-style options in the game's own settings).
4. Project workspace: `patches/<Game>/` with `autonomy/` subfolder (create on
   first run). Timestamped backups per AGENTS.md before touching any config.

## Durable State (resume-safe autonomy)

All autonomous progress lives in `patches/<Game>/autonomy/`:

```
autonomy/
  state.json          current phase, per-phase status, open issues, next action
  screens/            numbered screenshots: NNN_<phase>_<what>.png
  journal.md          append-only: timestamp, action, evidence path, conclusion
```

`state.json` shape (extend freely, never repurpose fields):

```json
{
  "game_dir": "C:/Games/MyGame",
  "exe": "game.exe",
  "phase": 4,
  "phases": {"0": "done", "1": "done", "2": "done", "3": "in_progress"},
  "menu_map": {"title->play": "RETURN WAIT:1500 RETURN"},
  "issues": [{"id": "unstable-hud-hash", "status": "open", "evidence": "screens/012_5_hashflicker.png"}],
  "next_action": "capture 2-frame trace and run --hash-stability"
}
```

Rules:
- **One bounded step per iteration**, then update `state.json` + `journal.md`.
  A step is one verifiable unit: "sent macro X and screenshotted result", not
  "set up the game".
- On wake/start, read `state.json` first and continue from `next_action` —
  never restart phases that are `done`.
- Screenshots are numbered monotonically; journal entries reference them.
- Every rtx.conf change goes through `livetools remix conf/preset` (auto-backup).

## Unattended Operation

Designed to run under a recurring driver (`/loop`, scheduled wakeups, or a
plain long session):

- Each iteration: read state → one step → write state → (if looping) yield.
- **Watchdog** at the top of every iteration:
  `livetools gamectl --exe <exe> info` — if no window, check the process,
  relaunch with `livetools attach "<GAMEDIR>/<exe>" --spawn`, wait, verify
  with a screenshot. Record crashes in `journal.md` with the last action
  taken (crash loops after a specific action = stop repeating that action,
  file an issue in state).
- **Stop conditions** (end the loop, write a final report): success criteria
  met; the same action failed 3 times; the game crash-loops at startup; or a
  needed decision is genuinely user-only (e.g. installing the Remix runtime).
- Static analysis runs in background subagents (`static-analyzer`) while the
  live loop continues — never idle waiting for it.

## Phase Machine

Phases run in order; later phases loop back when verification fails.

### Phase 0 — Preflight
`verify_install.py`, `remix status`, create workspace + state file, back up
any existing rtx.conf / proxy ini. Gate: `remix status` shows Remix markers.

### Phase 1 — Static bootstrap (background)
If `patches/<Game>/kb.h` is sparse (`grep -cE '^[@$]|^struct ' kb.h` < 50):
spawn `static-analyzer` subagents for `bootstrap.py` and
`pyghidra_backend.py analyze` (see subagent-workflow.md). Also queue DX
scans: `classify_draws.py`, `find_vs_constants.py`, `find_skinning.py`.
Don't block on results — continue to Phase 2.

### Phase 2 — Launch and see
```
python -m livetools attach "<GAMEDIR>/<exe>" --spawn
python -m livetools screenshot grab --exe <exe> --out patches/<Game>/autonomy/screens/001_2_boot.png
```
Read the image. Gate: a real frame (title screen / menu), not black.
Black frame → fullscreen problem (see prerequisites) or crash (check
`remix log --errors`, `rtx_comp/diagnostics.log`).

### Phase 3 — Navigate into gameplay
Goal: reach a repeatable in-level viewpoint (menus don't exercise the 3D
pipeline). Method, screenshot-verified at every step:

1. Screenshot, Read it, identify the current screen.
2. Choose ONE input (`RETURN`, arrows, `ESCAPE`, or a click at visible
   button coordinates). Send via
   `livetools gamectl --exe <exe> keys "..."` / `click X Y`.
3. Screenshot again; `screenshot diff` confirms the screen changed; Read to
   see where you landed. Dead end → `ESCAPE` back, try the next candidate.
4. Record every discovered transition in `patches/<Game>/macros.json`
   (gamectl macro format) — second visits replay macros instead of exploring.
5. In-level test: 3D scene visible in screenshot + `livetools dipcnt` (if
   device pointer known) or a 2-frame dx9tracer capture shows hundreds of
   draws.

Games needing held keys or timing use `HOLD:KEY:ms` / `WAIT:ms` tokens;
Remix's own menu hotkeys pass through `CHORD:` tokens.

### Phase 4 — Remix baseline
```
python -m livetools remix preset apply devmenu -d <GAMEDIR>
python -m livetools remix log -d <GAMEDIR> --errors
```
Screenshot the normal render. Then verify runtime control end-to-end once:
`remix menu --exe <exe>` → screenshot (menu visible?) → `remix menu` again to
close. Cycle debug views by conf (restart between): preset
`debug-geometry-hash` → relaunch → screenshot → preset `debug-off`.
Restarts replay the Phase 3 macro to get back in-level. Gate: menu opens,
debug view renders, macros reliably reach the same viewpoint.

### Phase 5 — Trace and hash stability
At the repeatable viewpoint, camera still:
```
python -m graphics.directx.dx9.tracer trigger --game-dir <GAMEDIR> --frames 2 --wait
python -m graphics.directx.dx9.tracer analyze <GAMEDIR>/dxtrace_frame.jsonl --summary --classify-draws --hash-stability
```
(delegate heavy analysis passes to `static-analyzer`). Then the live flicker
test: preset `debug-geometry-hash`, restart, two screenshots 1s apart with
the camera still, `screenshot diff`. Ratio ≥ ~0.01 on a static scene =
unstable hashes.

Fix by finding-category (details: `.claude/references/remix-compat-catalog.md`):
- up-draws / dynamic VBs / vb-churn → `remix preset apply hash-stable-anim`
- pretransformed HUD draws → tag `rtx.uiTextures`
- programmable-vs draws → `rtx.useVertexCapture = True`, else Phase 6
- flickering signatures → `rtx.uniqueObjectDistance` tuning

Re-run the flicker test after every fix. Gate: diff ratio below threshold on
two consecutive checks, and `--hash-stability` recommendations addressed or
consciously deferred (recorded as issues).

### Phase 6 — FFP conversion (only if needed)
If Remix gets no usable geometry (shader-heavy game, vertex capture
insufficient): invoke the **`dx9-ffp-port` skill** — copy the proxy template
to `patches/<Game>/`, discover VS register layout (Phase 1 results + live
`trace` on SetVertexShaderConstantF callers), configure ini, build, deploy,
diagnose via `rtx_comp/diagnostics.log` + ImGui F4. Return to Phase 5 after
deploying — hashing changes when draw routing changes.

### Phase 7 — Rendering correctness
Work the symptom table in `remix-compat-catalog.md` until the normal render
looks right. For each symptom: screenshot evidence → apply the mapped fix →
restart → macro back in-level → screenshot → compare. Standard sequence:
1. Lighting present? (black scene → fallback light diagnosis pattern)
2. Sky correct? (`rtx.skyBoxTextures` / thresholds)
3. UI clean? (`rtx.uiTextures` complete)
4. No double lighting (lightmaps), no ghost/flicker (`uniqueObjectDistance`),
   decals stable.
5. Axis/scale sane in debug view 11 (`rtx.zUp`, `rtx.sceneScale`).
Each fix is one iteration with before/after screenshots in the journal.

### Phase 8 — Report and persist
- `patches/<Game>/findings.md`: what was discovered, every fix applied and
  why, open issues with evidence links.
- kb.h updated with any functions/globals identified along the way.
- Final rtx.conf diff vs. the Phase 0 backup, summarized in the report.
- state.json marked complete with success-criteria checklist:
  1. game launches under Remix and reaches gameplay via recorded macros,
  2. hash flicker test passes,
  3. normal render screenshot shows lit, correctly textured scene with clean
     UI and sky,
  4. all applied settings survive a restart (verify once end-to-end).

## Anti-patterns

- **Blind input chains** — more than one input between screenshots while
  exploring.
- **Menu-only settings** — anything durable belongs in rtx.conf via
  `remix conf`; the dev menu is for live experiments only.
- **Restarting from scratch** — `state.json` + `macros.json` exist so no
  discovery is ever repeated.
- **Claiming without evidence** — every conclusion in the journal cites a
  screenshot, log excerpt, or analyzer output.
- **Waiting idle on subagents** — the live loop always has a next step.

## References

- rtx.conf option catalog + symptom→fix playbook: `.claude/references/remix-compat-catalog.md`
- livetools syntax (attach/trace/gamectl/screenshot/remix): `/dynamic-analysis` skill, `.claude/references/tool-catalog.md`
- FFP porting: `dx9-ffp-port` skill
- Delegation rules: `.claude/rules/subagent-workflow.md`
