"""KEPUB conversion via the ``kepubify`` binary.

KEPUB is Kobo's own flavour of EPUB and renders better on device than a plain
sideloaded EPUB. We shell out to `kepubify <https://pgaskin.net/kepubify/>`_
rather than depending on Calibre and its KePub Output plugin.

Ordering matters: ``epubcheck`` validates EPUB, not KEPUB, so the intermediate
EPUB 3 must be validated *before* conversion. Conversion is always the last step.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

KEPUB_SUFFIX = ".kepub.epub"


class KepubError(Exception):
    """Raised when KEPUB conversion fails."""


def kepubify_available() -> bool:
    return shutil.which("kepubify") is not None


def kepub_name(epub_path: Path) -> str:
    """``book.annotated.epub`` -> ``book.annotated.kepub.epub``.

    The double extension is not cosmetic: without it the Kobo treats a sideloaded
    file as a plain EPUB and ignores the KEPUB features entirely.
    """
    stem = epub_path.name
    # longest suffix first, or "book.kepub.epub" becomes "book.kepub.kepub.epub"
    for suffix in (KEPUB_SUFFIX, ".epub"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem + KEPUB_SUFFIX


def kepubify(epub_path: Path, out_dir: Path) -> Path:
    """Convert an EPUB to KEPUB, returning the output path."""
    if not kepubify_available():
        raise KepubError("kepubify not found on PATH — install with: brew install kepubify")

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / kepub_name(epub_path)

    proc = subprocess.run(  # noqa: S603
        ["kepubify", "--inplace=false", "--output", str(target), str(epub_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise KepubError(f"kepubify failed (exit {proc.returncode}): {detail}")

    if not target.is_file():
        # kepubify picks its own filename in some modes; find what it produced
        produced = sorted(out_dir.glob("*.kepub.epub"), key=lambda p: p.stat().st_mtime)
        if not produced:
            raise KepubError(f"kepubify reported success but produced no file in {out_dir}")
        produced[-1].rename(target)

    return target
