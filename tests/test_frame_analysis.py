import random

import pytest

from livetools.screenshot import (classify_frame, encode_png, frame_stats,
                                  stats_png, tiled_diff)

W, H = 64, 48


def solid(r, g, b):
    return bytes([r, g, b] * (W * H))


def noise(seed=1):
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(W * H * 3))


def verdict(rgb, width=W, height=H):
    return classify_frame(frame_stats(width, height, rgb, stride=1))["verdict"]


def test_black_frame_is_black():
    assert verdict(solid(0, 0, 0)) == "black"


def test_near_black_capture_noise_still_reads_as_black():
    # Compositing leaves a few non-zero pixels; that must not read as content.
    rgb = bytearray(solid(0, 0, 0))
    rgb[0:3] = b"\x40\x40\x40"
    assert verdict(bytes(rgb)) == "black"


def test_single_colour_fill_is_blank():
    assert verdict(solid(40, 60, 90)) == "blank"


def test_white_screen_is_blank_not_black():
    assert verdict(solid(255, 255, 255)) == "blank"


def test_low_detail_gradient_is_flat():
    rgb = bytearray()
    for y in range(H):
        for _ in range(W):
            level = 20 + (y * 2)
            rgb += bytes([level, level, level])
    assert verdict(bytes(rgb)) == "flat"


def test_detailed_frame_is_content():
    assert verdict(noise()) == "content"


def test_only_black_and_blank_are_unusable():
    assert not classify_frame(frame_stats(W, H, solid(0, 0, 0), stride=1))["usable"]
    assert not classify_frame(frame_stats(W, H, solid(9, 9, 200), stride=1))["usable"]
    assert classify_frame(frame_stats(W, H, noise(), stride=1))["usable"]


def test_stats_report_the_measurements_behind_the_verdict():
    stats = frame_stats(W, H, solid(0, 0, 0), stride=1)
    assert stats["black_ratio"] == 1.0
    assert stats["luma_mean"] == 0.0
    assert stats["color_count"] == 1
    assert stats["sampled"] == W * H


def test_saturation_separates_grey_from_colour():
    grey = frame_stats(W, H, solid(128, 128, 128), stride=1)
    colour = frame_stats(W, H, solid(255, 0, 0), stride=1)
    assert grey["saturation_mean"] == 0
    assert colour["saturation_mean"] == 255


def test_stride_does_not_change_the_verdict():
    rgb = noise()
    coarse = classify_frame(frame_stats(W, H, rgb, stride=4))
    fine = classify_frame(frame_stats(W, H, rgb, stride=1))
    assert coarse["verdict"] == fine["verdict"]
    assert coarse["reason"] != ""


def test_frame_stats_rejects_a_mismatched_buffer():
    with pytest.raises(ValueError):
        frame_stats(W, H, b"\x00" * 10)


def test_frame_stats_rejects_a_zero_stride():
    with pytest.raises(ValueError):
        frame_stats(W, H, noise(), stride=0)


def test_stats_png_round_trip(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(encode_png(W, H, solid(0, 0, 0)))
    result = stats_png(path)
    assert result["verdict"] == "black"
    assert result["usable"] is False


# ── tiled diff ────────────────────────────────────────────────────────────

def test_tiled_diff_localizes_change_to_one_cell():
    before = noise()
    after = bytearray(before)
    for y in range(H // 2, H):
        for x in range(W // 2, W):
            i = (y * W + x) * 3
            after[i] = 255 - after[i]

    grid = tiled_diff(W, H, before, bytes(after), cols=2, rows=2, stride=1)
    hot = grid["hottest"]
    assert (hot["col"], hot["row"]) == (1, 1)
    assert hot["ratio"] > 0.9
    assert all(t["ratio"] == 0 for t in grid["tiles"]
               if (t["col"], t["row"]) != (1, 1))


def test_tiled_diff_of_identical_frames_is_all_zero():
    rgb = noise()
    grid = tiled_diff(W, H, rgb, rgb, cols=3, rows=3)
    assert grid["hottest"]["ratio"] == 0.0
    assert len(grid["tiles"]) == 9


def test_tiled_diff_covers_every_pixel_when_the_grid_does_not_divide_evenly():
    grid = tiled_diff(W, H, noise(), noise(2), cols=5, rows=7, stride=1)
    assert sum(t["sampled"] for t in grid["tiles"]) == W * H


def test_tiled_diff_rejects_an_empty_grid():
    rgb = noise()
    with pytest.raises(ValueError):
        tiled_diff(W, H, rgb, rgb, cols=0, rows=2)


def test_tiled_diff_rejects_mismatched_buffers():
    with pytest.raises(ValueError):
        tiled_diff(W, H, noise(), b"\x00" * 9)
