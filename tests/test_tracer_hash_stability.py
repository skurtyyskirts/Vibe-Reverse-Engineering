from graphics.directx.dx9.tracer.analyze import do_hash_stability
from graphics.directx.dx9.tracer.d3d9_methods import SLOT


def _rec(seq, method, frame, args=None, **extra):
    rec = {"seq": seq, "frame": frame, "slot": SLOT[method],
           "method": method, "args": args or {}, "backtrace": []}
    rec.update(extra)
    return rec


def _static_scene_frame(frame, seq0, vb="0x0BAD0002"):
    return [
        _rec(seq0, "SetFVF", frame, {"FVF": "0x00000112"}),
        _rec(seq0 + 1, "SetStreamSource", frame,
             {"StreamNumber": 0, "pStreamData": vb, "Stride": 32}),
        _rec(seq0 + 2, "SetTexture", frame,
             {"Stage": 0, "pTexture": "0x11110001"}),
        _rec(seq0 + 3, "DrawIndexedPrimitive", frame,
             {"PrimitiveType": 4, "NumVertices": 100, "PrimitiveCount": 50}),
    ]


def test_clean_static_scene_reports_no_risks(capsys):
    records = _static_scene_frame(0, 0) + _static_scene_frame(1, 10)
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "No API-level risk factors found" in out
    assert "[up-draw]" not in out
    assert "[frame-flicker]" not in out


def test_up_draw_and_programmable_vs_flagged(capsys):
    records = [
        _rec(0, "SetVertexShader", 0, {"pShader": "0x22220001"}),
        _rec(1, "DrawPrimitiveUP", 0,
             {"PrimitiveType": 4, "PrimitiveCount": 2,
              "VertexStreamZeroStride": 24}),
    ]
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "[up-draw] 1 draw(s)" in out
    assert "[programmable-vs] 1 draw(s)" in out
    assert "hash-stable-anim" in out
    assert "rtx.useVertexCapture" in out


def test_dynamic_vb_flagged_via_created_handle(capsys):
    records = [
        _rec(0, "CreateVertexBuffer", -1,
             {"Length": 4096, "Usage": "0x00000200", "Pool": 0},
             created_handle="0x0BAD0001"),
    ] + _static_scene_frame(0, 1, vb="0x0BAD0001")
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "1 D3DUSAGE_DYNAMIC buffer(s)" in out
    assert "[dynamic-vb] 1 draw(s)" in out


def test_pretransformed_fvf_flagged(capsys):
    records = [
        _rec(0, "SetFVF", 0, {"FVF": "0x00000144"}),   # XYZRHW | TEX1
        _rec(1, "DrawPrimitive", 0,
             {"PrimitiveType": 4, "PrimitiveCount": 2}),
    ]
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "[pretransformed] 1 draw(s)" in out
    assert "rtx.uiTextures" in out


def test_cross_frame_flicker_detected(capsys):
    records = _static_scene_frame(0, 0) + _static_scene_frame(1, 10)
    records.append(_rec(20, "DrawIndexedPrimitive", 0,
                        {"PrimitiveType": 4, "NumVertices": 10,
                         "PrimitiveCount": 4}))
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "[frame-flicker] 1 signature(s)" in out


def test_vb_churn_detected(capsys):
    records = (_static_scene_frame(0, 0, vb="0x0BAD0002")
               + _static_scene_frame(1, 10, vb="0x0BAD0003"))
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "[vb-churn] 1 signature(s)" in out


def test_single_frame_capture_notes_missing_cross_frame_diff(capsys):
    records = _static_scene_frame(0, 0)
    do_hash_stability(records)
    out = capsys.readouterr().out
    assert "capture 2+ frames" in out
