# graphics/

Graphics capture and analysis tools.

---

## `directx/dx9/tracer/`

Full-frame D3D9 API capture system — a proxy DLL that intercepts all 119 `IDirect3DDevice9` methods and records every call with arguments, backtraces, pointer-followed data (matrices, constants, shader bytecodes), and in-process shader disassembly.

### Architecture

```
Python codegen (d3d9_methods.py)
        │
        ▼
C proxy DLL (src/)  ──── deployed as d3d9.dll in game dir
        │
        ▼ (JSONL capture files)
Python analyzer (analyze.py)
```

The proxy chains to the real `d3d9` (or another wrapper) and adds near-zero overhead when not capturing. Captures are triggered externally (`tracer trigger`) rather than on every frame.

### Quick Usage

```bash
# Build the proxy DLL
cd graphics/directx/dx9/tracer/src && build.bat

# Deploy to game directory, then trigger a capture (3s countdown)
python -m graphics.directx.dx9.tracer trigger --game-dir "C:/path/to/game"

# Analyze the capture
python -m graphics.directx.dx9.tracer analyze frame_capture.jsonl --summary
python -m graphics.directx.dx9.tracer analyze frame_capture.jsonl --classify-draws
python -m graphics.directx.dx9.tracer analyze frame_capture.jsonl --shader-map
python -m graphics.directx.dx9.tracer analyze frame_capture.jsonl --const-evolution vs:c0-c15
```

### Key Analysis Options

| Option | Use case |
|--------|----------|
| `--summary` | Overview: call counts per method, frame count |
| `--draw-calls` | Every draw call with state deltas |
| `--classify-draws` | Tag draws: alpha, z-test, fullscreen-quad, shader type |
| `--shader-map` | Disassemble all shaders with CTAB register names |
| `--const-evolution RANGE` | Track VS/PS constant registers across draws (e.g. `vs:c0-c3`) |
| `--diff-frames A B` | Compare two captured frames |
| `--vtx-formats` | Group draws by vertex declaration |
| `--matrix-flow` | Track matrix uploads per `SetTransform` / `SetVertexShaderConstantF` |
| `--render-passes` | Group draws by render target, classify pass types |
| `--state-snapshot DRAW#` | Complete device state at a specific draw index |

### proxy.ini Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `CaptureFrames=N` | 2 | Number of frames to record per trigger |
| `CaptureInit=1` | 0 | Also capture boot-time calls (shader creation, device init) |
| `Chain.DLL=<name>` | — | Chain to another d3d9 wrapper (e.g. `d3d9_remix.dll`) |

**Important:** `--game-dir` must point to the directory containing the deployed proxy DLL (the directory the game exe runs from).

For full option reference, see [`docs/reference/`](../docs/reference/).
