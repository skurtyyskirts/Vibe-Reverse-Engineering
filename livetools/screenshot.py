"""Game window screenshot capture, pixel diffing and frame classification.

Capture gives the agent eyes on the game during unattended work: grab the
window after every input or config change, decide the next step. Diff
quantifies change between two captures — verify a menu opened, detect render
flicker (unstable Remix geometry-hash debug view), or confirm a debug view
actually changed the output.

Reading the PNG with a vision model answers "what am I looking at", but an
unattended loop also needs cheap, deterministic answers to "is this capture
even valid" and "where did the picture change". `frame_stats` and
`classify_frame` provide the first (a black or flat frame means a broken
capture path, a hung game or a dead renderer — not a screen worth reasoning
about); `tiled_diff` provides the second, localizing change to a screen region
so HUD churn is distinguishable from world churn.

Capture uses PrintWindow with PW_RENDERFULLCONTENT (works for D3D9 windowed /
borderless swapchains) and falls back to a BitBlt screen copy of the window
rectangle. Exclusive-fullscreen games bypass GDI — run the game windowed or
borderless for autonomous capture.

PNG encode/decode is pure stdlib (zlib) so analysis works anywhere; only the
capture functions require Windows.

Usage (CLI):
    python -m livetools screenshot grab --exe game.exe --out shot.png
    python -m livetools screenshot grab --window "Game Title" --out shot.png
    python -m livetools screenshot diff a.png b.png
    python -m livetools screenshot diff a.png b.png --threshold 0.02 --tolerance 8
    python -m livetools screenshot diff a.png b.png --tiles 4x3
    python -m livetools screenshot stats shot.png

Usage (library):
    from livetools.screenshot import capture_window_png, diff_png, stats_png
    path = capture_window_png(hwnd, "shot.png")
    result = diff_png("a.png", "b.png")   # {"ratio": 0.031, "bbox": (...), ...}
    stats = stats_png("shot.png")         # {"verdict": "content", ...}
"""

from __future__ import annotations

import struct
import sys
import time
import zlib
from pathlib import Path

# ── PNG encode (8-bit RGB, filter 0) ───────────────────────────────────────

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode raw RGB bytes (row-major, 3 bytes/pixel) as a PNG.

    Args:
        width:  Image width in pixels.
        height: Image height in pixels.
        rgb:    Exactly width*height*3 bytes of RGB data.

    Returns:
        Complete PNG file contents.

    Raises:
        ValueError: If rgb length does not match width*height*3.
    """
    if len(rgb) != width * height * 3:
        raise ValueError(f"Expected {width * height * 3} bytes, got {len(rgb)}")
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride]
                   for y in range(height))
    return (_PNG_SIG + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 6))
            + _chunk(b"IEND", b""))


# ── PNG decode (8-bit RGB/RGBA, all standard filters, no interlace) ────────

def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG into (width, height, rgb_bytes).

    Supports 8-bit RGB and RGBA (alpha is dropped), non-interlaced — which
    covers everything encode_png and common capture tools produce.

    Returns:
        (width, height, rgb) where rgb is width*height*3 bytes.

    Raises:
        ValueError: On malformed or unsupported PNG variants.
    """
    if data[:8] != _PNG_SIG:
        raise ValueError("Not a PNG file")
    pos = 8
    width = height = 0
    channels = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise ValueError(f"Truncated PNG chunk {tag!r}")
        pos += 12 + length
        if tag == b"IHDR":
            if length != 13:
                raise ValueError("Malformed IHDR chunk")
            width, height, depth, color, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace != 0:
                raise ValueError(
                    f"Unsupported PNG (depth={depth}, color={color}, "
                    f"interlace={interlace}); need 8-bit RGB/RGBA")
            channels = 3 if color == 2 else 4
        elif tag == b"IDAT":
            idat.extend(body)
        elif tag == b"IEND":
            break
    if not width or not idat:
        raise ValueError("PNG missing IHDR or IDAT")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise ValueError(f"Corrupt PNG image data: {e}") from e
    stride = width * channels
    if len(raw) != (stride + 1) * height:
        raise ValueError("PNG scanline data has unexpected length")

    out = bytearray(stride * height)
    prev = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        ftype = raw[base]
        line = bytearray(raw[base + 1:base + 1 + stride])
        if ftype == 1:      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:    # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError(f"Unknown PNG filter type {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    if channels == 4:
        rgb = bytearray(width * height * 3)
        for px in range(width * height):
            rgb[px * 3:px * 3 + 3] = out[px * 4:px * 4 + 3]
        return width, height, bytes(rgb)
    return width, height, bytes(out)


# ── Pixel diffing ──────────────────────────────────────────────────────────

def diff_rgb(width: int, height: int, a: bytes, b: bytes,
             tolerance: int = 4) -> dict:
    """Compare two same-sized RGB buffers pixel by pixel.

    Args:
        width, height: Image dimensions (must match for both buffers).
        a, b:          RGB byte buffers of width*height*3 bytes each.
        tolerance:     Max per-channel delta still counted as "same" —
                       absorbs dithering / video compression noise.

    Returns:
        dict with:
            changed:  number of differing pixels
            total:    width*height
            ratio:    changed / total
            bbox:     (min_x, min_y, max_x, max_y) of changed region, or None
    """
    if len(a) != len(b) or len(a) != width * height * 3:
        raise ValueError("Buffer sizes do not match dimensions")
    changed = 0
    min_x = min_y = 1 << 30
    max_x = max_y = -1
    for y in range(height):
        row = y * width * 3
        for x in range(width):
            i = row + x * 3
            if (abs(a[i] - b[i]) > tolerance
                    or abs(a[i + 1] - b[i + 1]) > tolerance
                    or abs(a[i + 2] - b[i + 2]) > tolerance):
                changed += 1
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y
    total = width * height
    bbox = (min_x, min_y, max_x, max_y) if changed else None
    return {"changed": changed, "total": total,
            "ratio": changed / total if total else 0.0, "bbox": bbox}


def diff_png(path_a: str | Path, path_b: str | Path,
             tolerance: int = 4) -> dict:
    """Diff two PNG files. See diff_rgb for the result dict format.

    Raises:
        ValueError: If the images have different dimensions.
    """
    wa, ha, rgb_a = decode_png(Path(path_a).read_bytes())
    wb, hb, rgb_b = decode_png(Path(path_b).read_bytes())
    if (wa, ha) != (wb, hb):
        raise ValueError(f"Dimension mismatch: {wa}x{ha} vs {wb}x{hb}")
    result = diff_rgb(wa, ha, rgb_a, rgb_b, tolerance=tolerance)
    result["width"], result["height"] = wa, ha
    return result


def tiled_diff(width: int, height: int, a: bytes, b: bytes,
               cols: int = 4, rows: int = 3, tolerance: int = 4,
               stride: int = 2) -> dict:
    """Diff two RGB buffers per screen region instead of as one number.

    A single ratio cannot tell "the HUD is flickering" from "the world is
    flickering" — both read as a small non-zero number. Per-tile ratios can,
    which is what decides whether a Remix hash problem needs `rtx.uiTextures`
    or a geometry hash rule.

    Args:
        width, height: Image dimensions.
        a, b:          RGB buffers of width*height*3 bytes.
        cols, rows:    Tile grid; tiles at the right/bottom edge absorb the
                       remainder when dimensions do not divide evenly.
        tolerance:     Per-channel delta still counted as "same".
        stride:        Sample every Nth pixel in each axis. Ratios stay
                       comparable between tiles; cost drops by stride².

    Returns:
        dict with:
            tiles:   row-major list of {col, row, ratio, changed, sampled}
            hottest: the tile dict with the highest ratio (None if empty)
            grid:    (cols, rows)

    Raises:
        ValueError: On mismatched buffer sizes or a non-positive grid/stride.
    """
    if len(a) != len(b) or len(a) != width * height * 3:
        raise ValueError("Buffer sizes do not match dimensions")
    if cols < 1 or rows < 1 or stride < 1:
        raise ValueError("cols, rows and stride must all be >= 1")

    x_edges = [width * i // cols for i in range(cols + 1)]
    y_edges = [height * j // rows for j in range(rows + 1)]
    tiles = []
    for row in range(rows):
        for col in range(cols):
            changed = sampled = 0
            for y in range(y_edges[row], y_edges[row + 1], stride):
                base = y * width * 3
                for x in range(x_edges[col], x_edges[col + 1], stride):
                    i = base + x * 3
                    sampled += 1
                    if (abs(a[i] - b[i]) > tolerance
                            or abs(a[i + 1] - b[i + 1]) > tolerance
                            or abs(a[i + 2] - b[i + 2]) > tolerance):
                        changed += 1
            tiles.append({"col": col, "row": row, "changed": changed,
                          "sampled": sampled,
                          "ratio": changed / sampled if sampled else 0.0})
    hottest = max(tiles, key=lambda t: t["ratio"]) if tiles else None
    return {"tiles": tiles, "hottest": hottest, "grid": (cols, rows)}


# ── Frame statistics and classification ────────────────────────────────────

#: Luminance at or below this counts as black. Above 0 because lossy capture
#: paths and PW_RENDERFULLCONTENT compositing leave near-zero noise.
BLACK_LEVEL = 12

#: Adjacent-pixel luminance step that counts as an edge.
EDGE_LEVEL = 16


def frame_stats(width: int, height: int, rgb: bytes, stride: int = 4) -> dict:
    """Summarize a frame's content without decoding what it depicts.

    Args:
        width, height: Image dimensions.
        rgb:           RGB buffer of width*height*3 bytes.
        stride:        Sample every Nth pixel in each axis. Statistics are
                       stable well past stride 8 on real frames; the default
                       keeps a 1080p frame under a tenth of a second.

    Returns:
        dict with:
            luma_mean / luma_stdev: 0-255 brightness and its spread
            black_ratio / white_ratio: fraction at the extremes
            saturation_mean: 0-255 mean of (max channel - min channel)
            color_count: distinct colors quantized to 4 bits per channel
                (capped at 4096 — the whole quantized space)
            edge_density: fraction of sampled horizontal neighbours differing
                by more than EDGE_LEVEL — a detail proxy that separates flat
                menus from rendered 3D
            sampled: number of pixels examined

    Raises:
        ValueError: On a buffer size mismatch or non-positive stride.
    """
    if len(rgb) != width * height * 3:
        raise ValueError("Buffer size does not match dimensions")
    if stride < 1:
        raise ValueError("stride must be >= 1")

    total = 0
    luma_sum = luma_sq = 0
    black = white = sat_sum = 0
    edges = edge_pairs = 0
    colors: set[int] = set()

    for y in range(0, height, stride):
        base = y * width * 3
        prev_luma = -1
        for x in range(0, width, stride):
            i = base + x * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            # Integer Rec.601 luma; exact weights are irrelevant at this scale.
            luma = (r * 77 + g * 151 + b * 28) >> 8
            total += 1
            luma_sum += luma
            luma_sq += luma * luma
            if luma <= BLACK_LEVEL:
                black += 1
            elif luma >= 255 - BLACK_LEVEL:
                white += 1
            sat_sum += max(r, g, b) - min(r, g, b)
            colors.add(((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4))
            if prev_luma >= 0:
                edge_pairs += 1
                if abs(luma - prev_luma) > EDGE_LEVEL:
                    edges += 1
            prev_luma = luma

    if not total:
        raise ValueError("No pixels sampled; image is empty")
    mean = luma_sum / total
    variance = max(0.0, luma_sq / total - mean * mean)
    return {
        "width": width, "height": height, "sampled": total,
        "luma_mean": round(mean, 2),
        "luma_stdev": round(variance ** 0.5, 2),
        "black_ratio": round(black / total, 4),
        "white_ratio": round(white / total, 4),
        "saturation_mean": round(sat_sum / total, 2),
        "color_count": len(colors),
        "edge_density": round(edges / edge_pairs, 4) if edge_pairs else 0.0,
    }


def classify_frame(stats: dict) -> dict:
    """Judge whether a capture is usable, and roughly what it shows.

    The autonomous loop branches on this before spending a vision pass: a
    `black` frame means the capture path or the renderer is broken (exclusive
    fullscreen, a crashed device, a debug view that outputs nothing), and no
    amount of navigation logic will fix it.

    Args:
        stats: A `frame_stats` result.

    Returns:
        dict with:
            verdict: one of
                black   — nothing rendered; capture or renderer is broken
                blank   — a single flat colour filling the frame
                flat    — low-detail screen: loading screen, fade, solid menu
                content — a real rendered frame worth reading
            usable: False for black/blank — do not reason about the picture
            reason: which measurement drove the verdict
    """
    black_ratio = stats["black_ratio"]
    colors = stats["color_count"]
    edges = stats["edge_density"]
    stdev = stats["luma_stdev"]

    if black_ratio >= 0.995:
        verdict, reason = "black", f"{black_ratio:.1%} of pixels at black level"
    elif colors <= 2 and stdev < 2.0:
        verdict, reason = "blank", f"only {colors} distinct colours"
    elif colors < 64 and edges < 0.02:
        verdict, reason = ("flat",
                           f"{colors} colours, edge density {edges:.3f}")
    else:
        verdict, reason = ("content",
                           f"{colors} colours, edge density {edges:.3f}")
    return {"verdict": verdict, "usable": verdict not in ("black", "blank"),
            "reason": reason}


def stats_png(path: str | Path, stride: int = 4) -> dict:
    """Run `frame_stats` + `classify_frame` on a PNG file.

    Returns:
        The stats dict with `verdict`, `usable` and `reason` merged in.
    """
    width, height, rgb = decode_png(Path(path).read_bytes())
    stats = frame_stats(width, height, rgb, stride=stride)
    stats.update(classify_frame(stats))
    return stats


# ── Window capture (Windows only) ──────────────────────────────────────────

PW_CLIENTONLY        = 0x1
PW_RENDERFULLCONTENT = 0x2
SRCCOPY              = 0x00CC0020
BI_RGB               = 0
DIB_RGB_COLORS       = 0


def capture_window(hwnd: int, client_only: bool = True) -> tuple[int, int, bytes]:
    """Capture a window's pixels via PrintWindow (BitBlt fallback).

    Args:
        hwnd:        Target window handle.
        client_only: Capture only the client area (excludes title bar).

    Returns:
        (width, height, rgb_bytes)

    Raises:
        OSError: If not on Windows or every capture path fails.
    """
    if sys.platform != "win32":
        raise OSError("Window capture requires Windows")
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wt.RECT()
    if client_only:
        user32.GetClientRect(hwnd, ctypes.byref(rect))
    else:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise OSError(f"Window has empty rect ({width}x{height})")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height   # top-down rows
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS,
                                  ctypes.byref(bits), None, 0)
    if not hbmp:
        user32.ReleaseDC(hwnd, hdc_win)
        gdi32.DeleteDC(hdc_mem)
        raise OSError("CreateDIBSection failed")

    try:
        old = gdi32.SelectObject(hdc_mem, hbmp)
        flags = PW_RENDERFULLCONTENT | (PW_CLIENTONLY if client_only else 0)
        ok = user32.PrintWindow(hwnd, hdc_mem, flags)
        if not ok:
            # Screen-copy fallback: works when the window is visible on screen
            pt = wt.POINT(0, 0)
            if client_only:
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
            else:
                wrect = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(wrect))
                pt.x, pt.y = wrect.left, wrect.top
            hdc_screen = user32.GetDC(None)
            gdi32.BitBlt(hdc_mem, 0, 0, width, height,
                         hdc_screen, pt.x, pt.y, SRCCOPY)
            user32.ReleaseDC(None, hdc_screen)
        gdi32.SelectObject(hdc_mem, old)

        buf = ctypes.string_at(bits, width * height * 4)
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

    rgb = bytearray(width * height * 3)
    for px in range(width * height):
        # DIB rows are BGRX
        rgb[px * 3] = buf[px * 4 + 2]
        rgb[px * 3 + 1] = buf[px * 4 + 1]
        rgb[px * 3 + 2] = buf[px * 4]
    return width, height, bytes(rgb)


def capture_window_png(hwnd: int, out_path: str | Path,
                       client_only: bool = True) -> Path:
    """Capture a window and write it as a PNG file.

    Returns:
        The output path.
    """
    from . import gamectl
    # Capture size must match the window's real pixels, not DPI-virtualized
    # ones, or every coordinate read off the screenshot is wrong.
    gamectl.set_dpi_aware()
    width, height, rgb = capture_window(hwnd, client_only=client_only)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_png(width, height, rgb))
    return out


def default_output_path(prefix: str = "screenshot") -> Path:
    return Path(f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.png")
