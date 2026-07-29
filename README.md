# Vibe Reverse Engineering

LLM-friendly static and dynamic analysis tools for **x86/x64 PE binaries**, designed for agentic coding tools. Point an agent at an `.exe`, describe what you want, and let it work.

No reverse engineering experience required -- just good prompting. Although some basic knowledge of programming and RE can go a long way.

## Requirements

- A supported agentic coding tool:
  - [Cursor IDE](https://cursor.sh)
  - [VSCode](https://code.visualstudio.com/) + [Copilot](https://github.com/features/copilot)
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
  - [Kiro](https://kiro.dev)
- Python 3.10+
- Visual Studio 2022+ with C++ Desktop workload (only needed to build ASI patches)

Radare2 (used by the decompiler) is bundled in `tools/` for Windows -- no separate install needed.

### Python setup

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Tools

**Static analysis** (`retools/`) works directly on PE files on disk: disassembly, decompilation, cross-references, call graphs, vtable analysis, byte pattern search, and more.

**Indexed queries** (`retools/index.py` + `retools/query.py`) cache analyzed facts (functions, names, cross-references, strings, imports) into a per-game SQLite file (`patches/<Game>/index.db`), so the agent can answer "who calls this" or "find all strings matching X" with a local SQL query instead of re-scanning the binary. Bootstrap and Ghidra analysis both feed it; Ghidra-sourced facts win over provisional ones.

**Ghidra server** (`retools/ghidra_server.py`) keeps one Ghidra program warm per game project so repeat decompilations return in well under a second instead of paying Ghidra's analysis cost on every call.

**Dynamic analysis** (`livetools/`) attaches to a running process via Frida: breakpoints, register/memory inspection, function tracing, instruction-level stepping, and live memory patching.

**Game window automation** (`livetools/gamectl.py`) sends keystrokes and mouse clicks to a game window without Frida. Uses `SendInput` with `AttachThreadInput` focus management — works with DirectInput/RawInput games that ignore `PostMessage`. Target by process exe name:

```bash
python -m livetools gamectl --exe game.exe info
python -m livetools gamectl --exe game.exe keys "DOWN DOWN RETURN"
python -m livetools gamectl --exe game.exe macro --macro-file patches/MyGame/macros.json navigate_menu
```

**Screenshots** (`livetools/screenshot.py`) capture the game window to PNG and compare captures. Beyond a changed-pixel ratio it reports whether a frame is usable at all (`black`/`blank` means the capture path or the renderer is broken, not that the game is showing something boring) and can localize change to a screen region, which separates HUD churn from world churn.

**D3D9 frame tracer** (`graphics/directx/dx9/tracer/`) captures every `IDirect3DDevice9` call with arguments, backtraces, shader bytecodes, and matrix data. Outputs JSONL for offline analysis.

**RTX Remix runtime control** (`livetools/remixctl.py`) edits `rtx.conf`, applies named presets, toggles the developer menu, reads the runtime logs, and takes USD captures. Captures are what make texture tagging scriptable: the developer menu only shows texture hashes to a person clicking through tabs, while a capture writes them to disk next to the exported image.

```bash
python -m livetools remix preset apply capture-ready -d "C:/Games/MyGame"
python -m livetools remix capture trigger -d "C:/Games/MyGame" --exe game.exe
python -m livetools remix capture assets  -d "C:/Games/MyGame"
python -m livetools remix conf add-hash rtx.uiTextures 0x1234ABCD -d "C:/Games/MyGame"
```

All ~1000 rtx.conf options ship as an offline table, so the agent can find settings nobody documented and `conf set` rejects names and values the runtime would silently ignore:

```bash
python -m livetools remix options search "ghosting"
python -m livetools remix options show rtx.uniqueObjectDistance
```

**Game health and lifecycle** (`livetools/health.py`, `livetools/procctl.py`) answer the question every unattended iteration starts with. `health` reduces the probes to one verdict — `not-running`, `crashed`, `no-window`, `hung`, `not-rendering`, `frozen`, `ok` — because those need different responses and look identical in a screenshot. `proc` stops, starts and restarts the game (every rtx.conf change costs a restart) and keeps Windows from sleeping an overnight run.

**Unattended porting runs** (`autonomy/`) hold the state an agent's context cannot: which phase the port is in and the evidence that proved it, how many times an approach has failed before it should be abandoned, open issues, a journal, and numbered screenshots.

```bash
python -m autonomy init MyGame --game-dir "C:/Games/MyGame" --exe game.exe
python -m autonomy watchdog MyGame     # relaunch/unblock the game if it needs it
python -m autonomy status MyGame       # phase, next action, attempt budgets
python -m autonomy report MyGame --out patches/MyGame/findings.md
```

Commands the loop branches on return exit codes: 0 succeeded, 1 the command failed, 3 it ran and the answer was no (game unhealthy, frame black, nothing changed, approach exhausted).

**RTX Remix FFP template** (`rtx_remix_tools/dx/dx9_ffp_template/`) is a D3D9 proxy DLL that converts shader-based games to fixed-function pipeline for RTX Remix compatibility.

## How it works

Agent instructions are **single-sourced** — edit once, every harness picks it up:

- **`AGENTS.md`** (repo root) — canonical instructions: project conventions, engineering standards, and pointers to the tool catalog. Read natively by Cursor, Kiro, Codex, and most agents. VS Code Copilot loads it via the shipped `.vscode/settings.json` (`chat.useAgentsMdFile`); Claude Code imports it from `.claude/CLAUDE.md`.
- **`.claude/`** — the maintained tree for everything deeper: skills (`.claude/skills/`), tool catalog (`.claude/references/`), workflow rules (`.claude/rules/`), and subagent definitions (`.claude/agents/`). These files are plain Markdown any agent can read by path.

Skills install themselves: Claude Code reads `.claude/skills/` natively, and AGENTS.md instructs every other agent to install the skills into its own skills directory on first use (via the [skills CLI](https://github.com/vercel-labs/skills) or a manual copy). Normally you don't need to do anything — open the repo and start working.

To install manually instead, or to consume the skills from another project:

```bash
# Inside this repo (pick your agent):
npx skills add ./.claude/skills -a cursor -y     # or -a copilot, -a kiro-cli, ...

# From anywhere else:
npx skills add Ekozmaster/Vibe-Reverse-Engineering
```

Inside the repo, point the source at `./.claude/skills` explicitly — with a bare `.` the CLI skips the current project's own agent directories and finds nothing. Installed copies live in git-ignored locations (`.agents/`, `.cursor/skills/`, `skills-lock.json`, …); the canonical, editable copies stay in `.claude/skills/`.

## Usage

Open this directory in your agentic coding tool and describe what you're after:

> Disable frustum culling in "D:/Games/MyGame/AwesomeGame.exe" -- I'm modding raytracing and need geometry to render behind the camera for reflections/mirrors.

Be descriptive about the feature or bug, the expected behavior, and your goal. The agent will plan and execute from there.

## Important

Some processes (especially games) require their window to be focused for dynamic analysis to capture data -- breakpoints won't hit and traces won't register otherwise. Follow the agent's instructions and watch what it is doing.

## License

[MIT](LICENSE)
