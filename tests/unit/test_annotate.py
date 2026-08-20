"""Insertion mechanics. The text invariant must hold by construction."""

from __future__ import annotations

import pytest
from lxml import etree

from joven.epub.document import XHTML_NS, document_text, parse, serialize, text_of
from joven.model import Annotation
from joven.render.annotate import (
    FootnoteRenderer,
    InlineRenderer,
    RenderError,
    get_renderer,
    insert_at_offset,
)

XML_DECL = "<?xml version='1.0' encoding='utf-8'?>"


def _html(body: str) -> bytes:
    return f"""{XML_DECL}
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>t</title></head>
  <body>{body}</body>
</html>
""".encode()


def _marker() -> etree._Element:
    el = etree.Element(f"{{{XHTML_NS}}}a")
    el.set("data-joven", "marker")
    el.text = "*"
    return el


# ------------------------------------------------------------ insert_at_offset


@pytest.mark.parametrize("offset", range(0, 12))
def test_insertion_at_every_offset_preserves_text(offset: int) -> None:
    """Exhaustive: splitting a text node must never change the text."""
    source = "Hola mundo."
    root = etree.fromstring(f'<p xmlns="{XHTML_NS}">{source}</p>'.encode())
    insert_at_offset(root, offset, _marker())
    assert text_of(root, exclude_inserted=True) == source


def test_insertion_splits_at_the_right_place() -> None:
    root = etree.fromstring(f'<p xmlns="{XHTML_NS}">Cuántos años tienes? he said.</p>'.encode())
    insert_at_offset(root, len("Cuántos años tienes?"), _marker())
    assert root.text == "Cuántos años tienes?"
    assert root[0].tail == " he said."


def test_insertion_past_end_appends() -> None:
    root = etree.fromstring(f'<p xmlns="{XHTML_NS}">Se fué.</p>'.encode())
    insert_at_offset(root, 999, _marker())
    assert text_of(root, exclude_inserted=True) == "Se fué."
    assert root[-1].get("data-joven") == "marker"


def test_insertion_descends_into_child_elements() -> None:
    root = etree.fromstring(
        f'<p xmlns="{XHTML_NS}">before <i>inner text</i> after</p>'.encode()
    )
    original = text_of(root, exclude_inserted=False)
    insert_at_offset(root, len("before inn"), _marker())
    assert text_of(root, exclude_inserted=True) == original
    # landed inside the <i>, not as a sibling
    assert root.find(f"{{{XHTML_NS}}}i")[0].get("data-joven") == "marker"


def test_insertion_into_child_tail() -> None:
    root = etree.fromstring(f'<p xmlns="{XHTML_NS}">a<i>b</i>cdef</p>'.encode())
    original = text_of(root, exclude_inserted=False)
    insert_at_offset(root, 4, _marker())
    assert text_of(root, exclude_inserted=True) == original


def test_negative_offset_rejected() -> None:
    root = etree.fromstring(f'<p xmlns="{XHTML_NS}">x</p>'.encode())
    with pytest.raises(RenderError, match="negative"):
        insert_at_offset(root, -1, _marker())


# --------------------------------------------------------------- renderers


def _annotation(text: str, translation: str, *, index: int, spans=None) -> Annotation:
    return Annotation.create(
        href="OEBPS/part1.xhtml",
        para_index=index,
        source_text=text,
        spans=spans or [(0, len(text))],
        translation=translation,
    )


def test_footnote_renderer_emits_noteref_and_a_separate_note_document() -> None:
    """Default recipe: marker in the chapter, note in its own document."""
    data = _html('<p class="calibre4">Vaya con Dios.</p>')
    tree = parse(data)
    engine = FootnoteRenderer()
    count = engine.apply(tree, [_annotation("Vaya con Dios.", "Go with God.", index=1)])
    assert count == 1

    out = serialize(tree, original=data).decode()
    assert 'epub:type="noteref"' in out
    assert "joven-notes/" in out, "marker should link to the note document"
    assert "Go with God." not in out, "the note body belongs in its own file"

    assert len(engine.new_documents) == 1
    _, body = engine.new_documents[0]
    note = body.decode()
    assert 'epub:type="footnote"' in note
    assert "<aside" in note
    assert "Go with God." in note
    # the prose itself is untouched
    assert document_text(serialize(tree, original=data), exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )


def test_footnote_ids_link_up_across_files() -> None:
    """Marker -> note document -> back to the marker, all resolvable."""
    data = _html("<p>Se fué.</p>")
    tree = parse(data)
    engine = FootnoteRenderer()
    engine.apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])

    ref = next(el for el in tree.getroot().iter() if el.get("data-joven") == "marker")
    doc_part, _, fragment = ref.get("href").partition("#")
    assert doc_part.startswith("joven-notes/")

    note_root = parse(engine.new_documents[0][1]).getroot()
    note = next(el for el in note_root.iter() if el.get("data-joven") == "note")
    assert note.get("id") == fragment

    # the backlink needs a path back, not a bare fragment (epubcheck RSC-012)
    backlink = note.find(f".//{{{XHTML_NS}}}a")
    assert backlink.get("href") == f"../part1.xhtml#{ref.get('id')}"


def test_inline_renderer_brackets_the_translation() -> None:
    data = _html("<p>Se fué.</p>")
    tree = parse(data)
    InlineRenderer().apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])
    out = serialize(tree, original=data)
    assert "[He is gone.]" in out.decode()
    assert document_text(out, exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )


def test_marker_lands_before_the_english_tag() -> None:
    """Pattern B: the marker belongs at the end of the Spanish span."""
    source = "Cuántos años tienes? the old man said."
    data = _html(f"<p>{source}</p>")
    tree = parse(data)
    annotation = Annotation.create(
        href="OEBPS/part1.xhtml",
        para_index=1,
        source_text=source,
        spans=[(0, 20)],
        translation="How old are you?",
    )
    FootnoteRenderer().apply(tree, [annotation])
    out = serialize(tree, original=data).decode()
    assert "Cuántos años tienes?<a" in out
    assert "</a> the old man said." in out


def test_multi_span_paragraph_marker_goes_after_last_span() -> None:
    source = "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad."
    data = _html(f"<p>{source}</p>")
    tree = parse(data)
    annotation = Annotation.create(
        href="OEBPS/part1.xhtml",
        para_index=1,
        source_text=source,
        spans=[(0, 17), (27, 41), (42, len(source))],
        translation="Listen to me, young man, he said. I know nothing. That is the truth.",
    )
    assert annotation.marker_offset == len(source)
    FootnoteRenderer().apply(tree, [annotation])
    assert document_text(serialize(tree, original=data), exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )


def test_several_annotations_in_one_document() -> None:
    data = _html("<p>Se fué.</p><p>English here.</p><p>Está bien.</p>")
    tree = parse(data)
    count = FootnoteRenderer().apply(
        tree,
        [
            _annotation("Se fué.", "He is gone.", index=1),
            _annotation("Está bien.", "That's fine.", index=3),
        ],
    )
    assert count == 2
    out = serialize(tree, original=data)
    assert document_text(out, exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )
    assert out.decode().count('epub:type="noteref"') == 2


def test_blank_paragraphs_do_not_shift_addresses() -> None:
    """Index 3 must be the third *block*, blank or not."""
    data = _html("<p>One.</p><p>   </p><p>Está bien.</p>")
    tree = parse(data)
    FootnoteRenderer().apply(tree, [_annotation("Está bien.", "That's fine.", index=3)])
    out = serialize(tree, original=data).decode()
    assert "Está bien.<a" in out


def test_missing_paragraph_is_a_clear_error() -> None:
    tree = parse(_html("<p>Only one.</p>"))
    with pytest.raises(RenderError, match="no block #9"):
        FootnoteRenderer().apply(tree, [_annotation("Only one.", "x", index=9)])


def test_drifted_source_text_is_rejected() -> None:
    """A sidecar built against a different edition must fail loudly."""
    tree = parse(_html("<p>Different text entirely.</p>"))
    with pytest.raises(ValueError, match="drifted"):
        FootnoteRenderer().apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])


def test_span_outside_paragraph_is_rejected() -> None:
    source = "Se fué."
    tree = parse(_html(f"<p>{source}</p>"))
    annotation = Annotation.create(
        href="OEBPS/part1.xhtml",
        para_index=1,
        source_text=source,
        spans=[(0, 500)],
        translation="x",
    )
    with pytest.raises(ValueError, match="outside"):
        FootnoteRenderer().apply(tree, [annotation])


def test_get_renderer_rejects_unknown_style() -> None:
    with pytest.raises(RenderError, match="unknown renderer"):
        get_renderer("nope")


def test_renderer_registry() -> None:
    # Both need EPUB 3: the footnote renderer for epub:type, the inline renderer
    # for the data-joven marker attribute (HTML5-only).
    assert get_renderer("footnote").needs_epub3() is True
    assert get_renderer("inline").needs_epub3() is True


# --------------------------------------------------------- the shipping recipe


def test_note_insertion_preserves_inter_paragraph_whitespace() -> None:
    """lxml reassigns tails on insertion — regression guard for that."""
    data = _html("<p>Se fué.</p>\n      <p>Next.</p>")
    tree = parse(data)
    FootnoteRenderer().apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])
    assert document_text(serialize(tree, original=data), exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )


def test_every_renderer_builds_and_preserves_text() -> None:
    """Whatever the renderer, the prose invariant is non-negotiable."""
    from joven.render.annotate import RENDERERS

    data = _html("<p>Se fué.</p>\n      <p>English.</p>\n      <p>Está bien.</p>")
    for key in RENDERERS:
        tree = parse(data)
        engine = get_renderer(key)
        engine.apply(
            tree,
            [
                _annotation("Se fué.", "He is gone.", index=1),
                _annotation("Está bien.", "That's fine.", index=3),
            ],
        )
        out = serialize(tree, original=data)
        assert document_text(out, exclude_inserted=True) == document_text(
            data, exclude_inserted=False
        ), key
        assert engine.css(), f"{key} produced no CSS"


def test_footnote_css_never_hides_the_note() -> None:
    """The Kobo bug this recipe exists to avoid.

    A ``display: none`` target generates no layout box, so a reader that treats
    the marker as an ordinary internal link has nowhere to scroll and falls back
    to the start of the book. Device-confirmed; see DESIGN.md §6.6b.
    """
    assert "display: none" not in get_renderer("footnote").css()


def test_each_note_becomes_its_own_document() -> None:
    """One note per file is what stops Kobo's preview running into the next note."""
    data = _html("<p>Se fué.</p>\n      <p>English.</p>\n      <p>Está bien.</p>")
    tree = parse(data)
    engine = FootnoteRenderer()
    engine.apply(
        tree,
        [
            _annotation("Se fué.", "He is gone.", index=1),
            _annotation("Está bien.", "That's fine.", index=3),
        ],
    )
    assert len(engine.new_documents) == 2
    for _, body in engine.new_documents:
        assert body.decode().count('data-joven="note"') == 1


def test_note_carries_the_conforming_reader_contract() -> None:
    """noteref on the marker, footnote on the aside, backlink on the way back."""
    data = _html("<p>Se fué.</p>")
    tree = parse(data)
    engine = FootnoteRenderer()
    engine.apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])

    assert 'epub:type="noteref"' in serialize(tree, original=data).decode()
    note = engine.new_documents[0][1].decode()
    assert "<aside" in note
    assert 'epub:type="footnote"' in note
    assert 'epub:type="backlink"' in note
    assert "He is gone." in note


def test_inline_renderer_also_needs_epub3() -> None:
    """Not for the brackets — for the ``data-joven`` marker.

    ``data-*`` is HTML5. EPUB 2's XHTML 1.1 rejects it, so an un-upgraded inline
    build fails epubcheck with 'attribute "data-joven" not allowed here'.
    """
    assert get_renderer("inline").needs_epub3() is True
