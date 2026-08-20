#!/usr/bin/env python3
"""Build a calibration book that measures Kobo's footnote-preview length limit.

Device testing showed a 25-character note popping up as a "Footnote preview" while
a 99-character note jumped straight to the note page. Kobo's published limit is
5000 characters, so the real cutoff is far lower — but two data points cannot
locate it, and they came from two different books, so length and ``epub:type``
are still confounded.

This attaches notes of *known, systematically increasing* length to the twelve
sample paragraphs, all using one recipe. Every note announces its own length, so
tapping down the page reads the threshold straight off the screen:

    003 chars: xxx...
    ...
    240 chars: xxx...

One device round-trip gives the exact cutoff instead of another hypothesis.

Usage:
    python tools/make_calibration.py <book.epub> [-o calibration-annotations.json]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from joven.epub.archive import EpubArchive
from joven.epub.document import iter_text_units
from joven.epub.package import read_package
from joven.model import Annotation, Sidecar, file_sha256, normalize, occurrence_indices

# The paragraphs to hang calibration notes on, in reading order down the page.
TARGETS = [
    "Se fué.",
    "Ay. Ándale, joven. Ándale pues.",
    "Vaya con Dios.",
    "Y tú, joven.",
    "Cuántos años tienes? the old man said.",
    "Dieciseis.",
    "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
    "Está bien.",
    "Y qué clase de lugar es éste? the boy said.",
    "Lugares donde el fierro ya está en la tierra, the old man said. "
    "Lugares donde ha quemado el fuego.",
    "Y cómo se encuentra?",
    "Y por eso soy hereje, he said. Por eso y nada más.",
]

# Brackets the observed popup (25) and jump (99) closely, then extends past it so
# we can see whether the behaviour flips back.
LENGTHS = [10, 20, 30, 40, 50, 60, 70, 85, 100, 130, 180, 250]


def calibration_text(length: int) -> str:
    """A note of exactly ``length`` characters that states its own length.

    The label is kept to four characters (``010:``) so it still fits inside the
    shortest rung of the ladder.
    """
    label = f"{length:03d}:"
    if length < len(label):
        raise ValueError(f"length {length} is too short to label")
    filler = "abcdefghij " * 40
    return (label + filler)[:length]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("calibration-annotations.json"))
    args = parser.parse_args()

    archive = EpubArchive.read(args.epub)
    package = read_package(archive)

    index: dict[str, tuple[str, int, str, int]] = {}
    for href in package.spine_hrefs:
        if href not in archive:
            continue
        units = iter_text_units(archive.get(href), href)
        for unit, occurrence in zip(units, occurrence_indices(u.text for u in units), strict=True):
            index.setdefault(normalize(unit.text), (href, unit.index, unit.text, occurrence))

    sidecar = Sidecar(
        source_sha256=file_sha256(args.epub),
        title=package.metadata.get("title", args.epub.stem),
    )

    for target, length in zip(TARGETS, LENGTHS, strict=True):
        match = index.get(normalize(target))
        if match is None:
            print(f"  NOT FOUND  {target[:50]!r}")
            continue
        href, para_index, actual, occurrence = match
        text = calibration_text(length)
        assert len(text) == length, (len(text), length)
        sidecar.annotations.append(
            Annotation.create(
                href=href,
                para_index=para_index,
                source_text=actual,
                spans=[(0, len(actual))],
                translation=text,
                occurrence=occurrence,
                detector_confidence=1.0,
                model="calibration",
            )
        )
        print(f"  {length:>3} chars  ->  {target[:44]!r}")

    sidecar.save(args.out)
    print(f"\nwrote {args.out} with {len(sidecar.annotations)} calibration note(s)")
    print("Tap each marker down the page; the last one that shows a popup is the limit.")


if __name__ == "__main__":
    main()
