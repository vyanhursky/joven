"""Build the app icons from the README's horse.

The art in the README is the only copy: this reads it from there rather than
keeping a second one that can drift. Run it when the art changes; the generated
files are committed, so neither CI nor a contributor needs Pillow to build.

    python packaging/make_icon.py

It writes ``packaging/icons/joven.icns`` (macOS), ``joven.ico`` (Windows) and
``joven.png`` (Linux desktop files), and the .iconset it built the .icns from.

Two things about the source shape it to an icon:

**It is line art, not a silhouette.** The horse is drawn in outline, so shrinking
it merges the strokes into a smudge. Small sizes get the strokes dilated first --
the standard trick -- which is why the 16px art is not just the 512px art scaled.

**Light strokes need a ground.** The art is light-on-dark, which on a light
desktop background would be an invisible icon, so it sits on a dark rounded card
rather than on transparency.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "packaging" / "icons"

BG = (26, 32, 41)
INK = (236, 239, 244)

# The art's own density ramp, lightest to heaviest.
RAMP = {" ": 0.0, ".": 0.22, ":": 0.34, "-": 0.44, "=": 0.58,
        "+": 0.68, "*": 0.78, "#": 0.90, "%": 0.94, "@": 1.0}

# How much to dilate the strokes before downscaling, per final pixel size. Zero
# above 128, where the art survives on its own.
GROW = {16: 11, 32: 7, 48: 5, 64: 3, 128: 0, 256: 0, 512: 0, 1024: 0}


def art_from_readme() -> list[str]:
    """The longest fenced block in the README is the horse."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```\n(.*?)```", text, re.S)
    if not blocks:
        raise SystemExit("no fenced block in README.md to take the art from")
    return max(blocks, key=len).rstrip("\n").split("\n")


def master(art: list[str]) -> Image.Image:
    """The art as a greyscale mask, square and padded."""
    rows, cols = len(art), max(len(line) for line in art)
    cell = Image.new("L", (cols, rows), 0)
    pixels = cell.load()
    for y, line in enumerate(art):
        for x, char in enumerate(line):
            pixels[x, y] = int(255 * RAMP.get(char, 0.0 if char.isspace() else 1.0))
    # A terminal cell is about twice as tall as it is wide; without this the
    # horse comes out squashed.
    scaled = cell.resize((cols * 16, rows * 32), Image.LANCZOS)
    box = scaled.getbbox()
    cropped = scaled.crop(box)
    side = int(max(cropped.size) * 1.14)
    square = Image.new("L", (side, side), 0)
    square.paste(cropped, ((side - cropped.width) // 2, (side - cropped.height) // 2))
    return square


def tile(mask: Image.Image, size: int) -> Image.Image:
    """One icon at one size: dilated if small, inked, on a rounded dark card."""
    work = mask.resize((size * 8, size * 8), Image.LANCZOS)
    if grow := GROW.get(size, 0):
        work = work.filter(ImageFilter.MaxFilter(grow))
    work = work.resize((size, size), Image.LANCZOS)

    card = Image.new("RGB", (size, size), BG)
    card.paste(Image.new("RGB", (size, size), INK), (0, 0), work)

    corner = Image.new("L", (size, size), 0)
    ImageDraw.Draw(corner).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(2, round(size * 0.22)), fill=255
    )
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(card, (0, 0), corner)
    return out


def main() -> int:
    ICONS.mkdir(parents=True, exist_ok=True)
    mask = master(art_from_readme())

    # macOS wants an .iconset of named pairs, which iconutil turns into .icns.
    iconset = ICONS / "joven.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        tile(mask, size).save(iconset / f"icon_{size}x{size}.png")
        tile(mask, size * 2).save(iconset / f"icon_{size}x{size}@2x.png")

    if sys.platform == "darwin" and shutil.which("iconutil"):
        subprocess.run(  # noqa: S603
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ICONS / "joven.icns")],
            check=True,
        )
        print(f"  -> {ICONS / 'joven.icns'}")
    else:
        print("  .. iconutil not available; .icns left as the .iconset beside it")

    # Windows: one .ico carrying every size, so Explorer picks per context.
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [tile(mask, s) for s in sizes]
    frames[-1].save(
        ICONS / "joven.ico", format="ICO", sizes=[(s, s) for s in sizes],
        append_images=frames[:-1],
    )
    print(f"  -> {ICONS / 'joven.ico'}")

    # Linux: a plain PNG, which is what a .desktop file points at.
    tile(mask, 512).save(ICONS / "joven.png")
    print(f"  -> {ICONS / 'joven.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
