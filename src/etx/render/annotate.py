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

from ..epub.document import ETX_ATTR, XHTML_NS, body_of, iter_blocks, text_of
from ..model import Annotation

EPUB_NS = "http://www.idpf.org/2007/ops"
EPUB_TYPE = f"{{{EPUB_NS}}}type"

MARKER_GLYPH = "*"
NOTES_DIR = "etx-notes"
NOTE_ID_PREFIX = "etx-"
REF_ID_PREFIX = "etx-ref-"


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
    """EPUB 3 footnotes, parameterised so reader quirks can be A/B tested.

    Every knob here exists because a device disagreed with the spec, not for
    generality's sake.

    Defaults are the recipe that survived device testing on Kobo (see
    DESIGN.md 6.6b): one note per file, note left visible, ``epub:type`` present.

    ``placement`` — where the note body goes
        ``"file"`` (default) gives each note its own XHTML document. This is the
        only arrangement that made Kobo preview a *single* note: its footnote
        preview does not stop at the target element, so with notes as siblings it
        rendered the tapped note and every note after it. End of file is the only
        boundary it respects.

        ``"adjacent"`` puts the note straight after its paragraph — which Kobo
        renders inline with no preview at all. ``"end"`` collects notes at the end
        of ``<body>``, which previews but concatenates. Both are kept for
        diagnosis, not for shipping.

    ``hide`` — how the note is kept out of the reading flow
        ``"visible"`` (default) leaves it in the flow. ``"css"`` applies
        ``display: none``. ``"offscreen"`` positions it off the page.

        **This is the suspected Kobo bug.** ``display: none`` generates no layout
        box, so a reader that treats the marker as an ordinary internal link has
        nowhere to scroll and falls back to the start of the book — exactly the
        observed symptom. Apple Books is unaffected because it implements the
        ``noteref`` → popup contract and never needs the note laid out.

    ``element`` — ``"aside"`` (per spec) or ``"div"``, for readers that only
        recognise one.

    ``backlink`` / ``noteref`` — some readers appear to key off these; dropping
        ``noteref`` turns the marker into a plain navigation link, which is the
        endnote fallback.
    """

    name: str = "footnote"
    placement: str = "file"
    element: str = "aside"
    hide: str = "visible"
    backlink: bool = True
    noteref: bool = True
    # populated in "file" placement; the caller adds these to the archive
    new_documents: list[tuple[str, bytes]] = field(default_factory=list)

    _PLACEMENTS = ("adjacent", "end", "file")
    _HIDES = ("visible", "css", "offscreen")
    _ELEMENTS = ("aside", "div", "span")

    def needs_epub3(self) -> bool:
        return True

    def css(self) -> str:
        rules = [
            "/* --- etx translation footnotes --- */",
            "a.etx-note {",
            "  vertical-align: super;",
            "  font-size: 0.7em;",
            "  text-decoration: none;",
            "  line-height: 0;",
            "  padding: 0 0.15em;",
            "}",
        ]
        if self.hide == "css":
            rules += [
                "/* WARNING: suspected cause of Kobo's jump-to-start behaviour —",
                "   a display:none target has no position to navigate to. */",
                ".etx-footnote { display: none; }",
            ]
        elif self.hide == "offscreen":
            rules += [
                "/* Kept in layout (so anchors resolve) but off the page. */",
                ".etx-footnote {",
                "  position: absolute;",
                "  left: -9999px;",
                "  width: 1px;",
                "  height: 1px;",
                "  overflow: hidden;",
                "}",
            ]
        else:
            rules += [
                "/* Left in the flow: conforming readers still pop it up, and",
                "   readers that don't at least render something navigable. */",
                ".etx-footnote {",
                "  font-size: 0.85em;",
                "  margin: 0.35em 0 0.6em 1.2em;",
                "  text-indent: 0;",
                "  opacity: 0.75;",
                "}",
            ]
        return "\n".join(rules) + "\n"

    def _validate(self) -> None:
        for value, allowed, label in (
            (self.placement, self._PLACEMENTS, "placement"),
            (self.hide, self._HIDES, "hide"),
            (self.element, self._ELEMENTS, "element"),
        ):
            if value not in allowed:
                raise RenderError(f"{label} must be one of {allowed}, got {value!r}")

    def apply(self, tree: etree._ElementTree, annotations: list[Annotation]) -> int:
        self._validate()
        located = self._locate(tree, annotations)
        body = body_of(tree)
        collected: list[etree._Element] = []
        doc_href = annotations[0].href if annotations else ""

        for annotation, el in located:
            note_id = f"{NOTE_ID_PREFIX}{annotation.id}"
            ref_id = f"{REF_ID_PREFIX}{annotation.id}"

            marker = etree.Element(f"{{{XHTML_NS}}}a")
            if self.placement == "file":
                # Kobo's documented trigger wants a document-plus-node href
                # ("chapter.html#id"), and a one-note-per-file target is the only
                # reliable way to stop the preview running on into the next note.
                rel, archive_path = self._note_paths(doc_href, annotation.id)
                marker.set("href", f"{rel}#{note_id}")
            else:
                marker.set("href", f"#{note_id}")
            marker.set("id", ref_id)
            marker.set("class", "etx-note")
            if self.noteref:
                marker.set(EPUB_TYPE, "noteref")
            marker.set(ETX_ATTR, "marker")
            marker.text = self.marker_glyph
            insert_at_offset(el, annotation.marker_offset, marker)

            if self.element == "span":
                # Kobo's own spec example hangs id + epub:type on an inline
                # <span>, but a span may not contain a <p> and may not sit
                # directly in <body> — so wrap it in an unmarked block. The
                # data-etx marker goes on the wrapper, since that is the whole
                # subtree the text invariant must exclude.
                note = etree.Element(f"{{{XHTML_NS}}}p")
                note.set("class", "etx-footnote")
                note.set(ETX_ATTR, "note")
                paragraph = etree.SubElement(note, f"{{{XHTML_NS}}}span")
                paragraph.set("id", note_id)
                if self.noteref:
                    paragraph.set(EPUB_TYPE, "footnote")
            else:
                note = etree.Element(f"{{{XHTML_NS}}}{self.element}")
                note.set("id", note_id)
                note.set("class", "etx-footnote")
                if self.noteref:
                    note.set(EPUB_TYPE, "footnote")
                note.set(ETX_ATTR, "note")
                paragraph = etree.SubElement(note, f"{{{XHTML_NS}}}p")

            if self.backlink:
                # A way back matters whenever the note renders as an endnote
                # rather than a popup.
                back = etree.SubElement(paragraph, f"{{{XHTML_NS}}}a")
                if self.placement == "file":
                    # The note lives in its own document, so a bare "#id" would
                    # resolve inside *that* file, where the marker's id does not
                    # exist (epubcheck RSC-012: fragment identifier is not
                    # defined). Point back at the chapter explicitly.
                    _, note_path = self._note_paths(doc_href, annotation.id)
                    back_href = posixpath.relpath(doc_href, posixpath.dirname(note_path))
                    back.set("href", f"{back_href}#{ref_id}")
                else:
                    back.set("href", f"#{ref_id}")
                if self.noteref:
                    back.set(EPUB_TYPE, "backlink")
                back.text = self.marker_glyph
                back.tail = f" {annotation.translation}"
            else:
                paragraph.text = annotation.translation

            if self.placement == "adjacent":
                # lxml's addnext() reassigns the tail to the inserted element, which
                # would silently eat the whitespace between paragraphs and break the
                # text invariant. Save and restore it explicitly.
                tail = el.tail
                el.addnext(note)
                el.tail = tail
                note.tail = None
            elif self.placement == "file":
                _, archive_path = self._note_paths(doc_href, annotation.id)
                self.new_documents.append((archive_path, self._note_document(note)))
            else:
                collected.append(note)

        # 'end' placement only: appended in document order so the notes read sensibly.
        for note in reversed(collected):
            body.append(note)
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
        # Not for the brackets themselves — for the ``data-etx`` marker attribute.
        # ``data-*`` is HTML5, which EPUB 3 permits and EPUB 2's XHTML 1.1 does
        # not, so an un-upgraded inline build fails epubcheck with
        # 'attribute "data-etx" not allowed here'. The attribute is load-bearing
        # (it is how the text-preservation invariant finds inserted nodes), so the
        # package gets upgraded rather than the marker weakened to a class.
        return True

    def css(self) -> str:
        return "span.etx-inline { font-style: italic; opacity: 0.8; }\n"

    def apply(self, tree: etree._ElementTree, annotations: list[Annotation]) -> int:
        located = self._locate(tree, annotations)
        for annotation, el in located:
            span = etree.Element(f"{{{XHTML_NS}}}span")
            span.set("class", "etx-inline")
            span.set(ETX_ATTR, "marker")
            span.text = f"{self.open_bracket}{annotation.translation}{self.close_bracket}"
            insert_at_offset(el, annotation.marker_offset, span)
        return len(located)


# --------------------------------------------------------------------- variants
#
# Named builds for device A/B testing. Kobo showed the markers but every tap
# navigated to the start of the book, while Apple Books rendered the same KEPUB
# correctly — so the disagreement is about *mechanism*, not validity, and the only
# way to settle it is to try each mechanism on the hardware.
#
# Each entry isolates one hypothesis. Build them all at once: a device round-trip
# is the expensive step, so testing one hypothesis per trip wastes cycles.

VARIANTS: dict[str, tuple[str, dict]] = {
    # the shipping default — prime hypothesis: display:none was the whole problem
    "footnote": ("EPUB 3 popup footnotes, note left in the flow", {}),
    "A-visible": (
        "aside + noteref, NO display:none  <-- prime suspect fix",
        {"hide": "visible"},
    ),
    "B-offscreen": (
        "aside + noteref, positioned off-page (keeps a layout box)",
        {"hide": "offscreen"},
    ),
    "C-div": (
        "<div> instead of <aside>, no backlink — for readers that only grok div",
        {"element": "div", "backlink": False, "hide": "visible"},
    ),
    "D-endnotes": (
        "plain in-chapter endnotes: notes at end of body, no epub:type at all",
        {"placement": "end", "noteref": False, "hide": "visible"},
    ),
    "E-inline": ("bracketed text inline — no interaction needed anywhere", {}),
    # --- round 2, after the device test showed Kobo's "Footnote preview" firing
    # only for D (no epub:type, notes at end of file). Per Kobo's published spec
    # the popup needs the target to come *after* the reference, which my
    # "adjacency" change had quietly undone — so these fill in the cells that
    # were never tried.
    "F-type-end": (
        "epub:type + notes at END of chapter  <-- the cell I never tested",
        {"placement": "end", "hide": "visible"},
    ),
    "G-span-end": (
        "epub:type on a <span> (Kobo's own documented example element), notes at end",
        {"placement": "end", "element": "span", "hide": "visible"},
    ),
    "H-file-type": (
        "ONE FILE PER NOTE + epub:type  <-- isolates the preview to a single note",
        {"placement": "file", "hide": "visible"},
    ),
    "I-file-plain": (
        "one file per note, NO epub:type (relies on Kobo's 4 documented conditions)",
        {"placement": "file", "noteref": False, "hide": "visible"},
    ),
    # the control: what we shipped when Kobo failed. Reproduces the bug on purpose.
    "Z-control-hidden": (
        "CONTROL — aside + display:none, i.e. the build that failed on Kobo",
        {"hide": "css"},
    ),
}

RENDERERS: dict[str, type[Renderer]] = {
    "footnote": FootnoteRenderer,  # type: ignore[dict-item]
    "inline": InlineRenderer,  # type: ignore[dict-item]
}


def get_renderer(name: str) -> Renderer:
    """Build a renderer by name, accepting either a base name or a variant key."""
    if name in RENDERERS:
        return RENDERERS[name]()  # type: ignore[abstract]
    if name in VARIANTS:
        _, options = VARIANTS[name]
        if name == "E-inline":
            return InlineRenderer()
        return FootnoteRenderer(**options)  # type: ignore[arg-type]
    raise RenderError(
        f"unknown renderer {name!r} — choose from "
        f"{sorted(set(RENDERERS) | set(VARIANTS))}"
    )
