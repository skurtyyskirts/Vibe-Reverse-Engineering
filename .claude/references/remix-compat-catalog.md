# RTX Remix Compatibility Catalog

Verified rtx.conf option reference and symptom→fix playbook for making DX9
games render correctly under the RTX Remix runtime (dxvk-remix). Option names
and defaults are taken from dxvk-remix's generated `RtxOptions.md`
(github.com/NVIDIAGameWorks/dxvk-remix, main branch); debug view indices from
`src/dxvk/shaders/rtx/utility/debug_view_indices.h`.

Edit options with `python -m livetools remix conf set/unset/add-hash` or
`remix preset apply` — never hand-edit rtx.conf without a backup (the tool
makes one automatically). **rtx.conf is read at game launch**: conf changes
need a restart; only the in-game menu (ALT+X) changes settings live.

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
3. `livetools screenshot diff a.png b.png` — ratio ≳ 0.01 on a static scene
   means hashes are churning; cross-check `dx9tracer analyze --hash-stability`.
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

## Texture Categories (hash sets)

All are comma-separated hash lists (`rtx.X = 0xAAAA, 0xBBBB`), default empty.
Get hashes from the Remix menu texture tabs, a USD capture, or
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
| `rtx.useVertexCapture` | bool | True | Inject into original vertex shaders to capture final positions (shader games that still set FFP matrices) |
| `rtx.useVertexCapturedNormals` | bool | True | Use input-assembler normals during vertex capture |
| `rtx.useVertexCapturedTexcoords` | bool | False | Prefer VS-output texcoords (shader-animated UVs) |
| `rtx.skyDrawcallIdThreshold` | int | 0 | First N untextured draws treated as sky |
| `rtx.skyMinZThreshold` | float | 1 | Viewport minZ ≥ this ⇒ sky |
| `rtx.skyReprojectToMainCameraSpace` | bool | False | Promote 3D skybox geometry into the main scene |
| `rtx.skyForceHDR` | bool | False | Rasterize sky in HDR format (HDR sky replacements) |
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

## Symptom → Fix Playbook

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Black screen, geometry visible in debug view 1/11 | No game lights converted | preset `fallback-light-always`, then tune `rtx.lightConversion*`; production: real lights + `fallback-light-off` |
| Scene renders but replacements/tags don't stick between runs | Unstable geometry hashes | `dx9tracer analyze --hash-stability`; preset `hash-stable-anim`; verify with debug view 277 flicker test |
| Textures flicker / wrong texture on objects | Pooled textures rewritten by game | `rtx.recomputeTextureHashOnWrite = True` (watch animated-texture side effects) |
| HUD/UI raytraced into the world or missing | UI not tagged | add HUD texture hashes to `rtx.uiTextures` (pretransformed draws in `--hash-stability` output are the candidates) |
| Sky missing / drawn as near geometry | Sky not identified | `rtx.skyBoxTextures` (or `skyDrawcallIdThreshold` for early untextured sky draws); 3D skybox: `rtx.skyReprojectToMainCameraSpace` |
| Geometry present but nothing animates/skins correctly | Shader-transformed vertices not captured | `rtx.useVertexCapture = True`; complex shaders: FFP-convert via remix-comp-proxy (`dx9-ffp-port` skill) |
| Fast objects flicker or leave ghost lighting | Object correlation distance | tune `rtx.uniqueObjectDistance` |
| Double lighting (baked + RT) | Lightmaps still applied | `rtx.lightmapTextures` / `rtx.ignoreBakedLightingTextures`; FFP lightmap-last games: `rtx.ignoreLastTextureStage = True` |
| Lights too bright/dim after conversion | Legacy intensity mismatch | `rtx.lightConversionIntensityFactor` |
| Wrong up-axis artifacts (terrain baker, atmosphere) | Y/Z mismatch | `rtx.zUp = True` |
| Lighting falloff wrong everywhere | Unit scale | `rtx.sceneScale = 1cm/game-unit` |
| Decals z-fight or float | Untagged decals | `rtx.decalTextures` (+ offset variants) |
| Effects/fullscreen quads corrupt the RT scene | Post-process draws raytraced | `rtx.ignoreTextures`, or `rtx.skipDrawCallsPostRTXInjection = True` |
