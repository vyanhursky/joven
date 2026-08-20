#!/usr/bin/env python3
"""Build one KEPUB per footnote variant, for a single batched device test.

Kobo showed the markers but every tap jumped to the start of the book, while
Apple Books rendered the identical KEPUB correctly. So the file is valid and the
disagreement is about *mechanism* — which only the hardware can settle.

A device round-trip is the expensive step (rebuild, copy, eject, find the page,
tap), so testing one hypothesis per trip burns cycles. This builds every variant
at once with distinct titles, so one pass through the book answers all of them.

Usage:
    python tools/build_variants.py <book.epub> <annotations.json> [-o out-variants]
    python tools/build_variants.py ... --install     # copy straight to the Kobo
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from joven.model import Sidecar
from joven.render import render_epub
from joven.render.annotate import VARIANTS
from joven.verify import verify

KOBO = Path("/Volumes/KOBOeReader")

# Order matters: this is the order to test them in on the device, most likely
# fix first, control last.
# Round 2. Round 1 established that Kobo's "Footnote preview" *does* fire — but
# only for D (no epub:type, notes at end of chapter), and it then ran on into
# every following note. These fill the untested cells and try to scope it to one.
ORDER = [
    "H-file-type",    # one file per note + epub:type  <- most likely correct
    "I-file-plain",   # one file per note, no epub:type
    "F-type-end",     # epub:type + notes at end (the cell never tested)
    "G-span-end",     # epub:type on a <span>, per Kobo's own spec example
    "D-endnotes",     # round-1 winner, for comparison
    "E-inline",       # known-good fallback
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    parser.add_argument("annotations", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("out-variants"))
    parser.add_argument("--install", action="store_true", help="copy the KEPUBs to the Kobo")
    parser.add_argument(
        "--only", nargs="*", help="build a subset of variant keys (default: all)"
    )
    args = parser.parse_args()

    sidecar = Sidecar.load(args.annotations)
    keys = args.only or ORDER
    print(f"{len(sidecar.annotations)} annotations x {len(keys)} variants\n")

    built: list[tuple[str, Path, bool]] = []
    for key in keys:
        label, _ = VARIANTS[key]
        out_dir = args.out / key
        shutil.rmtree(out_dir, ignore_errors=True)

        result = render_epub(
            args.epub, sidecar, out_dir, renderer=key, make_kepub=True, suffix=f".{key}"
        )
        findings = verify(result.epub_path, args.epub, result.kepub_path)
        failed = [f for f in findings if not f.ok]
        ok = not failed

        print(f"{'ok  ' if ok else 'FAIL'} {key:18} {label}")
        print(f"       applied {result.annotations_applied}, css -> {result.stylesheet}")
        for finding in failed:
            print(f"       !! {finding.check}: {finding.detail.splitlines()[0][:90]}")

        if result.kepub_path:
            built.append((key, result.kepub_path, ok))

    if not args.install:
        print(f"\nbuilt {len(built)} KEPUB(s) under {args.out}/  (pass --install to copy)")
        return

    if not KOBO.is_dir():
        raise SystemExit(f"Kobo not mounted at {KOBO}")

    print("\ninstalling to the Kobo:")
    # Remove anything we put there previously so the test set is unambiguous.
    for stale in list(KOBO.glob("*joven*.kepub.epub")) + list(KOBO.glob("._*joven*")):
        stale.unlink()
        print(f"  removed  {stale.name}")

    for index, (key, path, ok) in enumerate(built, start=1):
        # Numbered so they sort into test order in the Kobo library, and named so
        # you always know which build you are looking at.
        dest = KOBO / f"joven {index} {key}.kepub.epub"
        shutil.copy2(path, dest)
        print(f"  copied   {dest.name}{'' if ok else '   (verify FAILED)'}")

    # macOS AppleDouble sidecars show up as junk books on the device.
    for junk in KOBO.glob("._*"):
        junk.unlink()

    print("\nEject the Kobo, then open each book and tap an asterisk.")
    for index, (key, _, _) in enumerate(built, start=1):
        print(f"  {index}. {key:18} {VARIANTS[key][0]}")


if __name__ == "__main__":
    main()
