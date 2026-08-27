"""Undo a source EPUB's existing Kobo conversion so we can annotate clean XHTML.

Most EPUBs arrive as plain XHTML. Some arrive **already converted for a Kobo** —
by kepubify, or (as with the Knopf *The Crossing*) by Calibre's KePub plugin,
which wraps every sentence in ``<span class="koboSpan">``.

Annotating such a book in place looks correct in every file-level check and then
**loses text on the device**. Kobo treats a koboSpan as a leaf text unit: give one
mixed content and it renders the child element and drops the span's own text. A
footnote marker inserted inside ``kobo.681.1`` therefore renders as a bare
asterisk with the Spanish gone::

    <span class="koboSpan" id="kobo.681.1">Cuántos años tienes?<a ...>*</a> </span>

kepubify's own placement puts the marker *outside* the span, with a span of its
own — which is what the reader needs::

    <span class="koboSpan" id="kobo.1.2">Cuántos años tienes?</span><a ...><span
      class="koboSpan" id="kobo.1.3">*</span></a>

kepubify will not repair the first form: it skips documents that already carry
koboSpans, so re-running it over our output is not a fix. The only reliable route
is to strip the Kobo layer first, annotate ordinary XHTML, and let kepubify wrap
the result — the same path every non-kepubified book already takes.

Unwrapping is safe because a koboSpan carries no meaning of its own: it adds
markup, never text. Verified across all 19 documents of the Knopf edition, the
concatenated text of every document is **identical** before and after unwrapping,
which is also why an existing sidecar's character offsets stay valid.

``js/kobo.js`` is deliberately left in the archive. It becomes unreferenced, but
the manifest still declares it, so epubcheck is satisfied — and dropping entries
would violate the "no entry may disappear" invariant for no benefit.
"""

from __future__ import annotations

from lxml import etree

from ..epub.archive import EpubArchive
from ..epub.document import parse, serialize

XHTML_NS = "http://www.w3.org/1999/xhtml"

#: Wrapper divs kepubify and the Calibre plugin add around the body content.
_WRAPPER_IDS = {"book-columns", "book-inner"}


def _unwrap(element: etree._Element) -> None:
    """Replace ``element`` with its own text and children, preserving both tails."""
    parent = element.getparent()
    index = parent.index(element)
    previous = element.getprevious()

    def absorb(text: str | None) -> None:
        nonlocal previous
        if not text:
            return
        if previous is not None:
            previous.tail = (previous.tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    absorb(element.text)
    for offset, child in enumerate(list(element)):
        parent.insert(index + offset, child)
        previous = child
    absorb(element.tail)
    parent.remove(element)


def _drop(element: etree._Element) -> None:
    """Remove ``element`` entirely, but never the text that followed it."""
    parent = element.getparent()
    if element.tail:
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + element.tail
        else:
            parent.text = (parent.text or "") + element.tail
    parent.remove(element)


def is_kepubified(archive: EpubArchive) -> bool:
    """True if any document already carries Kobo conversion markup."""
    return any(b"koboSpan" in archive.get(href) for href in archive.xhtml_names())


def normalize_document(tree: etree._ElementTree) -> int:
    """Strip Kobo conversion markup from one parsed document.

    Returns the number of nodes removed, so a caller can tell a normalized
    document from an untouched one.
    """
    removed = 0
    for span in list(tree.iter(f"{{{XHTML_NS}}}span")):
        if span.get("class") == "koboSpan":
            _unwrap(span)
            removed += 1
    for div in list(tree.iter(f"{{{XHTML_NS}}}div")):
        if div.get("id") in _WRAPPER_IDS:
            _unwrap(div)
            removed += 1
    for style in list(tree.iter(f"{{{XHTML_NS}}}style")):
        if style.get("class") == "kobostylehacks":
            _drop(style)
            removed += 1
    for script in list(tree.iter(f"{{{XHTML_NS}}}script")):
        if (script.get("src") or "").endswith("kobo.js"):
            _drop(script)
            removed += 1
    return removed


def dekepubify(archive: EpubArchive) -> list[str]:
    """Normalize every document in ``archive``; return the hrefs changed."""
    normalized: list[str] = []
    for href in archive.xhtml_names():
        original = archive.get(href)
        if b"koboSpan" not in original and b"kobo" not in original:
            continue
        tree = parse(original)
        if normalize_document(tree):
            archive.replace(href, serialize(tree, original=original))
            normalized.append(href)
    return normalized
