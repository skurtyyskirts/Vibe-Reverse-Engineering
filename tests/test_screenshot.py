import struct
import zlib

import pytest

from livetools import screenshot as ss


def _gradient_rgb(width, height):
    rgb = bytearray()
    for y in range(height):
        for x in range(width):
            rgb += bytes(((x * 7) & 0xFF, (y * 11) & 0xFF, (x * y) & 0xFF))
    return bytes(rgb)


def test_png_round_trip():
    w, h = 33, 17   # non-multiple-of-4 width exercises stride handling
    rgb = _gradient_rgb(w, h)
    assert ss.decode_png(ss.encode_png(w, h, rgb)) == (w, h, rgb)


def test_encode_rejects_wrong_length():
    with pytest.raises(ValueError):
        ss.encode_png(4, 4, b"\0" * 5)


def test_decode_rejects_non_png():
    with pytest.raises(ValueError):
        ss.decode_png(b"definitely not a png")


def test_decode_truncated_png_raises_valueerror():
    png = ss.encode_png(8, 8, _gradient_rgb(8, 8))
    with pytest.raises(ValueError):
        ss.decode_png(png[:len(png) - 20])


def test_decode_corrupt_idat_raises_valueerror():
    w, h = 4, 4
    raw_len = (w * 3 + 1) * h
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + ss._chunk(b"IHDR", ihdr)
           + ss._chunk(b"IDAT", b"\xde\xad" * (raw_len // 2))
           + ss._chunk(b"IEND", b""))
    with pytest.raises(ValueError):
        ss.decode_png(png)


def _encode_with_filters(width, height, rgb, filter_type):
    """Build a valid RGB PNG whose every scanline uses filter_type."""
    stride = width * 3
    prev = bytearray(stride)
    raw = bytearray()
    for y in range(height):
        line = bytearray(rgb[y * stride:(y + 1) * stride])
        filtered = bytearray(line)
        for i in range(stride):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            if filter_type == 1:
                filtered[i] = (line[i] - a) & 0xFF
            elif filter_type == 2:
                filtered[i] = (line[i] - b) & 0xFF
            elif filter_type == 3:
                filtered[i] = (line[i] - ((a + b) >> 1)) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                filtered[i] = (line[i] - pred) & 0xFF
        raw += bytes([filter_type]) + filtered
        prev = line
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + ss._chunk(b"IHDR", ihdr)
            + ss._chunk(b"IDAT", zlib.compress(bytes(raw)))
            + ss._chunk(b"IEND", b""))


@pytest.mark.parametrize("filter_type", [1, 2, 3, 4])
def test_decode_supports_standard_filters(filter_type):
    w, h = 16, 8
    rgb = _gradient_rgb(w, h)
    png = _encode_with_filters(w, h, rgb, filter_type)
    assert ss.decode_png(png) == (w, h, rgb)


def test_decode_rgba_drops_alpha():
    w, h = 5, 3
    rgb = _gradient_rgb(w, h)
    rgba = bytearray()
    for px in range(w * h):
        rgba += rgb[px * 3:px * 3 + 3] + b"\x80"
    stride = w * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride])
                   for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + ss._chunk(b"IHDR", ihdr)
           + ss._chunk(b"IDAT", zlib.compress(raw)) + ss._chunk(b"IEND", b""))
    assert ss.decode_png(png) == (w, h, rgb)


def test_diff_identical_is_zero():
    w, h = 8, 8
    rgb = _gradient_rgb(w, h)
    r = ss.diff_rgb(w, h, rgb, rgb)
    assert r["changed"] == 0 and r["ratio"] == 0.0 and r["bbox"] is None


def test_diff_counts_changes_and_bbox():
    w, h = 10, 10
    a = bytes(w * h * 3)
    b = bytearray(a)
    for x, y in ((2, 3), (5, 7)):
        b[(y * w + x) * 3] = 200
    r = ss.diff_rgb(w, h, a, bytes(b))
    assert r["changed"] == 2
    assert r["ratio"] == pytest.approx(2 / 100)
    assert r["bbox"] == (2, 3, 5, 7)


def test_diff_tolerance_absorbs_noise():
    w, h = 4, 4
    a = bytes([100] * (w * h * 3))
    b = bytes([104] * (w * h * 3))
    assert ss.diff_rgb(w, h, a, b, tolerance=4)["changed"] == 0
    assert ss.diff_rgb(w, h, a, b, tolerance=3)["changed"] == w * h


def test_diff_png_files(tmp_path):
    w, h = 6, 6
    rgb_a = _gradient_rgb(w, h)
    rgb_b = bytearray(rgb_a)
    rgb_b[0] = (rgb_b[0] + 100) % 256
    pa, pb = tmp_path / "a.png", tmp_path / "b.png"
    pa.write_bytes(ss.encode_png(w, h, rgb_a))
    pb.write_bytes(ss.encode_png(w, h, bytes(rgb_b)))
    r = ss.diff_png(pa, pb)
    assert r["changed"] == 1 and r["width"] == w


def test_diff_png_dimension_mismatch(tmp_path):
    pa, pb = tmp_path / "a.png", tmp_path / "b.png"
    pa.write_bytes(ss.encode_png(2, 2, bytes(12)))
    pb.write_bytes(ss.encode_png(3, 2, bytes(18)))
    with pytest.raises(ValueError):
        ss.diff_png(pa, pb)
