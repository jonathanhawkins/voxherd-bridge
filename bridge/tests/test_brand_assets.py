"""Pin the brand-asset PNG output so refactors of the rasterizer can't
silently produce a broken image (wrong signature, wrong dimensions,
all-transparent because the block-char mapping flipped, etc)."""

from __future__ import annotations

import struct

from bridge.brand_assets import (
    CLAUDE_ORANGE,
    CLAUDE_PIG_ART,
    CLAUDE_PIG_PNG,
    _ascii_to_subpixel_grid,
    render_pig_png,
)


def test_png_starts_with_valid_signature():
    # All PNGs MUST start with this 8-byte magic. Without it, the iOS
    # Image loader will reject the asset and the lens shows nothing.
    assert CLAUDE_PIG_PNG.startswith(b"\x89PNG\r\n\x1a\n")


def test_png_dimensions_match_art_at_default_scale():
    # IHDR sits 8 bytes after the signature: 4 length + 4 type = 8, then
    # the 13-byte payload begins. Width is bytes [16:20], height [20:24].
    width = struct.unpack(">I", CLAUDE_PIG_PNG[16:20])[0]
    height = struct.unpack(">I", CLAUDE_PIG_PNG[20:24])[0]
    # Default pad_to_aspect=20.0: source canvas is 20× wider than tall.
    # Height = 3 art rows × 2 sub-pixels × 8× scale = 48. Width = 48 × 20
    # = 960. The pig itself stays 144 × 48 centered with transparent
    # horizontal padding; only the canvas grows. .fill stretches this to
    # the column width, so the visible pig shrinks proportionally.
    assert width == 960, f"width should be 960 with 20:1 padding, got {width}"
    assert height == 48, f"height should be 48 (unchanged pig height), got {height}"


def test_subpixel_grid_has_expected_dimensions():
    grid = _ascii_to_subpixel_grid()
    assert len(grid) == 2 * len(CLAUDE_PIG_ART), "one art row → two sub-pixel rows"
    expected_cols = 2 * max(len(row) for row in CLAUDE_PIG_ART)
    for row in grid:
        assert len(row) == expected_cols, "rows must be right-padded to a rectangle"


def test_subpixel_grid_has_filled_cells():
    # Regression: an earlier draft with a wrong-direction mapping produced
    # an all-False grid (everything transparent), which the PNG encoder
    # happily compresses into a 0-byte image. Pin that the mascot is NOT
    # completely empty — at minimum the full-block █ chars must light up.
    grid = _ascii_to_subpixel_grid()
    filled = sum(1 for row in grid for cell in row if cell)
    assert filled > 0, "rasterized pig must have at least some lit pixels"
    # A rough lower bound: every █ in the art contributes 4 sub-pixels.
    # The middle row alone has 5 █'s → 20 sub-pixels. Use 20 as the floor.
    assert filled >= 20, f"too few lit pixels ({filled}); block-char mapping may be broken"


def test_render_pig_png_honors_custom_color():
    # Generate at scale=1 with bright magenta. The IDAT is zlib-compressed
    # so we can't just bytes-search for the color, but inflating and
    # scanning is straightforward. The point of this test is to catch a
    # refactor that hardcodes the orange constant past the parameter.
    import zlib
    magenta = (255, 0, 255)
    png = render_pig_png(color_rgb=magenta, scale=1)
    # Walk chunks to find IDAT. Skip signature (8 bytes), then loop.
    pos = 8
    inflated = b""
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        chunk_type = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        if chunk_type == b"IDAT":
            inflated = zlib.decompress(data)
            break
        pos += 8 + length + 4
    assert inflated, "IDAT chunk must be present"
    # Inflated stream is row-major: filter byte + pixels per row. Just
    # check the color bytes appear somewhere — proof the parameter took.
    assert bytes(magenta) + b"\xff" in inflated, "magenta pixels not in IDAT"
    assert bytes(CLAUDE_ORANGE) + b"\xff" not in inflated, "default color leaked into custom render"
