"""Take Joven's annotations back out of a book it produced.

Rendering is a pure function of (original EPUB + sidecar), and the original is
never edited — so in principle the annotated copy is disposable. In practice the
annotated copy is the one that went into the library, and the original is the one
that did not. Found by running the browser UI against exactly such a library: the
only *The Crossing* on disk was a Joven output, and rendering a new sidecar onto
it put a second marker after every footnote, broke the text-preservation
invariant and failed epubcheck — while looking, at the paragraph level, entirely
plausible.

So two things. :func:`count_markers` lets ``render`` refuse an annotated source
outright, and :func:`strip_annotations` gives the original back: every inserted
node removed (markers, inline translations, in-file notes), every note document
dropped from the archive and the OPF, the appended CSS block cut. The prose is
byte-for-byte what the original had, because insertion only ever split text
nodes and removal only ever rejoins them. What it cannot undo is the EPUB 2 → 3
package upgrade; a stripped book is a clean EPUB 3, which renders again exactly
as the original would have.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from lxml import etree

from ..epub.archive import EpubArchive
from ..epub.document import JOVEN_ATTR, parse, serialize
from ..epub.package import OPF_NS, read_package
from .annotate import NOTES_DIR
from .kobo import _drop as drop_element

MARKER_BYTES = f'{JOVEN_ATTR}="marker"'.encode()

# How each renderer's CSS block begins; everything from there to the end of the
# stylesheet is ours, because register_stylesheet appends and nothing follows.
CSS_BLOCK_STARTS = ("/* --- joven translation footnotes --- */", "span.joven-inline {")


@dataclass(slots=True)
class StripResult:
    markers_removed: int = 0
    """Inserted nodes taken out of the prose: markers, inline translations, in-file notes."""
    notes_removed: int = 0
    """Standalone note documents dropped from the archive and the OPF."""
    documents_touched: list[str] = field(default_factory=list)
    stylesheet: str | None = None
    """The stylesheet the CSS block was cut from, if any."""


def _is_note_document(name: str) -> bool:
    return f"/{NOTES_DIR}/" in f"/{name}"


def count_markers(archive: EpubArchive) -> int:
    """How many footnote markers a previous render left in the prose. 0 for a clean book."""
    return sum(
        archive.get(name).count(MARKER_BYTES)
        for name in archive.xhtml_names()
        if not _is_note_document(name)
    )


def strip_annotations(archive: EpubArchive) -> StripResult:
    """Remove every trace of a previous render, in place, and report what went."""
    package = read_package(archive)
    result = StripResult()

    for name in archive.xhtml_names():
        if _is_note_document(name):
            continue
        original = archive.get(name)
        if JOVEN_ATTR.encode() not in original:
            continue
        tree = parse(original)
        inserted = [
            el
            for el in tree.getroot().iter()
            if isinstance(el.tag, str) and el.get(JOVEN_ATTR) is not None
        ]
        # Innermost first, so a note's marker is gone before the note is: dropping
        # a parent first would leave the child's tail nowhere to go.
        for el in reversed(inserted):
            drop_element(el)
        if inserted:
            archive.replace(name, serialize(tree, original=original))
            result.markers_removed += len(inserted)
            result.documents_touched.append(name)

    notes = [name for name in archive.names() if _is_note_document(name)]
    for name in notes:
        archive.remove(name)
    result.notes_removed = len(notes)
    if notes:
        _unregister(archive, package.opf_path, set(notes))

    result.stylesheet = _cut_css(archive, package.manifest.values())
    return result


def _unregister(archive: EpubArchive, opf_path: str, removed: set[str]) -> None:
    """Take the note documents out of the manifest and the spine."""
    original = archive.get(opf_path)
    opf = etree.fromstring(original)
    base = posixpath.dirname(opf_path)
    gone_ids: set[str] = set()
    for item in list(opf.iter(f"{{{OPF_NS}}}item")):
        href = item.get("href")
        if not href:
            continue
        resolved = posixpath.normpath(posixpath.join(base, href)) if base else href
        if resolved in removed:
            if item_id := item.get("id"):
                gone_ids.add(item_id)
            drop_element(item)
    for itemref in list(opf.iter(f"{{{OPF_NS}}}itemref")):
        if itemref.get("idref") in gone_ids:
            drop_element(itemref)
    # The same serializer the upgrade uses, so the declaration keeps its quoting.
    archive.replace(opf_path, serialize(etree.ElementTree(opf), original=original))


def _cut_css(archive: EpubArchive, hrefs) -> str | None:
    for href in hrefs:
        if not href.lower().endswith(".css") or href not in archive:
            continue
        text = archive.get(href).decode("utf-8", errors="replace")
        starts = [i for start in CSS_BLOCK_STARTS if (i := text.find(start)) >= 0]
        if not starts:
            continue
        archive.replace(href, text[: min(starts)].rstrip().encode("utf-8") + b"\n")
        return href
    return None
