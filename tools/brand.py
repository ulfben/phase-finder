"""Draw the brand icons: three phase waves, the middle one picked out by a ring.

Run: python tools/brand.py  (needs Pillow)
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "custom_components" / "phase_finder" / "brand"
# IEC 60446 phase colours: brown, black, grey. Black becomes light on dark backgrounds.
LIGHT = [(139, 90, 43), (40, 40, 40), (128, 128, 128)]
DARK = [(196, 132, 72), (225, 225, 225), (150, 150, 150)]
ACCENT = (3, 169, 244)


def draw(size: int, colours: list[tuple[int, int, int]]) -> Image.Image:
    scale = 4  # draw large, then downsample for smooth lines
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    width = int(s * 0.055)
    margin = s * 0.1
    for i, colour in enumerate(colours):
        cy = s * (0.28 + 0.22 * i)
        phase = i * 2 * math.pi / 3
        points = [
            (
                margin + x * (s - 2 * margin) / 200,
                cy + s * 0.07 * math.sin(phase + x / 200 * 4 * math.pi),
            )
            for x in [i / 4 for i in range(801)]
        ]
        half = width / 2
        for x, y in points:  # round dots along the curve: smooth, with round ends
            d.ellipse((x - half, y - half, x + half, y + half), fill=colour)
    r = s * 0.17
    cx, cy = s * 0.5, s * 0.5
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ACCENT, width=int(s * 0.05))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, colours in (
        ("icon", LIGHT),
        ("logo", LIGHT),
        ("dark_icon", DARK),
        ("dark_logo", DARK),
    ):
        draw(256, colours).save(OUT / f"{name}.png")
        draw(512, colours).save(OUT / f"{name}@2x.png")


if __name__ == "__main__":
    main()
