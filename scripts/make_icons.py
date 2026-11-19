#!/usr/bin/env python3
"""Generate every icon file `src-tauri/tauri.conf.json` references.

Tauri will not build without these; it fails with a confusing "icon not found"
error during bundling. This script keeps them reproducible: edit the colours or
the bolt geometry below and re-run, rather than hand-editing PNGs.

    python scripts/make_icons.py

Writes into src-tauri/icons/:
    32x32.png, 128x128.png, 128x128@2x.png (256), icon.png (512), icon.ico

`icon.ico` is a real multi-resolution Windows icon (16/24/32/48/64/128/256),
which matters: NSIS and Explorer each pick a different size, and a single-size
ICO shows up blurry in the taskbar.

Requires Pillow:  pip install pillow
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    sys.exit("Pillow is required:  pip install pillow")

OUT = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"

# --- Palette. Deliberately high-contrast: this has to read at 16px. --------
BG_TOP = (18, 20, 28)
BG_BOTTOM = (8, 9, 13)
BOLT_TOP = (255, 92, 92)
BOLT_BOTTOM = (176, 16, 44)
GLOW = (226, 55, 68)

MASTER = 1024  # drawn at this size, then downsampled with a good filter


def rounded_square(size: int, radius_ratio: float = 0.22) -> Image.Image:
    """The app tile: a subtle vertical gradient inside a rounded square."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Vertical gradient, drawn as horizontal scanlines.
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel((0, y), tuple(
            round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)
        ))
    grad = grad.resize((size, size))

    # Rounded-rect mask, supersampled 4x for a clean antialiased edge.
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size * 4 - 1, size * 4 - 1),
        radius=int(size * 4 * radius_ratio),
        fill=255,
    )
    mask = mask.resize((size, size), Image.LANCZOS)

    img.paste(grad, (0, 0), mask)
    return img


def bolt_polygon(size: int) -> list[tuple[float, float]]:
    """A lightning bolt, in a 0..1 coordinate space scaled to `size`.

    Coordinates are hand-tuned so the silhouette stays recognisable when the
    whole thing is 16 pixels wide.
    """
    pts = [
        (0.585, 0.105),
        (0.300, 0.545),
        (0.475, 0.545),
        (0.395, 0.895),
        (0.700, 0.430),
        (0.520, 0.430),
        (0.610, 0.105),
    ]
    return [(x * size, y * size) for x, y in pts]


def draw_bolt(size: int) -> Image.Image:
    """The bolt, with a soft crimson glow behind it."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    poly = bolt_polygon(size)

    # Glow: draw the shape, blur it, composite underneath.
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(poly, fill=GLOW + (190,))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.045))
    layer.alpha_composite(glow)
    layer.alpha_composite(glow)  # twice, for a denser core

    # The bolt itself, filled with a vertical gradient.
    bolt_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(bolt_mask).polygon(poly, fill=255)

    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel((0, y), tuple(
            round(a + (b - a) * t) for a, b in zip(BOLT_TOP, BOLT_BOTTOM)
        ))
    grad = grad.resize((size, size))

    layer.paste(grad, (0, 0), bolt_mask)
    return layer


def make_master() -> Image.Image:
    base = rounded_square(MASTER)
    base.alpha_composite(draw_bolt(MASTER))
    return base


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    master = make_master()

    # PNG sizes referenced by tauri.conf.json's bundle.icon list.
    png_targets = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
    }
    for name, size in png_targets.items():
        master.resize((size, size), Image.LANCZOS).save(OUT / name)
        print(f"  wrote {name:<16} {size}x{size}")

    # Multi-resolution ICO. Pillow picks the embedded sizes from `sizes`.
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(OUT / "icon.ico", format="ICO", sizes=ico_sizes)
    print(f"  wrote {'icon.ico':<16} {len(ico_sizes)} sizes (16..256)")

    print(f"\nDone -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
