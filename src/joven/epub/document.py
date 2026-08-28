"""XHTML parsing, text extraction, and the text-preservation invariant.

Every element this tool inserts carries a ``data-joven`` attribute. That marker is
what makes the core invariant checkable: strip the inserted nodes back out and
the remaining text must be *byte-identical* to the original. Matching on an
explicit attribute rather than on class names keeps that check unambiguous.

Two marker kinds, because they differ in how their tail text is treated:

``data-joven="marker"``
    An inline noteref spliced into the middle of a paragraph. Its ``tail`` is
    original prose that followed the split point, so the tail is **kept**.

``data-joven="note"``
    A whole ``<aside>`` footnote body appended to the document. Both the element
    and its ``tail`` are ours, so the tail is **dropped**.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from html.entities import html5

from lxml import etree

from .archive import EpubError

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
JOVEN_ATTR = "data-joven"

# The XML declaration and any newline that follows it, captured verbatim so the
# output matches the input's style byte for byte. `[^>]*` is safe: a declaration
# cannot contain ">" before its "?>".
_XML_DECL = re.compile(rb"\A\s*<\?xml[^>]*\?>(?:\r\n|\n)?")

NSMAP = {"x": XHTML_NS}


class DocumentError(EpubError):
    """Raised when an XHTML document cannot be parsed.

    An :class:`~joven.epub.archive.EpubError` because it means the same thing to
    the caller: this file cannot be used. The CLI reports both the same way.
    """


# ------------------------------------------------------- HTML named entities

# XML defines exactly five named entities. XHTML documents in the wild use the
# full HTML set -- `&nbsp;`, `&mdash;`, `&rsquo;` -- and are then not well-formed
# XML unless they declare them, which publishers routinely don't bother to do.
# The Knopf edition this tool was built against happens to use none of them; most
# EPUBs use several, and lxml answers with `Entity 'nbsp' not defined`.
#
# So resolve the HTML names to their characters before parsing. Doing it here
# rather than at a call site matters for the text-preservation invariant: the
# original and the annotated output are both read back through this function, so
# whatever a `&nbsp;` becomes, it becomes on both sides and the comparison stays
# honest.
_XML_BUILTIN = frozenset({"amp", "lt", "gt", "quot", "apos"})
_NAMED_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
# A replacement that is itself XML markup has to stay an entity reference, or
# resolving `&AMP;` would inject a bare `&` and break the parse it was meant to fix.
_REMAINS_MARKUP = {"&": b"&amp;", "<": b"&lt;", ">": b"&gt;"}


@lru_cache(maxsize=1)
def _entity_table() -> dict[bytes, bytes]:
    """HTML entity name -> replacement bytes, minus the five XML already knows."""
    table: dict[bytes, bytes] = {}
    for name, char in html5.items():
        if not name.endswith(";") or name[:-1] in _XML_BUILTIN:
            continue
        table[name[:-1].encode("ascii")] = b"".join(
            _REMAINS_MARKUP.get(ch) or ch.encode("utf-8") for ch in char
        )
    return table


def resolve_named_entities(data: bytes) -> bytes:
    """Replace HTML named entity references with the characters they stand for.

    Numeric references (``&#160;``) are already valid XML and are left alone, as
    is any name we don't recognise -- a document declaring its own entities in a
    DOCTYPE keeps them, and an undeclared one still fails loudly.
    """
    table = _entity_table()

    def substitute(match: re.Match[bytes]) -> bytes:
        return table.get(match.group(1), match.group(0))

    return _NAMED_ENTITY.sub(substitute, data)


def parse(data: bytes) -> etree._ElementTree:
    """Parse an XHTML document, preserving structure as faithfully as possible."""
    parser = etree.XMLParser(
        resolve_entities=False,
        remove_blank_text=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False,
    )
    try:
        return etree.ElementTree(etree.fromstring(resolve_named_entities(data), parser=parser))
    except etree.XMLSyntaxError as exc:
        raise DocumentError(f"malformed XHTML: {exc}") from exc


def serialize(tree: etree._ElementTree, *, original: bytes | None = None) -> bytes:
    """Serialize back to bytes, matching the original XML declaration style.

    Publishers differ in how they write the declaration, and both details matter
    for keeping the output diff honest:

    * **Quoting.** Calibre writes ``<?xml version='1.0' encoding='utf-8'?>`` with
      single quotes; lxml emits double. Cosmetic, but a spurious diff on every
      document hides the real changes.
    * **Whether a newline follows it.** Calibre puts the declaration on its own
      line. The Knopf edition of this book puts it on the *same* line as the
      ``<html>`` open tag.

    That second case broke an earlier version of this function, which took
    "everything before the first newline" as the declaration. On a file with no
    newline after ``?>`` that swallowed the entire ``<html …>`` open tag and
    prepended it to a body that already had one — producing two ``<html>`` opens
    and one close, on all 14 documents:

        DocumentError: malformed XHTML: Premature end of data in tag html line 1

    So match the declaration exactly, and carry its trailing newline only if it
    had one.
    """
    body = etree.tostring(tree, xml_declaration=False, encoding="utf-8")
    decl = b"<?xml version='1.0' encoding='utf-8'?>\n"
    if original is not None:
        found = _XML_DECL.match(original)
        if found:
            decl = found.group(0)
    return decl + body


def _is_inserted(el: etree._Element) -> str | None:
    """Return the ``data-joven`` kind for an element, or None if not ours."""
    return el.get(JOVEN_ATTR)


def text_of(el: etree._Element, *, exclude_inserted: bool = True) -> str:
    """Concatenate all text under ``el`` in document order.

    With ``exclude_inserted`` (the default) this reconstructs the *original*
    text: inserted subtrees are skipped, and tail handling follows the marker
    kind documented at the top of this module.
    """
    parts: list[str] = []

    def walk(node: etree._Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            if not isinstance(child.tag, str):  # comment / PI
                if child.tail:
                    parts.append(child.tail)
                continue
            kind = _is_inserted(child) if exclude_inserted else None
            if kind is None:
                walk(child)
                if child.tail:
                    parts.append(child.tail)
            elif kind == "marker":
                # our element goes, the prose that followed it stays
                if child.tail:
                    parts.append(child.tail)
            else:  # "note" — element and its tail are both ours
                pass

    walk(el)
    return "".join(parts)


def document_text(data: bytes, *, exclude_inserted: bool = True) -> str:
    """Extract the full text of an XHTML document (body only if present)."""
    tree = parse(data)
    root = tree.getroot()
    body = root.find(f"{{{XHTML_NS}}}body")
    return text_of(body if body is not None else root, exclude_inserted=exclude_inserted)


@dataclass(frozen=True, slots=True)
class TextUnit:
    """An addressable block of prose — the unit detection operates on."""

    href: str
    index: int
    text: str
    tag: str

    @property
    def address(self) -> str:
        return f"{self.href}#{self.index}"


BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li", "div")


def body_of(tree: etree._ElementTree) -> etree._Element:
    root = tree.getroot()
    body = root.find(f"{{{XHTML_NS}}}body")
    return body if body is not None else root


def iter_blocks(tree: etree._ElementTree) -> Iterator[tuple[int, etree._Element]]:
    """Yield ``(index, element)`` for every leaf block, in document order.

    **This is the single definition of paragraph addressing.** Extraction and
    rendering must agree on what ``part3.xhtml#595`` means, so both go through
    here. Indices count *all* leaf blocks including whitespace-only ones, so
    addresses stay stable even though empty blocks are never annotated.

    Blocks containing other blocks are skipped, so a wrapper ``<div>`` doesn't
    shadow the paragraphs inside it.
    """
    wanted = {f"{{{XHTML_NS}}}{t}" for t in BLOCK_TAGS}
    index = 0
    for el in body_of(tree).iter():
        if el.tag not in wanted:
            continue
        if any(child.tag in wanted for child in el.iter() if child is not el):
            continue  # container, not a leaf block
        index += 1
        yield index, el


def iter_text_units(data: bytes, href: str) -> list[TextUnit]:
    """Extract non-empty block-level text units in document order."""
    tree = parse(data)
    return [
        TextUnit(href=href, index=index, text=text, tag=etree.QName(el).localname)
        for index, el in iter_blocks(tree)
        if (text := text_of(el)).strip()
    ]
