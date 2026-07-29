# RTX Remix Compatibility Catalog

Verified rtx.conf option reference and symptom→fix playbook for making DX9
games render correctly under the RTX Remix runtime (dxvk-remix). Option names
and defaults are taken from dxvk-remix's generated `RtxOptions.md`
(github.com/NVIDIAGameWorks/dxvk-remix, main branch); debug view indices from
`src/dxvk/shaders/rtx/utility/debug_view_indices.h`.

Edit options with `python -m livetools remix conf set/unset/add-hash` or
`remix preset apply` — never hand-edit rtx.conf without a backup (the tool
makes one automatically). **rtx.conf is read at game launch**: conf changes
need a restart (`livetools proc restart <exe_path>`); only the in-game menu
(ALT+X) changes settings live.

**This file is a playbook, not the full option list.** dxvk-remix exposes ~1000
options; the ones below are the ones that come up while making a game render.
For anything else, search the complete table offline:

```bash
python -m livetools remix options search terrain
python -m livetools remix options show rtx.uniqueObjectDistance
python -m livetools remix options sync          # refresh after a runtime upgrade
```

`remix conf set` refuses option names and values the table says are wrong —
the runtime ignores both silently, so a typo otherwise costs a restart to find.

## Config Surfaces

A Remix install reads three key=value files, and the settings that decide
whether an unattended run can see the game or open the Remix UI are **not** in
rtx.conf. All three are editable the same way:

```bash
python -m livetools remix status -d <GAMEDIR>          # which files exist, option counts
python -m livetools remix conf set --surface dxvk d3d9.enableDialogMode True -d <GAMEDIR>
python -m livetools remix conf get --surface bridge -d <GAMEDIR>
```

| Surface | File | Owns |
|---------|------|------|
| `rtx` | `rtx.conf` | dxvk-remix renderer options (everything else in this document) |
| `dxvk` | `dxvk.conf` | the D3D9 layer — exclusive fullscreen, frame pacing, shader model |
| `bridge` | `bridge.conf` (or `.trex/bridge.conf`) | the 32-bit bridge — forced windowed, DirectInput forwarding, log level |

| Option | Surface | Default | Meaning |
|--------|---------|---------|---------|
| `d3d9.enableDialogMode` | dxvk | False | **Disables** exclusive fullscreen — the fix for black GDI screenshots |
| `d3d9.maxFrameRate` | dxvk | 0 | Cap frame rate (steadier captures) |
| `d3d9.maxFrameLatency` | dxvk | 0 | Frames of queued latency |
| `client.forceWindowed` | bridge | False | Force windowed even if the game asks for fullscreen |
| `client.DirectInput.forward.keyboardPolicy` | bridge | 2 | 0 never, 1 UI inactive, 2 UI active, 3 always. **Set 3 when ALT+X does nothing** — the game is swallowing all key input |
| `client.DirectInput.forward.mousePolicy` | bridge | 2 | Same scale; lower it if the Remix UI shows two cursors |
| `client.DirectInput.disableExclusiveInput` | bridge | False | Release DirectInput exclusivity in fullscreen |
| `logLevel` | bridge | Info | `Debug` when a launch fails with nothing useful in the d3d9 log |

Presets: `windowed-capture` (black screenshots), `menu-input-force` (ALT+X does
nothing), `verbose-logs` (silent launch failure).

## Unattended Runs (apply first)

`remix preset apply automation` — Remix's own settings for automation-driven
execution. Without these an unattended run gets stuck behind a dialog nobody
is there to click, and every screenshot diff is non-zero because the memory
readout in the corner changes each frame.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.automation.disableBlockingDialogBoxes` | bool | False | Suppress popups that wait for user interaction |
| `rtx.automation.disableDisplayMemoryStatistics` | bool | False | Keep per-frame memory stats out of the image (makes screenshot diffs deterministic) |
| `rtx.automation.suppressAssetLoadingErrors` | bool | False | Don't stop on asset load failures |
| `rtx.automation.enableTestTrace` | bool | False | Emit the test trace used by Remix's own automation |

## Menu and UI

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.remixMenuKeyBinds` | keys | `ALT, X` | Hotkey that opens the Remix developer menu |
| `rtx.showUI` | int | 0 | Menu state at startup: 0 hidden, 1 Simple, 2 Advanced, 3 First-Use Guide |
| `rtx.defaultToAdvancedUI` | bool | False | ALT+X opens the Advanced UI directly |
| `rtx.showUICursor` | bool | True | Show ImGui cursor while menu is open (toggle: ALT+Delete) |
| `rtx.captureHotKey` | keys | `CTRL, SHFT, Q` | Trigger a USD capture without opening the menu |
| `rtx.captureInstances` | bool | True | Capture an instanced scene snapshot to the USD stage |
| `rtx.captureShowMenuOnHotkey` | bool | True | Capture hotkey opens the capture menu instead of capturing immediately |

Automation notes:
- `livetools remix menu --exe game.exe` sends the ALT+X chord (game must be
  windowed/borderless for follow-up screenshots).
- Prefer rtx.conf + restart over menu navigation for anything durable — it is
  deterministic and needs no UI interaction. Use the menu only for live
  experiments, and screenshot after every toggle to verify.

## Debug Views

`rtx.debugView.debugViewIdx` selects the view; `0` disables (index values from
`debug_view_indices.h`). `rtx.debugView.overlayOnTopOfRenderOutput` (bool,
False) overlays the view on the normal image.

| Idx | View | Compatibility use |
|-----|------|-------------------|
| 0 | disabled | normal rendering |
| 1 | primitive index | draw call coverage |
| 11 | position | world-space sanity (zUp/sceneScale wrong ⇒ garbage) |
| 12 | texcoords | UV correctness after FFP conversion |
| 15 / 16 / 17 | geometry / shading / virtual shading normal | normal orientation problems (inside-out lighting) |
| 18 | vertex color | vertex color pipeline |
| 23 / 32 | albedo / raw albedo | texture binding correctness |
| 41 | white noise | GPU pipeline aliveness |
| 180 / 181 | is-emissive / is-particle | category tagging verification |
| 276 | primitive index hash | per-primitive hash view |
| 277 | **geometry hash** | **hash stability: colors geometry by hash — flicker on static geometry across frames = unstable hashes** |

The geometry-hash flicker test (fully scriptable):
1. `livetools remix preset apply debug-geometry-hash -d <gamedir>` and restart the game.
2. Hold the camera still; `livetools screenshot grab` twice a second apart.
3. `livetools screenshot diff a.png b.png --expect unchanged` — measure the
   noise floor with the debug view off first; a renderer is never bit-identical
   frame to frame, so a fixed threshold means nothing on its own. Cross-check
   `dx9tracer analyze --hash-stability`.
4. `livetools remix preset apply debug-off -d <gamedir>` when done.

## Fallback Light

The fallback light keeps a scene visible when a game's lights don't convert.
It follows the camera. Primarily a debugging aid — ship real lights instead.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.fallbackLightMode` | int | 1 | 0 Never, 1 only when no lights convert, 2 Always |
| `rtx.fallbackLightType` | int | 0 | 0 Distant (directional), 1 Sphere (uses radius/position offset) |
| `rtx.fallbackLightRadiance` | float3 | `1.6, 1.8, 2` | RGB radiance (raise for dark scenes) |
| `rtx.fallbackLightDirection` | float3 | `-0.2, -1, 0.4` | Direction (Distant type) |
| `rtx.fallbackLightAngle` | float | 5 | Angular size in degrees (Distant type) |
| `rtx.fallbackLightRadius` | float | 5 | Sphere radius (Sphere type) |
| `rtx.fallbackLightPositionOffset` | float3 | `0, 0, 0` | Offset from camera (Sphere type) |
| `rtx.fallbackLightConeAngle` / `ConeSoftness` / `FocusExponent` | float | 25 / 0.1 / 2 | Shaping (Sphere type) |
| `rtx.enableFallbackLightShaping` | bool | False | Enable cone/focus shaping |
| `rtx.enableFallbackLightViewPrimaryAxis` | bool | False | Shape along camera view axis |

Diagnosis pattern: black screen but geometry visible in debug views ⇒ apply
preset `fallback-light-always` (or `fallback-light-bright`); if the scene
appears, the problem is light conversion — tune `rtx.lightConversion*` or tag
`rtx.ignoreLights`, then return `rtx.fallbackLightMode` to 0/1.

## Geometry Hashing (hash stability)

Remix identifies assets by hashing draw geometry. Unstable hashes break asset
replacement, light anchoring, and texture tagging.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.geometryGenerationHashRuleString` | string | `positions,indices,texcoords,geometrydescriptor,vertexlayout,vertexshader` | Components hashed for runtime geometry identity |
| `rtx.geometryAssetHashRuleString` | string | `positions,indices,geometrydescriptor` | Components hashed for replacement/capture asset identity |
| `rtx.useObsoleteHashOnTextureUpload` | bool | False | Legacy XXH64 texture hash (only for old projects' hash compatibility) |
| `rtx.recomputeTextureHashOnWrite` | bool | False | Re-hash textures the game writes into (pooled/shuffled textures showing wrong images) |
| `rtx.logLegacyHashReplacementMatches` | bool | False | Log when legacy hashes match replacements |
| `rtx.hashCollisionDetection.enable` | bool | False | Detect distinct geometry mapping to one hash |
| `rtx.uniqueObjectDistance` | float | 300 | Game units an object may move per frame and stay "the same object" (too low ⇒ fast objects flicker; too high ⇒ repeated objects flicker) |
| `rtx.dumpAllInstancesOnFrame` | int | -1 | REMIX_DEVELOPMENT builds: dump all instances to log on frame N |

Rule tokens: `positions`, `indices`, `texcoords`, `geometrydescriptor`,
`vertexlayout`, `vertexshader`.

Choosing a generation rule:
- **Default** works when vertex data is static GPU-side.
- **CPU-animated / skinned / per-frame-rewritten vertex data** (dx9tracer
  `--hash-stability` flags up-draws, dynamic VBs, vb-churn): drop the
  per-frame-varying tokens — `indices,geometrydescriptor,vertexlayout,vertexshader`
  (preset `hash-stable-anim`). Tradeoff: meshes sharing index buffer + layout
  + shader may collide; verify with debug view 277 + `rtx.hashCollisionDetection.enable`.
- Keep `rtx.geometryAssetHashRuleString` at default unless captures show
  mismatched replacement anchoring — changing it invalidates existing mod
  captures/replacements.

## Getting Texture Hashes Without a Human (USD captures)

Every hash-set option below needs a texture hash. The developer menu shows
them to a person clicking through texture tabs; a capture writes them to disk
where a script can read them, each next to the exported image it identifies.

```bash
python -m livetools remix preset apply capture-ready -d <GAMEDIR>   # then restart
python -m livetools remix capture trigger -d <GAMEDIR> --exe game.exe
python -m livetools remix capture assets -d <GAMEDIR>
python -m livetools remix conf add-hash rtx.uiTextures 0x<hash> -d <GAMEDIR>
```

`capture trigger` waits for the stage to be written, so a capture that did not
happen is reported instead of assumed. `capture assets` lists texture, material,
mesh and thumbnail hashes with their files — open the images to decide which is
HUD, sky, decal or lightmap. Textures that live for one frame (loading screens,
transient HUD) need `remix preset apply keep-textures` first, or they are gone
before the capture runs.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.captureHotKey` | keys | `CTRL, SHFT, Q` | Capture hotkey |
| `rtx.captureShowMenuOnHotkey` | bool | True | **Set False** or the hotkey opens a menu instead of capturing |
| `rtx.captureInstances` | bool | True | Capture the instanced scene, not just assets |
| `rtx.captureMaxFrames` | int | 1 | Frames per capture |
| `rtx.captureEnableMultiframe` | bool | False | Capture animation across frames |
| `rtx.captureFramesPerSecond` | int | 24 | Multiframe capture rate |
| `rtx.captureOverwriteExistingCapture` | bool | False | Reuse one capture name instead of accumulating files |
| `rtx.keepTexturesForTagging` | bool | False | Keep every texture resident so short-lived ones can be tagged (costs VRAM) |

## Texture Categories (hash sets)

All are comma-separated hash lists (`rtx.X = 0xAAAA, 0xBBBB`), default empty.
Get hashes from a USD capture (above), the Remix menu texture tabs, or
`rtx.logLegacyHashReplacementMatches`. Edit with
`livetools remix conf add-hash/remove-hash`.

| Option | Tag draws whose textures are… |
|--------|-------------------------------|
| `rtx.uiTextures` | screen-space UI/HUD (rasterized, not raytraced; also sets the RTX-injection point) |
| `rtx.worldSpaceUiTextures` / `rtx.worldSpaceUiBackgroundTextures` | in-world UI panels / their backgrounds |
| `rtx.skyBoxTextures` | sky (drawn as environment, no parallax) |
| `rtx.skyBoxGeometries` | sky by *geometry* hash (uses asset hash rule) |
| `rtx.ignoreTextures` | dropped entirely (effects that break RT) |
| `rtx.ignoreLights` | lights to drop from conversion |
| `rtx.lightmapTextures` | baked lightmaps to strip (light comes from RT instead) |
| `rtx.ignoreBakedLightingTextures` | baked lighting to neutralize |
| `rtx.decalTextures` / `rtx.dynamicDecalTextures` / `rtx.singleOffsetDecalTextures` / `rtx.nonOffsetDecalTextures` | decals (get proper offset handling) |
| `rtx.terrainTextures` | terrain layers for the terrain baker |
| `rtx.animatedWaterTextures` | water planes to animate via layered water material |
| `rtx.particleTextures` / `rtx.particleEmitterTextures` / `rtx.beamTextures` | particles / emitters / beams |
| `rtx.playerModelTextures` / `rtx.playerModelBodyTextures` | first-person player model handling |
| `rtx.hideInstanceTextures` | hide these instances |
| `rtx.ignoreAlphaOnTextures` / `rtx.ignoreTransparencyLayerTextures` | alpha-channel / transparency-layer suppression |
| `rtx.opacityMicromapIgnoreTextures` | exclude from opacity micromaps |
| `rtx.hairCardTextures` | hair cards (mip bias via `rtx.hairCardMipBias`) |
| `rtx.smoothNormalsTextures` | force smoothed normals |
| `rtx.raytracedRenderTargetTextures` | render-target textures to raytrace |
| `rtx.lightConverter` | textured screens etc. converted to emitters |
| `rtx.antiCulling.antiCullingTextures` | exempt from anti-culling |
| `rtx.postfx.motionBlurMaskOutTextures` | mask out of motion blur |

## Scene / Rendering Correctness

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.zUp` | bool | False | Z is world-up (affects terrain baker, player model, atmosphere) |
| `rtx.sceneScale` | float | 1 | 1cm-per-game-unit ratio; wrong values break light falloff and volumetrics |
| `rtx.useVertexCapture` | bool | **True** | Inject into original vertex shaders to capture final positions. Already on — "set this to True" is not a fix |
| `rtx.useVertexCapturedNormals` | bool | **True** | Use input-assembler normals during vertex capture. Also already on |
| `rtx.useVertexCapturedTexcoords` | bool | False | Prefer VS-output texcoords (shader-animated UVs) |
| `rtx.skyAutoDetect` | int | 0 | Tag sky by camera heuristic: 0 none, 1 CameraPosition, 2 CameraPosition+DepthFlags. **Needs no texture hashes** — try this before `rtx.skyBoxTextures` |
| `rtx.skyAutoDetectUniqueCameraDistance` | float | 1 | Distance threshold separating a sky camera from the main camera |
| `rtx.skyForceAutoDetectedToReproject` | bool | False | Reproject auto-detected sky into main camera space |
| `rtx.skyDrawcallIdThreshold` | int | 0 | First N untextured draws treated as sky |
| `rtx.skyMinZThreshold` | float | 1 | Viewport minZ ≥ this ⇒ sky |
| `rtx.skyReprojectToMainCameraSpace` | bool | False | Promote 3D skybox geometry into the main scene |
| `rtx.skyForceHDR` | bool | False | Rasterize sky in HDR format (HDR sky replacements) |
| `rtx.skyBrightness` | float | 1 | Scale sky contribution |
| `rtx.ignoreGameDirectionalLights` / `PointLights` / `SpotLights` | bool | False | Drop that class of game lights (toolkit lights unaffected) |
| `rtx.lightConversionIntensityFactor` | float | 1 | Scale converted legacy light intensity |
| `rtx.lightConversionDistantLightFixedIntensity` | float | 1 | W/sr for converted distant lights |
| `rtx.ignoreLastTextureStage` | bool | False | Drop the last FFP texture stage (games binding lightmaps last) |
| `rtx.skipDrawCallsPostRTXInjection` | bool | False | Ignore draws recorded after RTX injection |
| `rtx.forceCutoutAlpha` | float | 0.5 | Alpha-test value forced on cutout-tagged textures |
| `rtx.keepTexturesForTagging` | bool | False | Keep all textures in VRAM to tag short-lived ones (loading screens) |

## Logs

dxvk-remix writes `<ExeName>_d3d9.log` (and `_dxvk.log`) in the game
directory; the 32→64-bit bridge writes `NvRemixBridge*.log`. Read with
`livetools remix log -d <gamedir> [--errors]`. Look for: unsupported D3D
usage, shader-capture failures, texture format warnings, option parse errors
(a typo in rtx.conf is silently ignored otherwise).

## Anti-Culling

Games frustum-cull geometry the ray tracer still needs: an object behind the
camera casts no reflection or shadow once the game stops submitting it. This is
the single most common reason a Remix scene looks right head-on and wrong in a
mirror. `remix preset apply anticulling-on`.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.antiCulling.object.enable` | bool | False | Keep game-culled objects in the RT scene |
| `rtx.antiCulling.object.numObjectsToKeep` | int | 10000 | Cap on retained objects (memory vs coverage) |
| `rtx.antiCulling.object.fovScale` | float | 1 | Widen the retention frustum |
| `rtx.antiCulling.object.farPlaneScale` | float | 10 | Extend retention distance |
| `rtx.antiCulling.object.enableInfinityFarFrustum` | bool | False | Never drop by distance |
| `rtx.antiCulling.object.enableHighPrecisionAntiCulling` | bool | True | Higher-precision retention test |
| `rtx.antiCulling.object.hashInstanceWithBoundingBoxHash` | bool | True | Include bbox in instance identity |
| `rtx.antiCulling.light.enable` | bool | False | Keep game-culled lights |
| `rtx.antiCulling.light.numFramesToExtendLightLifetime` | int | 1000 | How long a culled light survives |
| `rtx.antiCulling.antiCullingTextures` | hash set | — | Textures exempt from anti-culling |

## Particles, Denoising and Upscaling

Rarely the first fix, but they explain "it renders but looks wrong". Search the
full table (`remix options search particles`) for the rest of each family.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `rtx.particles.enable` | bool | True | Particle system handling |
| `rtx.particles.enableDiscontinuityGuard` | bool | False | Kill one-frame particle ghost trails when an emitter teleports |
| `rtx.enableRayReconstruction` | bool | True | DLSS Ray Reconstruction |
| `rtx.denoiserMode` / `rtx.denoiserIndirectMode` | int | 14 | Denoiser selection |
| `rtx.dlfg.enable` | bool | True | Frame generation |
| `rtx.dlssPreset` | int | 1 | DLSS preset |

Turning upscaling and frame generation off makes screenshot comparisons
deterministic; leave them on for the final look check, not for A/B tests.

## Symptom → Fix Playbook

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Black screen, geometry visible in debug view 1/11 | No game lights converted | preset `fallback-light-always`, then tune `rtx.lightConversion*`; production: real lights + `fallback-light-off` |
| Scene renders but replacements/tags don't stick between runs | Unstable geometry hashes | `dx9tracer analyze --hash-stability`; preset `hash-stable-anim`; verify with debug view 277 flicker test |
| Textures flicker / wrong texture on objects | Pooled textures rewritten by game | `rtx.recomputeTextureHashOnWrite = True` (watch animated-texture side effects) |
| HUD/UI raytraced into the world or missing | UI not tagged | add HUD texture hashes to `rtx.uiTextures` (pretransformed draws in `--hash-stability` output are the candidates) |
| Sky missing / drawn as near geometry | Sky not identified | `remix preset apply sky-autodetect` first (no hashes needed); then `rtx.skyBoxTextures` from a capture, or `skyDrawcallIdThreshold` for early untextured sky draws; 3D skybox: `rtx.skyReprojectToMainCameraSpace` |
| Objects missing from reflections/shadows but visible head-on | Game culled them | `remix preset apply anticulling-on`; widen with `antiCulling.object.fovScale` / `farPlaneScale` |
| Screenshot diffs never settle on a static scene | Memory readout / upscaler jitter in the image | `remix preset apply automation`; disable frame generation for A/B tests |
| A setting appears to do nothing | Option name or value silently rejected | `remix conf set` validates both; re-check with `remix conf get` and `remix log --errors` |
| Geometry present but nothing animates/skins correctly | Shader-transformed vertices not captured | Vertex capture is on by default; the non-default knob is `rtx.useVertexCapturedTexcoords` (preset `vertex-capture`) for shader-animated UVs. Complex shaders: FFP-convert via remix-comp-proxy (`dx9-ffp-port` skill) |
| Fast objects flicker or leave ghost lighting | Object correlation distance | tune `rtx.uniqueObjectDistance` |
| Double lighting (baked + RT) | Lightmaps still applied | `rtx.lightmapTextures` / `rtx.ignoreBakedLightingTextures`; FFP lightmap-last games: `rtx.ignoreLastTextureStage = True` |
| Lights too bright/dim after conversion | Legacy intensity mismatch | `rtx.lightConversionIntensityFactor` |
| Wrong up-axis artifacts (terrain baker, atmosphere) | Y/Z mismatch | `rtx.zUp = True` |
| Lighting falloff wrong everywhere | Unit scale | `rtx.sceneScale = 1cm/game-unit` |
| Decals z-fight or float | Untagged decals | `rtx.decalTextures` (+ offset variants) |
| Effects/fullscreen quads corrupt the RT scene | Post-process draws raytraced | `rtx.ignoreTextures`, or `rtx.skipDrawCallsPostRTXInjection = True` |
