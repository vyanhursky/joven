"""Render the project's cow-skull mark as ASCII, from geometry.

    python tools/skull.py                # the README size
    python tools/skull.py --scale 1.0 --width 74

Kept in the repo so the art in README.md is reproducible rather than an
inscrutable blob of punctuation somebody would have to redraw by hand.

**Why geometry and not hand-drawn strokes.** Every feature that makes a bovine
skull recognisable is a *gradient* — the depth of the orbits, the taper of the
horns, the pale sheen of bone. Line characters (``/ \\ | - '``) can only express
outlines, so a dozen hand-drawn attempts all came out as cartoons. Overlapping
implicit shapes sampled onto a grid and mapped through a density ramp gets the
gradients for free.

**The one trap.** A character cell is about twice as tall as it is wide, so the
horizontal axis must be scaled by half. Getting that wrong put the horns at
y=-26 in a view that only reached y=-5 — they rendered entirely off-canvas, and
the first version looked like a skull with no horns at all.
"""

from __future__ import annotations

import argparse
import math

RAMP = "@%#*+=:-. "
"""Dark to light. Cavities take `@`, lit bone takes `.`; horns use their own set."""


def _ellipse(x: float, y: float, cx: float, cy: float, rx: float, ry: float, rot: float = 0.0):
    """Signed coverage of a rotated ellipse: positive inside, zero on the edge."""
    dx, dy = x - cx, y - cy
    if rot:
        c, s = math.cos(rot), math.sin(rot)
        dx, dy = dx * c + dy * s, -dx * s + dy * c
    return 1.0 - ((dx / rx) ** 2 + (dy / ry) ** 2)


def _horn(x: float, y: float, side: int, s: float) -> float:
    """A lyre curve leaving the cranium outward, then rising, tapering to a point."""
    best = -9.0
    for i in range(81):
        t = i / 80
        hx = side * s * (5.0 + 12.5 * math.sin(t * 1.5))
        hy = s * (-3.5 - 10.5 * t**1.55)
        r = s * 1.95 * (1 - 0.80 * t)
        best = max(best, 1.0 - (((x - hx) / r) ** 2 + ((y - hy) / r) ** 2))
    return best


def render(scale: float = 0.80, width: int = 62) -> str:
    s = scale
    rows: list[str] = []
    for iy in range(int(-15 * s) - 1, int(23 * s) + 2):
        line: list[str] = []
        for ix in range(width):
            x, y = (ix - width / 2) * 0.5, float(iy)

            bone = max(
                _ellipse(x, y, 0, 0, 7.2 * s, 6.0 * s),                    # cranium
                _ellipse(x, y, 0, 10.0 * s, 4.6 * s, 11.5 * s),            # muzzle
                _ellipse(x, y, -5.6 * s, 3.4 * s, 3.4 * s, 4.6 * s),       # zygomatic
                _ellipse(x, y, 5.6 * s, 3.4 * s, 3.4 * s, 4.6 * s),
            )
            socket = max(
                _ellipse(x, y, -4.4 * s, 0.8 * s, 2.5 * s, 2.6 * s, -0.3),  # orbits
                _ellipse(x, y, 4.4 * s, 0.8 * s, 2.5 * s, 2.6 * s, 0.3),
                _ellipse(x, y, 0, 17.5 * s, 1.7 * s, 3.0 * s),              # nasal opening
            )
            horn = max(_horn(x, y, -1, s), _horn(x, y, 1, s))

            if socket > 0:
                line.append("@" if socket > 0.30 else "%")
            elif bone > 0:
                lit = min(1.0, bone * 1.9) - 0.035 * x / s + 0.010 * y / s
                line.append(RAMP[max(4, min(8, int(4 + lit * 4.4)))])
            elif horn > 0:
                line.append("#" if horn > 0.55 else "*" if horn > 0.15 else "+")
            else:
                line.append(" ")
        rows.append("".join(line).rstrip())
    return "\n".join(r for r in rows if r.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=float, default=0.80)
    parser.add_argument("--width", type=int, default=62)
    args = parser.parse_args()
    art = render(args.scale, args.width)
    print(art)
    # Below ~0.75 the horns fragment into disconnected marks and the muzzle
    # collapses; 0.80 is the practical floor for this to stay readable.
    if args.scale < 0.75:
        print("\n  note: below scale 0.75 the horns fragment — this will look broken")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
