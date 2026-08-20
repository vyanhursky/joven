"""Fail if tracked files contain substantial verbatim text from a book.

This repository is a tool for annotating books you own; it must not become a way
to distribute one. Two things make that easy to get wrong:

* **Derived artifacts are the real risk, not the EPUB.** ``trace.jsonl`` keeps one
  record per segment with the source text verbatim — about 772,000 characters for
  *The Crossing*, which is the whole novel. ``.gitignore`` covers those now, but a
  pattern list is only as good as its last update.
* **Git history is permanent.** A single commit containing them cannot be undone by
  deleting the file afterwards.

So this checks the *content* of what git is tracking rather than trusting filename
patterns, and it distinguishes two cases: long verbatim runs (a copy) from short
illustrative quotes in docs and tests (commentary, which is the point of a design
document). Run it against the book you developed against:

    python tools/check_no_book_content.py path/to/book.epub

Exits non-zero if any single tracked file quotes more than --max-chars.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from etx.epub.archive import EpubArchive  # noqa: E402
from etx.epub.document import iter_text_units  # noqa: E402
from etx.epub.package import read_package  # noqa: E402

MIN_QUOTE = 40
"""Shorter runs than this are phrases, not copying — 'Se fué.' is not a excerpt."""


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [Path(p) for p in out.split("\n") if p.strip()]


def book_passages(epub: Path) -> list[str]:
    archive = EpubArchive.read(epub)
    package = read_package(archive)
    passages: list[str] = []
    for href in package.spine_hrefs:
        if href not in archive:
            continue
        for unit in iter_text_units(archive.get(href), href):
            text = " ".join(unit.text.split())
            # Split long paragraphs into sentences so a doc quoting one sentence
            # is not scored as if it quoted the whole paragraph.
            for piece in text.replace("? ", "?|").replace(". ", ".|").split("|"):
                piece = piece.strip()
                if len(piece) >= MIN_QUOTE:
                    passages.append(piece)
    return passages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path, help="the book to check against")
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    passages = book_passages(args.epub)
    print(f"checking tracked files against {len(passages):,} passages from {args.epub.name}\n")

    offenders: list[tuple[Path, int, int, str]] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        try:
            blob = " ".join(path.read_text(encoding="utf-8").split())
        except (UnicodeDecodeError, OSError):
            continue
        hits = [p for p in passages if p in blob]
        if not hits:
            continue
        total = sum(len(h) for h in hits)
        longest = max(hits, key=len)
        offenders.append((path, total, len(hits), longest))

    offenders.sort(key=lambda row: -row[1])
    failed = False
    for path, total, count, longest in offenders:
        over = total > args.max_chars
        failed |= over
        flag = "FAIL" if over else "ok  "
        print(f"  [{flag}] {str(path):<34} {total:>6,} chars in {count:>3} quote(s)")
        if not args.quiet:
            print(f"           longest: {longest[:78]!r}")

    print()
    if failed:
        print(f"  FAIL: a tracked file quotes more than {args.max_chars:,} characters.")
        return 1
    grand = sum(row[1] for row in offenders)
    print(f"  PASS: {grand:,} characters quoted across {len(offenders)} file(s), "
          f"none over {args.max_chars:,}.")
    print("  Short excerpts used for technical commentary — no file contains a copy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
