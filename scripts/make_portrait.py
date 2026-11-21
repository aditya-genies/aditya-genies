#!/usr/bin/env python3
"""Turn a cutout photo into assets/portrait.txt: a grid of brightness levels 0-9, with a space where it is transparent.

generate_stats.py draws the grid as 0s and 1s. Use a PNG with a transparent background, for example
from "Copy Subject" in macOS Photos. Run this script again only when the photo changes:
    uv run --with pillow python scripts/make_portrait.py cutout.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

OUT = Path(__file__).resolve().parent.parent / "assets" / "portrait.txt"
COLS, ROWS = 132, 80  # A monospace cell is about 0.6 as wide as it is tall, so the grid is square.


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: make_portrait.py <cutout.png>")
    image = Image.open(sys.argv[1]).convert("RGBA")
    image = image.crop(image.getbbox())  # Crop to the subject.
    side = max(image.size)  # Center the subject in a square, so the grid is not stretched.
    square = Image.new("RGBA", (side, side))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    # Sharpen before the resize, so the eyes and mouth stay visible in the small grid.
    gray = square.convert("L").filter(ImageFilter.UnsharpMask(radius=4, percent=180)).resize((COLS, ROWS), Image.LANCZOS)
    alpha = square.getchannel("A").resize((COLS, ROWS), Image.LANCZOS).point(lambda a: 255 if a >= 128 else 0)
    gray = ImageOps.equalize(gray, mask=alpha)  # Spread the subject's tones over all levels.
    lines = []
    for row in range(ROWS):
        line = [
            str(round(9 * gray.getpixel((col, row)) / 255)) if alpha.getpixel((col, row)) else " "
            for col in range(COLS)
        ]
        lines.append("".join(line).rstrip())
    OUT.write_text("\n".join(lines) + "\n")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
