#!/usr/bin/env python3
"""Build a small hand-written sidecar for the M3 Kobo device test.

Locates a few known paragraphs by exact text and emits an ``annotations.json``
with hand-checked translations, so the device test exercises real markup on real
prose without waiting for M4's detection run.

Usage:
    python tools/make_sample.py <book.epub> [-o sample-annotations.json]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from etx.epub.archive import EpubArchive
from etx.epub.document import iter_text_units
from etx.epub.package import read_package
from etx.model import Annotation, Sidecar, file_sha256, normalize, occurrence_indices

# (paragraph text, Spanish substring to mark, translation)
#
# The whole blind-heretic passage from DESIGN.md §1.1, hand-translated and
# hand-checked. Deliberately *not* detector output: this sample exists to test
# rendering on the device, so every translation here is known-correct and a
# surprise on the Kobo is unambiguously a rendering bug, not a model error.
#
# Covers all three annotatable mixing patterns:
#   A  whole-paragraph Spanish
#   B  Spanish + trailing English dialogue tag (marker must land before the tag)
#   B' Spanish, English tag mid-paragraph, Spanish again (multi-sentence span)
SAMPLES: list[tuple[str, str, str]] = [
    ("Se fué.", "Se fué.", "He is gone."),
    (
        "Ay. Ándale, joven. Ándale pues.",
        "Ay. Ándale, joven. Ándale pues.",
        "Oh. Get on with it, young man. Go on then.",
    ),
    ("Vaya con Dios.", "Vaya con Dios.", "Go with God."),
    ("Y tú, joven.", "Y tú, joven.", "And you, young man."),
    # pattern B — the marker must sit before "the old man said"
    ("Cuántos años tienes? the old man said.", "Cuántos años tienes?", "How old are you?"),
    ("Dieciseis.", "Dieciseis.", "Sixteen."),
    # pattern B' — English dialogue tag inside the Spanish run
    (
        "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
        "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
        "Listen to me, young man, he said. I know nothing. That is the truth.",
    ),
    ("Está bien.", "Está bien.", "It is all right."),
    (
        "Y qué clase de lugar es éste? the boy said.",
        "Y qué clase de lugar es éste?",
        "And what kind of place is this?",
    ),
    (
        "Lugares donde el fierro ya está en la tierra, the old man said. "
        "Lugares donde ha quemado el fuego.",
        "Lugares donde el fierro ya está en la tierra, the old man said. "
        "Lugares donde ha quemado el fuego.",
        "Places where the iron is already in the ground, the old man said. "
        "Places where the fire has burned.",
    ),
    ("Y cómo se encuentra?", "Y cómo se encuentra?", "And how does one find it?"),
    (
        "Y por eso soy hereje, he said. Por eso y nada más.",
        "Y por eso soy hereje, he said. Por eso y nada más.",
        "And that is why I am a heretic, he said. For that and nothing more.",
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("sample-annotations.json"))
    args = parser.parse_args()

    archive = EpubArchive.read(args.epub)
    package = read_package(archive)

    # index every paragraph by normalized text, carrying its occurrence ordinal so
    # repeated dialogue ("Yessir." x21) does not collapse to one id
    index: dict[str, list[tuple[str, int, str, int]]] = {}
    for href in package.spine_hrefs:
        if href not in archive:
            continue
        units = iter_text_units(archive.get(href), href)
        for unit, occurrence in zip(units, occurrence_indices(u.text for u in units), strict=True):
            index.setdefault(normalize(unit.text), []).append(
                (href, unit.index, unit.text, occurrence)
            )

    sidecar = Sidecar(
        source_sha256=file_sha256(args.epub),
        title=package.metadata.get("title", args.epub.stem),
    )

    for wanted, spanish, translation in SAMPLES:
        matches = index.get(normalize(wanted))
        if not matches:
            print(f"  NOT FOUND  {wanted!r}")
            continue
        href, para_index, actual, occurrence = matches[0]
        start = actual.find(spanish)
        if start < 0:
            print(f"  span not in paragraph: {spanish!r} in {actual[:60]!r}")
            continue
        end = start + len(spanish)
        sidecar.annotations.append(
            Annotation.create(
                href=href,
                para_index=para_index,
                source_text=actual,
                spans=[(start, end)],
                translation=translation,
                occurrence=occurrence,
                detector_confidence=1.0,
                model="hand-written (M3 device sample)",
            )
        )
        occurrences = f" ({len(matches)} occurrences, using first)" if len(matches) > 1 else ""
        print(f"  ok  {href}#{para_index}  {wanted[:44]!r} -> {translation!r}{occurrences}")

    sidecar.save(args.out)
    print(f"\nwrote {args.out} with {len(sidecar.annotations)} annotation(s)")


if __name__ == "__main__":
    main()
