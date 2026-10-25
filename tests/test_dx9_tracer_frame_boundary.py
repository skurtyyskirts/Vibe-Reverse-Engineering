"""Guard the Device::Present to EndScene frame-boundary fallback."""

from pathlib import Path


_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "graphics/directx/dx9/tracer/src/d3d9_trace_device.c"
)


def test_swapchain_only_capture_promotes_endscene_after_the_threshold():
    source = _SOURCE.read_text(encoding="utf-8")

    assert "#define SWAPCHAIN_ONLY_SCENES 64" in source
    assert "if (g_endScenesSincePresent < SWAPCHAIN_ONLY_SCENES)" in source
    assert "g_endScenesSincePresent++;\n        else\n            trace_on_present();" in source


def test_device_present_resets_the_endscene_fallback_counter():
    source = _SOURCE.read_text(encoding="utf-8")

    present_branch = source.index("if (slot == SLOT_Present)")
    endscene_branch = source.index("else if (slot == SLOT_EndScene)", present_branch)
    assert "g_endScenesSincePresent = 0;" in source[present_branch:endscene_branch]
