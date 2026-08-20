"""Insert annotations into XHTML, preserving the original prose exactly.

The one hard requirement: strip the inserted nodes back out and the text must be
byte-identical to the original (see ``verify.check_text_preserved``). Everything
here is arranged so that property holds by construction — we only ever *split* an
existing text node, never rewrite it.

Two strategies:

``FootnoteRenderer``
    The real target. An inline ``<a epub:type="noteref">`` marker plus an
    ``<aside epub:type="footnote">`` body that conforming readers show as a popup.

``InlineRenderer``
    ``Vaya con Dios. [Go with God.]`` — universal, ugly, and invaluable: it
    exercises the same insertion machinery with none of the EPUB 3 surface, so a
    failure here is unambiguously an insertion bug.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Protocol

from lxml import etree

from ..epub.document import JOVEN_ATTR, XHTML_NS, iter_blocks, text_of
from ..model import Annotation

EPUB_NS = "http://www.idpf.org/2007/ops"
EPUB_TYPE = f"{{{EPUB_NS}}}type"

MARKER_GLYPH = "*"
NOTES_DIR = "joven-notes"
NOTE_ID_PREFIX = "joven-"
REF_ID_PREFIX = "joven-ref-"


class RenderError(Exception):
    """Raised when an annotation cannot be applied to the document."""


def insert_at_offset(el: etree._Element, offset: int, new: etree._Element) -> None:
    """Splice ``new`` into ``el`` at character ``offset`` of ``el``'s text.

    Walks text nodes in document order, accounting for child subtrees and tails,
    and splits the text node that contains the offset. An offset at or past the
    end appends.

    The split is why the text invariant holds: ``"abc"`` becomes
    ``"a" + <new/> + "bc"``, and concatenating text while skipping ``new``
    reproduces ``"abc"``.
    """
    if offset < 0:
        raise RenderError(f"negative offset {offset}")

    pos = 0
    if el.text:
        if pos + len(el.text) >= offset:
            cut = offset - pos
            el.insert(0, new)
            new.tail = el.text[cut:] or None
            el.text = el.text[:cut] or None
            return
        pos += len(el.text)

    for index, child in enumerate(list(el)):
        if isinstance(child.tag, str):
            inner = len(text_of(child, exclude_inserted=False))
            if pos + inner > offset:
                insert_at_offset(child, offset - pos, new)
                return
            pos += inner
        if child.tail:
            if pos + len(child.tail) >= offset:
                cut = offset - pos
                el.insert(index + 1, new)
                new.tail = child.tail[cut:] or None
                child.tail = child.tail[:cut] or None
                return
            pos += len(child.tail)

    el.append(new)


class Renderer(Protocol):
    """Applies annotations to a parsed XHTML tree."""

    name: str

    def needs_epub3(self) -> bool: ...

    def css(self) -> str:
        """Stylesheet rules this renderer needs appended to the book."""
        ...

    def apply(self, tree: etree._ElementTree, annotations: list[Annotation]) -> int: ...


@dataclass(slots=True)
class _Base:
    marker_glyph: str = MARKER_GLYPH

    def _locate(
        self, tree: etree._ElementTree, annotations: list[Annotation]
    ) -> list[tuple[Annotation, etree._Element]]:
        """Match annotations to their paragraphs, validating as we go."""
        blocks = dict(iter_blocks(tree))
        located = []
        for annotation in annotations:
            el = blocks.get(annotation.para_index)
            if el is None:
                raise RenderError(
                    f"{annotation.id}: no block #{annotation.para_index} in {annotation.href}"
                )
            annotation.validate_against(text_of(el))
            located.append((annotation, el))
        # Descending offset so an earlier insertion never shifts a later one.
        # With one footnote per paragraph this is belt-and-braces, but it makes
        # multiple markers per paragraph safe if we ever want them.
        located.sort(key=lambda pair: (pair[0].para_index, pair[0].marker_offset), reverse=True)
        return located


@dataclass(slots=True)
class FootnoteRenderer(_Base):
    """EPUB 3 popup footnotes, in the arrangement Kobo actually honours.

    The shape is not a matter of taste — it is what a device test settled
    (DESIGN.md §6.6b), and each part of it fixes an observed failure:

    **One note per file.** Kobo's footnote preview does not stop at the target
    element. With the notes as siblings in the chapter it rendered the tapped note
    *and every note after it*; end of file is the only boundary it respects.

    **The note stays visible in the flow.** ``display: none`` generates no layout
    box, so a reader that treats the marker as an ordinary internal link has
    nowhere to scroll and falls back to the start of the book — which is exactly
    what Kobo did. Apple Books was unaffected because it implements the
    ``noteref`` → popup contract and never needs the note laid out.

    **``epub:type`` on both ends.** ``noteref`` on the marker, ``footnote`` on the
    ``<aside>``, ``backlink`` on the way back — the conforming-reader contract, and
    the back-link is what makes the endnote fallback usable when it is not honoured.
    """

    name: str = "footnote"
    # populated as notes are rendered; the caller adds these to the archive
    new_documents: list[tuple[str, bytes]] = field(default_factory=list)

    def needs_epub3(self) -> bool:
        return True

    def css(self) -> str:
        return (
            "/* --- joven translation footnotes --- */\n"
            "a.joven-note {\n"
            "  vertical-align: super;\n"
            "  font-size: 0.7em;\n"
            "  text-decoration: none;\n"
            "  line-height: 0;\n"
            "  padding: 0 0.15em;\n"
            "}\n"
            "/* Left in the flow: conforming readers still pop it up, and\n"
            "   readers that don't at least render something navigable. */\n"
            ".joven-footnote {\n"
            "  font-size: 0.85em;\n"
            "  margin: 0.35em 0 0.6em 1.2em;\n"
            "  text-indent: 0;\n"
            "  opacity: 0.75;\n"
            "}\n"
        )

    def apply(self, tree: etree._ElementTree, annotations: list[Annotation]) -> int:
        located = self._locate(tree, annotations)
        doc_href = annotations[0].href if annotations else ""

        for annotation, el in located:
            note_id = f"{NOTE_ID_PREFIX}{annotation.id}"
            ref_id = f"{REF_ID_PREFIX}{annotation.id}"
            rel, archive_path = self._note_paths(doc_href, annotation.id)

            # Kobo's documented trigger wants a document-plus-node href
            # ("notes/joven-abc.xhtml#id"), not a bare fragment.
            marker = etree.Element(f"{{{XHTML_NS}}}a")
            marker.set("href", f"{rel}#{note_id}")
            marker.set("id", ref_id)
            marker.set("class", "joven-note")
            marker.set(EPUB_TYPE, "noteref")
            marker.set(JOVEN_ATTR, "marker")
            marker.text = self.marker_glyph
            insert_at_offset(el, annotation.marker_offset, marker)

            note = etree.Element(f"{{{XHTML_NS}}}aside")
            note.set("id", note_id)
            note.set("class", "joven-footnote")
            note.set(EPUB_TYPE, "footnote")
            note.set(JOVEN_ATTR, "note")
            paragraph = etree.SubElement(note, f"{{{XHTML_NS}}}p")

            # A way back matters whenever the note renders as an endnote rather
            # than a popup. The note lives in its own document, so a bare "#id"
            # would resolve inside *that* file, where the marker's id does not
            # exist (epubcheck RSC-012). Point back at the chapter explicitly.
            back = etree.SubElement(paragraph, f"{{{XHTML_NS}}}a")
            back_href = posixpath.relpath(doc_href, posixpath.dirname(archive_path))
            back.set("href", f"{back_href}#{ref_id}")
            back.set(EPUB_TYPE, "backlink")
            back.text = self.marker_glyph
            back.tail = f" {annotation.translation}"

            self.new_documents.append((archive_path, self._note_document(note)))

        return len(located)

    @staticmethod
    def _note_paths(doc_href: str, annotation_id: str) -> tuple[str, str]:
        """Return (href relative to the chapter, path inside the archive)."""
        directory = posixpath.dirname(doc_href)
        filename = f"{NOTE_ID_PREFIX}{annotation_id}.xhtml"
        relative = posixpath.join(NOTES_DIR, filename)
        archive_path = posixpath.join(directory, relative) if directory else relative
        return relative, archive_path

    def _note_document(self, note: etree._Element) -> bytes:
        """Wrap one note in a minimal standalone XHTML document.

        One note per file is the point: Kobo's footnote preview does not stop at
        the target element — with all notes as siblings it rendered the tapped note
        *and every note after it*. End of file is a boundary it must respect.
        """
        html = etree.Element(
            f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": EPUB_NS}
        )
        head = etree.SubElement(html, f"{{{XHTML_NS}}}head")
        etree.SubElement(head, f"{{{XHTML_NS}}}title").text = "Note"
        body = etree.SubElement(html, f"{{{XHTML_NS}}}body")
        body.append(note)
        return (
            b"<?xml version='1.0' encoding='utf-8'?>\n"
            + etree.tostring(html, xml_declaration=False, encoding="utf-8")
        )


@dataclass(slots=True)
class InlineRenderer(_Base):
    """``... [translation]`` inline. Debug/diff view; works in any reader."""

    name: str = "inline"
    open_bracket: str = " ["
    close_bracket: str = "]"

    def needs_epub3(self) -> bool:
        # Not for the brackets themselves — for the ``data-joven`` marker attribute.
        # ``data-*`` is HTML5, which EPUB 3 permits and EPUB 2's XHTML 1.1 does
        # not, so an un-upgraded inline build fails epubcheck with
        # 'attribute "data-joven" not allowed here'. The attribute is load-bearing
        # (it is how the text-preservation invariant finds inserted nodes), so the
        # package gets upgraded rather than the marker weakened to a class.
        return True

    def css(self) -> str:
        return "span.joven-inline { font-style: italic; opacity: 0.8; }\n"

    def apply(self, tree: etree._ElementTree, annotations: list[Annotation]) -> int:
        located = self._locate(tree, annotations)
        for annotation, el in located:
            span = etree.Element(f"{{{XHTML_NS}}}span")
            span.set("class", "joven-inline")
            span.set(JOVEN_ATTR, "marker")
            span.text = f"{self.open_bracket}{annotation.translation}{self.close_bracket}"
            insert_at_offset(el, annotation.marker_offset, span)
        return len(located)


RENDERERS: dict[str, type[Renderer]] = {
    "footnote": FootnoteRenderer,  # type: ignore[dict-item]
    "inline": InlineRenderer,  # type: ignore[dict-item]
}


def get_renderer(name: str) -> Renderer:
    if name in RENDERERS:
        return RENDERERS[name]()  # type: ignore[abstract]
    raise RenderError(f"unknown renderer {name!r} — choose from {sorted(RENDERERS)}")
