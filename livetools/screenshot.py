"""Game window screenshot capture and pixel diffing.

Capture gives the agent eyes on the game during unattended work: grab the
window after every input or config change, Read the PNG, decide the next step.
Diff quantifies change between two captures — verify a menu opened, detect
render flicker (unstable Remix geometry-hash debug view), or confirm a debug
view actually changed the output.

Capture uses PrintWindow with PW_RENDERFULLCONTENT (works for D3D9 windowed /
borderless swapchains) and falls back to a BitBlt screen copy of the window
rectangle. Exclusive-fullscreen games bypass GDI — run the game windowed or
borderless for autonomous capture.

PNG encode/decode is pure stdlib (zlib) so diffing works anywhere; only the
capture functions require Windows.

Usage (CLI):
    python -m livetools screenshot grab --exe game.exe --out shot.png
    python -m livetools screenshot grab --window "Game Title" --out shot.png
    python -m livetools screenshot diff a.png b.png
    python -m livetools screenshot diff a.png b.png --threshold 0.02 --tolerance 8

Usage (library):
    from livetools.screenshot import capture_window_png, diff_png
    path = capture_window_png(hwnd, "shot.png")
    result = diff_png("a.png", "b.png")   # {"ratio": 0.031, "bbox": (...), ...}
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
        pos += 12 + length
        if tag == b"IHDR":
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

    raw = zlib.decompress(bytes(idat))
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
    width, height, rgb = capture_window(hwnd, client_only=client_only)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_png(width, height, rgb))
    return out


def default_output_path(prefix: str = "screenshot") -> Path:
    return Path(f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.png")
