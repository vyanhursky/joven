"""Rendering: turn (original epub + sidecar) into an annotated EPUB 3 / KEPUB.

Rendering is a **pure, idempotent function** of its two inputs. It never reads
back the previous output and never mutates the source file, which is what makes
"edit the sidecar and re-render" the correction workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..epub.archive import EpubArchive
from ..epub.document import parse, serialize
from ..epub.package import read_package
from ..kepub import kepubify, kepubify_available
from ..model import Sidecar
from .annotate import Renderer, RenderError, get_renderer
from .kobo import dekepubify, is_kepubified
from .upgrade import (
    add_epub_namespace,
    ensure_epub_namespace,
    fix_content_type_meta,
    register_documents,
    register_stylesheet,
    upgrade_package,
)

__all__ = ["RenderError", "RenderResult", "render_epub"]


@dataclass(slots=True)
class RenderResult:
    epub_path: Path
    kepub_path: Path | None = None
    annotations_applied: int = 0
    documents_touched: int = 0
    upgraded_to_epub3: bool = False
    stylesheet: str | None = None
    skipped: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    normalized_kobo: list[str] = field(default_factory=list)
    note_documents: list[str] = field(default_factory=list)


def render_epub(
    source: str | Path,
    sidecar: Sidecar | None,
    out_dir: str | Path,
    *,
    renderer: str | Renderer = "footnote",
    make_kepub: bool = True,
    suffix: str = ".annotated",
) -> RenderResult:
    """Apply ``sidecar`` to ``source`` and write the results into ``out_dir``."""
    source = Path(source)
    out_dir = Path(out_dir)
    engine = get_renderer(renderer) if isinstance(renderer, str) else renderer

    archive = EpubArchive.read(source)
    package = read_package(archive)

    grouped = sidecar.by_document() if sidecar else {}
    result = RenderResult(epub_path=out_dir / f"{source.stem}{suffix}.epub")

    # Documents named in the sidecar but absent from the book: report, don't crash.
    for href in list(grouped):
        if href not in archive:
            result.skipped.append(href)
            grouped.pop(href)

    # A source that is already Kobo-converted must be un-converted before we
    # touch it: a marker inserted inside a koboSpan renders on the device as a
    # bare asterisk with the passage gone, and no file-level check can see it.
    # Skipped when there is nothing to annotate, so the passthrough render stays
    # byte-identical.
    if grouped and is_kepubified(archive):
        result.normalized_kobo = dekepubify(archive)

    if grouped:
        result.stylesheet = register_stylesheet(archive, package, engine.css())
    if grouped and engine.needs_epub3():
        result.upgraded_to_epub3 = upgrade_package(archive, package)
        # Declaring EPUB 3 makes the book's pre-existing Calibre defects into
        # validation errors, so repair them in the same breath.
        result.repaired = fix_content_type_meta(archive, package)

    for href, notes in grouped.items():
        original = archive.get(href)
        tree = parse(original)
        if engine.needs_epub3() and ensure_epub_namespace(tree.getroot()):
            tree = add_epub_namespace(tree)
        applied = engine.apply(tree, notes)
        archive.replace(href, serialize(tree, original=original))
        result.annotations_applied += applied
        result.documents_touched += 1

    # one-note-per-file placement generates standalone note documents
    pending = list(getattr(engine, "new_documents", []) or [])
    if pending:
        result.note_documents = register_documents(archive, package, pending)

    archive.write(result.epub_path)

    if make_kepub and kepubify_available():
        result.kepub_path = kepubify(result.epub_path, out_dir)

    return result
