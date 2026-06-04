"""Rasterized brand assets served to the Meta Ray-Ban Display lens.

The DAT SDK's `Image(uri:)` only accepts HTTP(S) URLs, and its Text widget
flattens block-element chars and Unicode-12 emoji to monochrome amber. The
only way to put real color on the lens is to serve a PNG. This module
generates one from the Claude Code pixel-art pig mascot.

Pure stdlib (`zlib` + `struct`) — avoids pulling Pillow in just to draw a
6x18 sub-pixel bitmap. The PNG is generated once at import time and held
in a module-level bytes object; FastAPI hands it back on every request.
"""

from __future__ import annotations

import struct
import zlib

# Claude Code v2.1.x splash mascot (rows 1-3 of the splash). Each character
# is a Unicode block element that represents one or more quadrants of a 2x2
# sub-pixel grid. We rasterize char-by-char into a 6-row × 18-col sub-pixel
# bitmap, then scale to a target PNG size.
#
# Pinned to the exact glyphs Claude Code emits today — if upstream changes
# the mascot, regenerate this constant from a fresh `tmux capture-pane -p`
# of the first three rows of a fresh session.
CLAUDE_PIG_ART: tuple[str, ...] = (
    " ▐▛███▜▌ ",
    "▝▜█████▛▘",
    "  ▘▘ ▝▝  ",
)

# Per-quadrant fill masks for each block-element char we expect in the
# mascot. Tuple is (top-left, top-right, bottom-left, bottom-right).
# Spaces / unknown chars fall through to all-empty.
_BLOCK_QUADRANTS: dict[str, tuple[bool, bool, bool, bool]] = {
    " ": (False, False, False, False),
    "█": (True,  True,  True,  True),   # U+2588 full block
    "▀": (True,  True,  False, False),  # U+2580 upper half
    "▄": (False, False, True,  True),   # U+2584 lower half
    "▌": (True,  False, True,  False),  # U+258C left half
    "▐": (False, True,  False, True),   # U+2590 right half
    "▖": (False, False, True,  False),  # U+2596 quadrant lower-left
    "▗": (False, False, False, True),   # U+2597 quadrant lower-right
    "▘": (True,  False, False, False),  # U+2598 quadrant upper-left
    "▝": (False, True,  False, False),  # U+259D quadrant upper-right
    "▙": (True,  False, True,  True),   # U+2599 quadrant TL+BL+BR
    "▚": (True,  False, False, True),   # U+259A diagonal TL+BR
    "▛": (True,  True,  True,  False),  # U+259B quadrant TL+TR+BL
    "▜": (True,  True,  False, True),   # U+259C quadrant TL+TR+BR
    "▞": (False, True,  True,  False),  # U+259E diagonal TR+BL
    "▟": (False, True,  True,  True),   # U+259F quadrant TR+BL+BR
}

# Claude/Anthropic brand orange (matches the website accent). RGB.
CLAUDE_ORANGE = (255, 140, 60)


def _ascii_to_subpixel_grid(
    art: tuple[str, ...] = CLAUDE_PIG_ART,
) -> list[list[bool]]:
    """Expand block-element rows to a 2x-resolution boolean grid.

    Each character contributes a 2x2 cell. Output rows = 2 * len(art),
    output cols = 2 * max(len(row) for row in art). Short rows are right-
    padded with empty cells so the bitmap is rectangular.
    """
    width_chars = max((len(row) for row in art), default=0)
    grid: list[list[bool]] = []
    for line in art:
        top_row: list[bool] = []
        bot_row: list[bool] = []
        for col in range(width_chars):
            ch = line[col] if col < len(line) else " "
            tl, tr, bl, br = _BLOCK_QUADRANTS.get(ch, (False, False, False, False))
            top_row.extend([tl, tr])
            bot_row.extend([bl, br])
        grid.append(top_row)
        grid.append(bot_row)
    return grid


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Build one PNG chunk (length + type + data + CRC)."""
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def render_pig_png(
    color_rgb: tuple[int, int, int] = CLAUDE_ORANGE,
    scale: int = 8,
    pad_to_aspect: float | None = 20.0,
) -> bytes:
    """Render the mascot to an RGBA PNG, scaled `scale`× per sub-pixel.

    `pad_to_aspect` controls how wide the source canvas is relative to
    the pig itself, by padding the LEFT and RIGHT with transparent
    columns. This is the only knob we have for "make the pig look smaller
    on the lens" — the DAT SDK's `.fill` size preset stretches the source
    to fill the column width, preserving aspect ratio. A wide source
    (e.g. 20:1) shrinks the rendered image's height proportionally, AND
    shrinks the pig within it (since the pig only occupies a small
    fraction of the canvas width). Without padding, the pig fills the
    column and eats ~200 lens-px of vertical real estate. Default `20.0`
    gives a thin strip ~30 lens-px tall with the pig as a small element
    in the middle.

    Filled sub-pixels paint `color_rgb`; empty sub-pixels are transparent
    so the lens background shows through.
    """
    grid = _ascii_to_subpixel_grid()
    sub_h = len(grid)
    sub_w = len(grid[0]) if grid else 0
    pig_width = sub_w * scale
    height = sub_h * scale
    if pad_to_aspect is not None and (pad_to_aspect * height) > pig_width:
        width = int(pad_to_aspect * height)
        left_pad = (width - pig_width) // 2
    else:
        width = pig_width
        left_pad = 0
    r, g, b = color_rgb

    # Build the raw scanline stream: one filter byte (0 = None) per row,
    # then `width` RGBA pixels. Each sub-pixel expands to a `scale`×`scale`
    # block of pixels, all the same color. Transparent columns left/right
    # of the pig form the horizontal padding.
    transparent_pixel = bytes([0, 0, 0, 0])
    left_strip = transparent_pixel * left_pad
    right_strip = transparent_pixel * (width - pig_width - left_pad)
    rows = bytearray()
    for sub_y in range(sub_h):
        pig_band = bytearray()
        for sub_x in range(sub_w):
            pixel = bytes([r, g, b, 255]) if grid[sub_y][sub_x] else transparent_pixel
            pig_band.extend(pixel * scale)
        for _ in range(scale):
            rows.append(0)  # PNG filter: None
            rows.extend(left_strip)
            rows.extend(pig_band)
            rows.extend(right_strip)

    # PNG signature + IHDR + IDAT + IEND
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        8,    # bit depth
        6,    # color type: truecolor + alpha (RGBA)
        0, 0, 0,  # compression / filter / interlace = default
    )
    idat = zlib.compress(bytes(rows), 9)
    return (
        signature
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


# Generated once at import. The mascot doesn't change session-to-session;
# repeated route hits just hand back this same bytes object.
CLAUDE_PIG_PNG: bytes = render_pig_png()
