---
name: autonomous-remix-port
description: Use when asked to reverse engineer or port a game for RTX Remix autonomously / unattended / "while I'm away" — driving the game with inputs and screenshots, controlling the Remix runtime (rtx.conf, debug views, captures, fallback light, dev menu), stabilizing geometry hashes, and iterating until the game renders correctly under Remix. Also use for setting up an overnight/looped porting session or resuming one from its state file.
---

# Autonomous RTX Remix Porting

Run the full reverse-engineering → Remix-compatibility loop without a human:
launch and drive the game, see it through screenshots, control the Remix
runtime, stabilize geometry hashes, and converge on correct rendering. Every
iteration leaves durable state so the session survives interruption and the
user returns to evidence, not just claims.

**Core loop: watchdog → act → screenshot → Read the image → decide → record.**
Never chain blind inputs. Never claim a render state you have not screenshotted.

## Prerequisites (verify before starting, report anything missing)

1. Repo tools: `python verify_install.py` from repo root.
2. Game directory with the Remix runtime installed:
   `python -m livetools remix status --game-dir <GAMEDIR>` — needs Remix
   markers and ideally the remix-comp-proxy already deployed (if the game is
   shader-based and unported, that becomes Phase 6 work via `dx9-ffp-port`).
3. Game must run **windowed or borderless** — exclusive fullscreen captures
   black. `screenshot grab` says so (`frame: black`, exit 3) instead of saving
   a black PNG silently. Fix it without touching the game's own settings:
   `remix preset apply windowed-capture -d <GAMEDIR>` writes
   `d3d9.enableDialogMode` (dxvk.conf) and `client.forceWindowed`
   (bridge.conf). If ALT+X later does nothing, the game is swallowing key
   input — `remix preset apply menu-input-force -d <GAMEDIR>`.
4. Overnight runs: start `python -m livetools proc keep-awake --duration 43200`
   in the background. A slept machine stops delivering input and captures black.

## Durable State

`python -m autonomy` owns all progress under `patches/<Game>/autonomy/`:

```
autonomy/
  state.json          phase progress, attempt budgets, open issues, next action
  journal.md          append-only: timestamp, action, evidence, conclusion
  screens/            NNN_<phase>_<label>.png, numbered by the tool
```

```bash
python -m autonomy init MyGame --game-dir "C:/Games/MyGame" --exe game.exe \
    --goal "reaches gameplay and renders lit, correctly textured, clean UI"
python -m autonomy status MyGame                  # start every iteration here
python -m autonomy shot-path MyGame title         # reserve the next screenshot path
python -m autonomy step MyGame --action "sent RETURN at title" \
    --key nav:title --outcome ok --evidence screens/003_3_title.png \
    --conclusion "reached main menu" --next "select Play"
python -m autonomy phase MyGame --complete 3 --gate screens/007_3_ingame.png
python -m autonomy issue MyGame --add sky-missing --summary "sky drawn near" \
    --evidence screens/019_7_sky.png
python -m autonomy report MyGame --out patches/MyGame/findings.md
```

Rules:
- **One bounded step per iteration**, then `autonomy step`. A step is one
  verifiable unit: "sent macro X and screenshotted the result", not "set up
  the game".
- On wake/start, `autonomy status` first and continue from `next_action` —
  never restart phases that are `done`.
- Give every repeated attempt the same `--key`. `--outcome fail` increments
  that key's budget; **`step` exits 3 when the key is exhausted** (3 failures)
  — that is the signal to change approach, not to retry again.
- `--outcome ok` clears the budget, so an unrelated later failure gets its own
  three chances.
- `phase --complete` requires `--gate` evidence. A phase without evidence is a
  claim, not a result.
- Every rtx.conf change goes through `livetools remix conf/preset`
  (auto-backed-up into `<GAMEDIR>/rtx-remix-backups/`; pass `--backup-dir` to
  keep them with the project instead).

## Unattended Operation

Designed to run under a recurring driver (`/loop`, scheduled wakeups, or a
plain long session).

**Start every iteration with the watchdog** — it does the mechanical recovery
so the loop's judgement is spent on the port, not on process management:

```bash
python -m autonomy watchdog MyGame
```

It checks health, dismisses blocking error dialogs, and relaunches a crashed,
hung or frozen game. Exit 0 healthy, 1 still broken, **3 crash loop** (the game
died `CRASH_LOOP_LIMIT` times in a row — stop repeating whatever precedes it,
the crash is a finding about the port). Diagnose without touching the game with
`--no-recover`.

Underneath it, `livetools health` distinguishes the failure modes that all look
identical from a screenshot:

| verdict | meaning | response |
|---------|---------|----------|
| `not-running` | no process | relaunch |
| `crashed` | crash reporter or error dialog up | read it, dismiss, relaunch |
| `no-window` | alive, no window yet | wait |
| `hung` | window ignores WM_NULL | kill and relaunch |
| `not-rendering` | frame is black/blank | **not a navigation problem** — fullscreen, dead device, or a debug view producing nothing |
| `frozen` | frames identical across the check window | kill and relaunch |
| `ok` | alive, responding, rendering | proceed |

**Exit codes across the toolset**: 0 succeeded, 1 the command failed, 3 it ran
and the answer was no (game unhealthy, frame black, nothing changed, budget
exhausted). Branch on these instead of parsing prose.

**Stop conditions** (end the loop, write the final report): success criteria
met; an action key exhausted with no alternative left; `watchdog` reports a
crash loop; or a decision is genuinely user-only (installing the Remix runtime,
buying/patching a different game build).

Static analysis runs in background `static-analyzer` subagents while the live
loop continues — never idle waiting for it.

## Phase Machine

Phases run in order; later phases loop back when verification fails.

### Phase 0 — Preflight
`verify_install.py`, `remix status` (reports all three config surfaces),
`autonomy init`, `proc keep-awake` in the background, `remix preset apply
automation`. Back up any existing rtx.conf / proxy ini.
Gate: `remix status` shows Remix markers.

### Phase 1 — Static bootstrap (background)
If `patches/<Game>/kb.h` is sparse (`grep -cE '^[@$]|^struct ' kb.h` < 50):
spawn `static-analyzer` subagents for `bootstrap.py` and
`pyghidra_backend.py analyze` (see subagent-workflow.md), then
`pyghidra_backend.py export` to seed `index.db` so later questions are
`retools.query` SQL instead of fresh scans. Also queue DX scans:
`classify_draws.py`, `find_vs_constants.py`, `find_skinning.py`,
`find_render_states.py`, `decode_vtx_decls.py`.
Don't block on results — continue to Phase 2.

### Phase 2 — Launch and see
```bash
python -m livetools proc start "<GAMEDIR>/<exe>" --wait 90
python -m livetools screenshot grab --exe <exe> --out "$(python -m autonomy shot-path MyGame boot)"
```
Gate: `screenshot grab` reports `frame: content` (exit 0). `black`/`blank`
(exit 3) means fullscreen, a crash, or a dead device — check
`remix log -d <GAMEDIR> --errors` and `rtx_comp/diagnostics.log`. Read the
image to see *what* screen it is.

### Phase 3 — Navigate into gameplay
Goal: a repeatable in-level viewpoint (menus don't exercise the 3D pipeline).

1. Screenshot, Read it, identify the current screen.
2. Send ONE input: `livetools gamectl --exe <exe> keys "..."` / `click X Y`.
3. Screenshot again; `screenshot diff a.png b.png --expect changed` confirms
   it did something (exit 3 = nothing happened, that input does nothing here).
   `--tiles 4x3` localizes *where* it changed. Read the new image to see where you landed. Dead end →
   `ESCAPE` back, try the next candidate.
4. **Record every transition that works, immediately:**
   ```bash
   python -m livetools gamectl macro-save title_to_gameplay \
       --steps "RETURN WAIT:1500 DOWN DOWN RETURN WAIT:3000" \
       --description "title -> first level, camera at spawn" \
       --macro-file patches/MyGame/macros.json
   ```
   Phases 4-7 restart the game constantly; an unrecorded path is rediscovered
   input by input every single time.
5. In-level test: 3D scene in the screenshot (`screenshot stats` shows high
   `color_count` and `edge_density`) plus `livetools dipcnt` or a 2-frame
   dx9tracer capture showing hundreds of draws.

Games needing held keys or timing use `HOLD:KEY:ms` / `WAIT:ms` tokens;
Remix's own hotkeys pass through `CHORD:` tokens.

### Phase 4 — Remix baseline
```bash
python -m livetools remix preset apply automation -d <GAMEDIR>
python -m livetools remix preset apply devmenu -d <GAMEDIR>
python -m livetools remix preset apply capture-ready -d <GAMEDIR>
python -m livetools remix log -d <GAMEDIR> --errors
```
Apply `automation` before anything else on an unattended run: it stops Remix
opening dialogs nobody will click and removes the per-frame memory readout
that otherwise makes every screenshot diff non-zero.
Restart, replay the Phase 3 macro, screenshot the normal render. Verify runtime
control end to end once: `remix menu --exe <exe>` → screenshot (menu visible?)
→ `remix menu` again to close. Cycle a debug view by conf (restart between):
preset `debug-geometry-hash` → relaunch → screenshot → preset `debug-off`.
Gate: menu opens, debug view renders, the macro reliably reaches the same
viewpoint.

**Remix settings you don't know about**: `remix options search <term>` covers
all ~1000 rtx.conf options offline — names, types, defaults and descriptions.
`remix conf set` refuses unknown keys (which the runtime would ignore silently)
and suggests the near misses.

### Phase 5 — Trace and hash stability
At the repeatable viewpoint, camera still:
```bash
python -m graphics.directx.dx9.tracer trigger --game-dir <GAMEDIR> --frames 2 --wait
python -m graphics.directx.dx9.tracer analyze <GAMEDIR>/dxtrace_frame.jsonl \
    --summary --classify-draws --hash-stability
```
The tracer is itself a `d3d9.dll` proxy — it cannot sit in the same slot as the
Remix runtime. Capture traces in a **copy of the game directory without the
Remix runtime**, or with Remix temporarily moved aside; note which in the
journal. Delegate heavy analysis passes to `static-analyzer`.

Then the live flicker test. **Measure the noise floor first** — a renderer is
never bit-identical frame to frame (dithering, temporal accumulation, upscaler
jitter), so a raw ratio means nothing without knowing what "no change" costs on
this game:

1. Debug view **off**, camera still, two screenshots 1s apart,
   `screenshot diff a.png b.png --expect unchanged` → that ratio is the floor.
2. `remix preset apply debug-geometry-hash`, restart, replay the macro, two
   screenshots 1s apart, `screenshot diff --expect unchanged` again.

**Always pass `--expect`.** The same command means opposite things in Phase 3
and Phase 5: navigation wants the screen to change, a flicker test wants it
not to. Exit 3 then means "not what you expected" in both, instead of meaning
"changed" in one and "did not change" in the other.

Hashes are unstable when the second ratio is clearly above the floor, not when
it is above a fixed constant. `--tiles 4x3` says whether the churn is HUD or
world, which decides between `rtx.uiTextures` and a geometry hash rule.

Applying `remix preset apply automation` first removes the largest artificial
source of floor noise (the per-frame memory readout Remix draws into the
image).

Fix by finding-category (details: `.claude/references/remix-compat-catalog.md`):
- up-draws / dynamic VBs / vb-churn → `remix preset apply hash-stable-anim`
- pretransformed HUD draws → tag `rtx.uiTextures` (Phase 7 tagging loop)
- programmable-vs draws → vertex capture is already on by default; the knob
  that is not is `rtx.useVertexCapturedTexcoords` (preset `vertex-capture`),
  for shader-animated UVs. If capture is genuinely insufficient, Phase 6
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

**The tagging loop** — this is how texture hashes are obtained without a human
clicking through the developer menu:

```bash
python -m livetools remix preset apply capture-ready -d <GAMEDIR>   # once
python -m livetools remix capture trigger -d <GAMEDIR> --exe <exe>  # in-level
python -m livetools remix capture assets -d <GAMEDIR>
```
`capture trigger` sends the hotkey **and waits for the stage to be written**,
so a missed capture is reported rather than assumed. `capture assets` lists
every exported texture/material/mesh hash next to the image file it identifies
— Read those files to decide which is HUD, sky, decal or lightmap, then:
```bash
python -m livetools remix conf add-hash rtx.uiTextures 0x<hash> -d <GAMEDIR>
```
Restart and screenshot to confirm the tag took. Short-lived textures (loading
screens, one-frame HUD) need `remix preset apply keep-textures` first.

Then work the symptom table in `remix-compat-catalog.md` until the normal
render looks right. For each symptom: screenshot evidence → apply the mapped
fix → restart → macro back in-level → screenshot → compare. Standard sequence:
1. Lighting present? (black scene → fallback light diagnosis pattern)
2. Sky correct? (`rtx.skyBoxTextures` / thresholds)
3. UI clean? (`rtx.uiTextures` complete)
4. Geometry culled out of reflections? (`remix preset apply anticulling-on`)
5. No double lighting (lightmaps), no ghost/flicker (`uniqueObjectDistance`),
   decals stable.
6. Axis/scale sane in debug view 11 (`rtx.zUp`, `rtx.sceneScale`).

Each fix is one iteration with before/after screenshots in the journal.

**Guard what already works.** Settings interact: a sky fix routinely breaks
lighting three iterations later, and without a reference frame that is only
found at the end with a dozen changes to bisect. Once a viewpoint looks right,
save it, and re-check after each later change:

```bash
python -m autonomy baseline MyGame --save ingame-lit --image screens/031_7_lit.png \
    --note "fallback light off, sky autodetect 2, UI tagged"
python -m autonomy baseline MyGame --check ingame-lit --image screens/044_7_after.png
```

`--check` exits 3 when the frame no longer matches, and names the grid cell
that changed most. The same mechanism answers the other question: baseline
immediately *before* a change, and if the check says `unchanged`, the setting
did not take — a misspelled option, a missed restart, or a debug view that
never engaged.

### Phase 8 — Report and persist
```bash
python -m autonomy finish MyGame --verdict success \
    --summary "reaches gameplay via macros, hashes stable, lit and textured"
python -m autonomy report MyGame --out patches/MyGame/findings.md
```
The report covers phases with their gate evidence, open and resolved issues,
and every abandoned approach (exhausted keys) so the user sees what was tried.
Also: kb.h updated with anything identified along the way, and the final
rtx.conf diffed against the Phase 0 backup.

Success criteria, verified end to end once:
1. game launches under Remix and reaches gameplay via recorded macros,
2. hash flicker test passes,
3. normal render screenshot shows a lit, correctly textured scene with clean
   UI and sky,
4. all applied settings survive a restart, verified with
   `autonomy baseline --check` after the final relaunch.

## Beyond the happy path

Tools worth reaching for when a phase stalls — all reachable from this loop:

- `livetools memwatch` — what writes this address (vertex buffer churn,
  culling flags)
- `livetools vishook` — force geometry visible without patching the binary
- `livetools dipcnt callers` — which code paths issue the draws
- `retools.asi_patcher build` — make a runtime fix permanent
- `retools.query` — SQL over `index.db` instead of re-scanning the binary
- `retools.throwmap` / `dumpinfo diagnose` — triage a crash the watchdog keeps
  hitting
- `retools.dataflow --constants` — where a magic render-state value comes from
- `retools.kb add` — write what a live trace proved back into kb.h, so the next
  decompilation reads better than the last

## Anti-patterns

- **Blind input chains** — more than one input between screenshots while
  exploring.
- **Menu-only settings** — anything durable belongs in rtx.conf via
  `remix conf`; the dev menu is for live experiments only.
- **Restarting from scratch** — `state.json` and `macros.json` exist so no
  discovery is ever repeated.
- **Claiming without evidence** — every conclusion in the journal cites a
  screenshot, log excerpt, or analyzer output.
- **Retrying an exhausted key** — exit 3 means the approach is wrong, not
  unlucky.
- **Ignoring exit codes** — a command that exits 3 answered "no"; reading its
  stdout as success is how a loop convinces itself of a render state it never
  reached.
- **Waiting idle on subagents** — the live loop always has a next step.

## References

- rtx.conf option catalog + symptom→fix playbook: `.claude/references/remix-compat-catalog.md`
- Full rtx.conf surface, offline: `python -m livetools remix options search <term>`
- livetools syntax (attach/trace/gamectl/screenshot/health/proc/remix): `/dynamic-analysis` skill, `.claude/references/tool-catalog.md`
- FFP porting: `dx9-ffp-port` skill
- Delegation rules: `.claude/rules/subagent-workflow.md`
