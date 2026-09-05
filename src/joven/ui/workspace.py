"""Where the browser UI keeps its books.

A browser cannot hand a local server a file path, so a dropped EPUB is copied into
a working directory keyed by its SHA-256, and everything the workflow derives from
it lives alongside::

    ~/.joven/books/<sha256>/
        <original filename>.epub    the book, never edited
        meta.json                   title, author, when it was added
        annotations.json            the sidecar the review page edits
        trace.jsonl                 the decision trace, flushed per record
        out/                        the rendered .epub and .kepub.epub

Keying by hash means dropping the same file twice finds the same directory, and
two editions of one title never share a sidecar. ``JOVEN_HOME`` moves the root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from ..epub.archive import EpubArchive, EpubError
from ..epub.document import iter_text_units
from ..epub.package import read_package
from ..model import Sidecar, file_sha256

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\-]+")


def default_home() -> Path:
    return Path(os.environ.get("JOVEN_HOME") or Path.home() / ".joven")


def safe_filename(name: str) -> str:
    """A filename that cannot escape its directory or upset a filesystem."""
    name = Path(name).name  # no directories, whatever the browser sent
    name = _UNSAFE.sub("_", name).strip(" .") or "book.epub"
    if not name.lower().endswith(".epub"):
        name += ".epub"
    return name[:120]


@dataclass(frozen=True, slots=True)
class Book:
    sha: str
    dir: Path
    filename: str

    @property
    def epub(self) -> Path:
        return self.dir / self.filename

    @property
    def sidecar_path(self) -> Path:
        return self.dir / "annotations.json"

    @property
    def trace_path(self) -> Path:
        return self.dir / "trace.jsonl"

    @property
    def out_dir(self) -> Path:
        return self.dir / "out"

    def meta(self) -> dict:
        try:
            return json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def outputs(self) -> list[Path]:
        if not self.out_dir.is_dir():
            return []
        return sorted(p for p in self.out_dir.iterdir() if p.suffix == ".epub")

    def summary(self) -> dict:
        """What the book list shows: identity, and which stages have run."""
        meta = self.meta()
        annotations = None
        counts = None
        if self.sidecar_path.is_file():
            try:
                sidecar = Sidecar.load(self.sidecar_path)
                annotations = len(sidecar.annotations)
                counts = sidecar.counts()
            except (OSError, ValueError, KeyError):
                annotations = None
        return {
            "sha": self.sha,
            "filename": self.filename,
            "title": meta.get("title", ""),
            "creator": meta.get("creator", ""),
            "added": meta.get("added"),
            "size": self.epub.stat().st_size if self.epub.is_file() else 0,
            "annotations": annotations,
            "counts": counts,
            "has_trace": self.trace_path.is_file(),
            "outputs": [p.name for p in self.outputs()],
        }

    def inspect(self) -> dict:
        """What ``joven inspect`` prints, as data."""
        archive = EpubArchive.read(self.epub)
        package = read_package(archive)
        spine = []
        total_units = total_words = 0
        for href in package.spine_hrefs:
            if href not in archive:
                spine.append({"href": href, "missing": True, "units": 0, "words": 0})
                continue
            units = iter_text_units(archive.get(href), href)
            words = sum(len(u.text.split()) for u in units)
            total_units += len(units)
            total_words += words
            spine.append({"href": href, "missing": False, "units": len(units), "words": words})
        return {
            "entries": len(archive.names()),
            "opf": package.opf_path,
            "version": package.version,
            "metadata": package.metadata,
            "spine": spine,
            "units": total_units,
            "words": total_words,
        }


class Workspace:
    def __init__(self, home: Path | None = None) -> None:
        self.root = (home or default_home()) / "books"

    def books(self) -> list[Book]:
        if not self.root.is_dir():
            return []
        found = []
        for entry in sorted(self.root.iterdir()):
            book = self._book_in(entry)
            if book is not None:
                found.append(book)
        found.sort(key=lambda b: b.meta().get("added") or 0, reverse=True)
        return found

    def get(self, sha: str) -> Book | None:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        return self._book_in(self.root / sha)

    def _book_in(self, directory: Path) -> Book | None:
        if not directory.is_dir():
            return None
        meta = directory / "meta.json"
        if not meta.is_file():
            return None
        try:
            filename = json.loads(meta.read_text(encoding="utf-8"))["filename"]
        except (OSError, ValueError, KeyError):
            return None
        book = Book(sha=directory.name, dir=directory, filename=filename)
        return book if book.epub.is_file() else None

    def add_bytes(self, data: bytes, filename: str) -> Book:
        """Store an uploaded EPUB. The same bytes twice land in the same place."""
        staging = self.root / f".incoming-{time.time_ns()}"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            candidate = staging / safe_filename(filename)
            candidate.write_bytes(data)
            return self._adopt(candidate)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def add_path(self, path: Path) -> Book:
        """Store a copy of an EPUB already on this machine."""
        if not path.is_file():
            raise EpubError(f"{path} is not a file")
        return self._adopt(path)

    def _adopt(self, source: Path) -> Book:
        # Refuse anything the pipeline would refuse, before it takes up residence.
        archive = EpubArchive.read(source)
        package = read_package(archive)
        sha = file_sha256(source)
        directory = self.root / sha
        existing = self._book_in(directory)
        if existing is not None:
            return existing
        directory.mkdir(parents=True, exist_ok=True)
        filename = safe_filename(source.name)
        shutil.copyfile(source, directory / filename)
        (directory / "meta.json").write_text(
            json.dumps(
                {
                    "filename": filename,
                    "title": package.metadata.get("title", ""),
                    "creator": package.metadata.get("creator", ""),
                    "added": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return Book(sha=sha, dir=directory, filename=filename)
