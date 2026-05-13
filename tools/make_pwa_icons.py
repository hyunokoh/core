#!/usr/bin/env python3
"""Generate zkCEX PWA icons using only the standard library.

Produces:
  - homepage/icons/icon-192.png
  - homepage/icons/icon-256.png
  - homepage/icons/icon-384.png
  - homepage/icons/icon-512.png
  - homepage/icons/icon-512-maskable.png   (20% safe-area padding)
  - homepage/icons/apple-touch-icon.png    (180x180)
  - homepage/icons/favicon.svg             (vector original)
  - homepage/icons/screenshot-mobile.png   (1080x1920 placeholder)
  - homepage/icons/screenshot-desktop.png  (1920x1080 placeholder)

Hand-rolls a PNG writer (struct + zlib) so we don't need Pillow or cairosvg.
"""

from __future__ import annotations

import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "homepage", "icons")
os.makedirs(OUT, exist_ok=True)

# ---- PNG writer -----------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: str, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    """Write a true-color + alpha PNG. ``pixels`` is rows of (r,g,b,a) tuples."""
    h = len(pixels)
    w = len(pixels[0]) if h else 0
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type: None
        for r, g, b, a in row:
            raw += bytes((r & 0xFF, g & 0xFF, b & 0xFF, a & 0xFF))
    idat = zlib.compress(bytes(raw), level=6)
    with open(path, "wb") as f:
        f.write(sig)
        f.write(_chunk(b"IHDR", ihdr))
        f.write(_chunk(b"IDAT", idat))
        f.write(_chunk(b"IEND", b""))


# ---- color helpers --------------------------------------------------------


def hex_rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (int(lerp(c1[0], c2[0], t)), int(lerp(c1[1], c2[1], t)), int(lerp(c1[2], c2[2], t)))


# ---- icon renderer --------------------------------------------------------

BRAND_FROM = hex_rgb("#0b3aff")
BRAND_TO = hex_rgb("#6e8bff")
BG = hex_rgb("#0b0e16")
WHITE = (255, 255, 255)


def make_icon(size: int, *, maskable: bool = False) -> list[list[tuple[int, int, int, int]]]:
    """Render a rounded square with a brand gradient and a white triangle/check.

    For ``maskable=True``, scale the artwork into the inner 60% of the canvas
    so Android's adaptive-icon mask can't crop the logo.
    """
    pixels = [[(0, 0, 0, 0)] * size for _ in range(size)]
    # Outer artwork bounds
    if maskable:
        pad = int(size * 0.20)  # 20% safe-area
    else:
        pad = 0
    ax0, ay0 = pad, pad
    ax1, ay1 = size - pad, size - pad
    aw = ax1 - ax0
    radius = int(aw * 0.22)  # Apple-style corner radius
    r2 = radius * radius

    # Gradient direction (top-left -> bottom-right). Normal length = sqrt(2)*aw.
    # We compute t = (dx + dy) / (2*aw) which already lands in [0, 1].
    for y in range(ay0, ay1):
        ly = y - ay0
        for x in range(ax0, ax1):
            lx = x - ax0
            # Rounded-square mask (anti-aliased at the corners).
            # Distance from nearest corner if inside the corner inset, else 0.
            cx = cy = None
            if lx < radius and ly < radius:
                cx, cy = radius, radius
            elif lx >= aw - radius and ly < radius:
                cx, cy = aw - radius, radius
            elif lx < radius and ly >= aw - radius:
                cx, cy = radius, aw - radius
            elif lx >= aw - radius and ly >= aw - radius:
                cx, cy = aw - radius, aw - radius
            if cx is not None:
                dx = lx - cx
                dy = ly - cy
                d2 = dx * dx + dy * dy
                if d2 > r2:
                    # Outside the rounded corner: leave fully transparent.
                    continue
                # Simple 1px AA: fade alpha near the edge of the disc.
                d = d2**0.5
                edge_a = max(0.0, min(1.0, radius - d))  # 0..1 across last 1px
                a = int(255 * edge_a) if d > radius - 1 else 255
            else:
                a = 255
            t = (lx + ly) / (2.0 * max(1, aw))
            r, g, b = mix(BRAND_FROM, BRAND_TO, t)
            pixels[y][x] = (r, g, b, a)

    # ---- Foreground glyph: an upward chevron / play-style triangle ------
    # Center it; height ~= 50% of artwork. The shape: a right-pointing
    # triangle (matches the .brand-mark::after on the site) overlaid with a
    # small horizontal underline to read as a "zk" mark.
    cx_pix = ax0 + aw / 2.0
    cy_pix = ay0 + aw / 2.0
    tri_h = aw * 0.46
    tri_w = tri_h * 0.86
    # Slightly biased left so the apex sits on the optical center.
    apex_x = cx_pix + tri_w * 0.38
    base_x = cx_pix - tri_w * 0.48
    top_y = cy_pix - tri_h / 2.0
    bot_y = cy_pix + tri_h / 2.0
    # Rasterise the triangle by barycentric test.
    v0 = (base_x, top_y)
    v1 = (base_x, bot_y)
    v2 = (apex_x, cy_pix)

    def edge(p, a, b):
        return (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])

    bb_x0 = max(ax0, int(min(v0[0], v1[0], v2[0])))
    bb_x1 = min(ax1, int(max(v0[0], v1[0], v2[0])) + 1)
    bb_y0 = max(ay0, int(min(v0[1], v1[1], v2[1])))
    bb_y1 = min(ay1, int(max(v0[1], v1[1], v2[1])) + 1)
    for y in range(bb_y0, bb_y1):
        for x in range(bb_x0, bb_x1):
            p = (x + 0.5, y + 0.5)
            w0 = edge(p, v1, v2)
            w1 = edge(p, v2, v0)
            w2 = edge(p, v0, v1)
            if (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0):
                # Compose white over the existing pixel (which is opaque blue).
                cur = pixels[y][x]
                if cur[3] == 0:
                    continue
                pixels[y][x] = (WHITE[0], WHITE[1], WHITE[2], cur[3])

    # ---- Subtle underline bar beneath the triangle to give it more body --
    bar_y0 = int(cy_pix + tri_h / 2.0 + aw * 0.04)
    bar_y1 = bar_y0 + max(2, int(aw * 0.05))
    bar_x0 = int(cx_pix - tri_w * 0.55)
    bar_x1 = int(cx_pix + tri_w * 0.55)
    for y in range(max(ay0, bar_y0), min(ay1, bar_y1)):
        for x in range(max(ax0, bar_x0), min(ax1, bar_x1)):
            cur = pixels[y][x]
            if cur[3] == 0:
                continue
            pixels[y][x] = (WHITE[0], WHITE[1], WHITE[2], cur[3])

    return pixels


def make_screenshot(w: int, h: int, label: str) -> list[list[tuple[int, int, int, int]]]:
    """Dark, single-color screenshot placeholder with a small brand square + label."""
    pixels = [[(BG[0], BG[1], BG[2], 255)] * w for _ in range(h)]
    # Brand square in the upper-left.
    sq = min(w, h) // 8
    for y in range(sq):
        for x in range(sq):
            t = (x + y) / (2.0 * sq)
            r, g, b = mix(BRAND_FROM, BRAND_TO, t)
            pixels[40 + y][40 + x] = (r, g, b, 255)
    # Crude 8x8 label rendered in pixels — just lay down a row of white squares
    # along the y-center to read as "wordmark" without bundling a font file.
    cy = h // 2
    bar_h = max(6, h // 90)
    bar_w = w // 3
    bar_x0 = (w - bar_w) // 2
    for y in range(cy - bar_h, cy + bar_h):
        for x in range(bar_x0, bar_x0 + bar_w):
            pixels[y][x] = (60, 80, 140, 255)
    return pixels


# ---- favicon.svg (the vector original) ------------------------------------

FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' role='img' aria-label='zkCEX'>
  <defs>
    <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#0b3aff'/>
      <stop offset='100%' stop-color='#6e8bff'/>
    </linearGradient>
  </defs>
  <rect x='2' y='2' width='60' height='60' rx='14' fill='url(#g)'/>
  <polygon points='22,18 22,46 46,32' fill='#ffffff'/>
  <rect x='18' y='49' width='28' height='4' rx='1.5' fill='#ffffff'/>
</svg>
"""


def main() -> None:
    sizes = [192, 256, 384, 512]
    for s in sizes:
        path = os.path.join(OUT, f"icon-{s}.png")
        write_png(path, make_icon(s, maskable=False))
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")
    # maskable 512
    path = os.path.join(OUT, "icon-512-maskable.png")
    write_png(path, make_icon(512, maskable=True))
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")
    # apple-touch-icon 180
    path = os.path.join(OUT, "apple-touch-icon.png")
    write_png(path, make_icon(180, maskable=False))
    print(f"wrote {path} ({os.path.getsize(path)} bytes)")
    # favicon.svg
    fav = os.path.join(OUT, "favicon.svg")
    with open(fav, "w") as f:
        f.write(FAVICON_SVG)
    print(f"wrote {fav}")
    # screenshots — keep these small (1080x1920 fully-rendered is ~6MB raw,
    # 1920x1080 is ~8MB raw), so we use solid backgrounds and minimal art.
    for name, w, h in (
        ("screenshot-mobile.png", 1080, 1920),
        ("screenshot-desktop.png", 1920, 1080),
    ):
        path = os.path.join(OUT, name)
        write_png(path, make_screenshot(w, h, "zkCEX"))
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")


if __name__ == "__main__":
    main()
