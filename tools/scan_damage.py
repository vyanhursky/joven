"""Report OCR/encoding damage in an EPUB — run it on a candidate before trusting it.

    python tools/scan_damage.py book.epub

Every marker here was found in the copy of *The Crossing* this project was built
against, by auditing what the translation pipeline got wrong. They fall into four
classes, and the classes matter more than the individual words: a different scan of
the same book will have its own substitutions but the same *kinds*.

Exits non-zero when anything is found, so it works as a gate.

The subtlety worth knowing: some corruptions are **real words**, so no spellchecker
flags them and the eye slides past them. ``Está fibre.`` for ``Está libre.`` reads
as a typo only if you know Spanish. Those are listed with the phrase that
disambiguates them, because the bare word gives false positives — ``bowl`` appears
27 times in this book and exactly once is damage (``el bowl`` for ``el bozal``).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from etx.epub.archive import EpubArchive  # noqa: E402
from etx.epub.document import iter_text_units  # noqa: E402
from etx.epub.package import read_package  # noqa: E402

_LETTER = re.compile(r"[^\W\d_]")
_DIGIT = re.compile(r"\d")

# Corruptions that are real words. Each carries the phrase that makes it
# unambiguous — searching the bare word alone produces false positives.
SUBSTITUTIONS: list[tuple[str, str, str]] = [
    # (search pattern, correct form, note)
    (r"\bBabe\b", "sabe", "knows"),
    (r"\bperms?\b", "perra / perros", "bitch / dogs"),
    (r"\baqua\b", "agua", "water"),
    (r"\bhistoric\b", "historia", "story"),
    (r"\bhistorian\b", "historias", "stories"),
    (r"\bfibre\b", "libre", "free"),
    (r"\bogre\b", "ogro", "ogre"),
    (r"\bMegan\b", "llegan", "they arrive"),
    (r"\bBabicora\b", "Babícora", "missing accent"),
    (r"\bto que\b", "lo que / todo que", "what / everything"),
    (r"\bsefior\b", "señor", "n-tilde lost"),
    # These need the phrase — the bare word is legitimate English elsewhere.
    (r"\bel bowl\b", "el bozal", "the muzzle"),
    (r"\bguitar el\b", "quitar el", "to remove"),
    (r"\bQue undo\b", "Qué mundo", "what a world"),
]


def book_text(path: Path) -> str:
    archive = EpubArchive.read(path)
    package = read_package(archive)
    return " ".join(
        unit.text
        for href in package.spine_hrefs
        if href in archive
        for unit in iter_text_units(archive.get(href), href)
    )


def scan(text: str) -> dict[str, Counter]:
    """Group findings by damage class."""
    found: dict[str, Counter] = {}

    # 1. Mojibake: UTF-8 bytes decoded as Latin-1. The most abundant marker in a
    #    bad conversion, and the easiest to eyeball — just search for "â".
    moji = Counter(m.group(0) for m in re.finditer(r"\S*[âÂÃ]\S*", text))
    if moji:
        found["mojibake (UTF-8 read as Latin-1)"] = moji

    # 2. A digit substituted for a letter: l->1, o->0, ó->16.
    #
    #    Naively flagging "any token with a letter and a digit" is too loose — it
    #    hits decades (1970s), ordinals (20th), catalogue numbers (PS3563), and
    #    version strings (v3), all legitimate. Real substitutions take one of two
    #    shapes: a substantial word with digits inside it (Conoc16, cuest16n), or a
    #    single capital followed by a 1 or 0 standing in for "El" / "Al".
    mixed = Counter(
        t
        for t in re.findall(r"[^\W_]+", text)
        if _DIGIT.search(t)
        and (
            len(_LETTER.findall(t)) >= 4
            or re.fullmatch(r"[A-ZÁÉÍÓÚÑ][01]", t)
        )
    )
    if mixed:
        found["digit inside a word (l->1, o->0)"] = mixed

    # 3. ñ scanned as fi/ii/ri.
    ntilde = Counter(
        m.group(0)
        for m in re.finditer(
            r"\b\w*(?:sefior|afio|nifio|mafiana|pequefi|compafi|castafio|Ibafiez)\w*\b",
            text,
            re.I,
        )
    )
    if ntilde:
        found["ñ corrupted to fi/ii"] = ntilde

    # 4. Substitutions that produce real words — the dangerous class.
    subs: Counter = Counter()
    for pattern, correct, note in SUBSTITUTIONS:
        for m in re.finditer(pattern, text):
            subs[f"{m.group(0)}  ->  {correct}  ({note})"] += 1
    if subs:
        found["wrong-but-real words"] = subs

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    parser.add_argument("--quiet", action="store_true", help="counts only")
    args = parser.parse_args()

    text = book_text(args.epub)
    found = scan(text)

    print(f"{args.epub.name}")
    print(f"  {len(text):,} characters of prose\n")

    total = 0
    for label, counter in found.items():
        n = sum(counter.values())
        total += n
        print(f"  {label}: {n} occurrence(s), {len(counter)} distinct")
        if not args.quiet:
            for token, count in sorted(counter.items(), key=lambda kv: -kv[1])[:12]:
                print(f"      {count:>3}x  {token}")
            if len(counter) > 12:
                print(f"      … and {len(counter) - 12} more")
        print()

    if total:
        print(f"  TOTAL {total} damage marker(s) — this copy is NOT clean")
        return 1
    print("  no damage markers found — this copy looks clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
