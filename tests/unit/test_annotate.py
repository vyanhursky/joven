"""Insertion mechanics. The text invariant must hold by construction."""

from __future__ import annotations

import pytest
from lxml import etree

from etx.epub.document import XHTML_NS, document_text, parse, serialize, text_of
from etx.model import Annotation
from etx.render.annotate import (
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
    el.set("data-etx", "marker")
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
    assert root[-1].get("data-etx") == "marker"


def test_insertion_descends_into_child_elements() -> None:
    root = etree.fromstring(
        f'<p xmlns="{XHTML_NS}">before <i>inner text</i> after</p>'.encode()
    )
    original = text_of(root, exclude_inserted=False)
    insert_at_offset(root, len("before inn"), _marker())
    assert text_of(root, exclude_inserted=True) == original
    # landed inside the <i>, not as a sibling
    assert root.find(f"{{{XHTML_NS}}}i")[0].get("data-etx") == "marker"


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
    assert "etx-notes/" in out, "marker should link to the note document"
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

    ref = next(el for el in tree.getroot().iter() if el.get("data-etx") == "marker")
    doc_part, _, fragment = ref.get("href").partition("#")
    assert doc_part.startswith("etx-notes/")

    note_root = parse(engine.new_documents[0][1]).getroot()
    note = next(el for el in note_root.iter() if el.get("data-etx") == "note")
    assert note.get("id") == fragment

    # the backlink needs a path back, not a bare fragment (epubcheck RSC-012)
    backlink = note.find(f".//{{{XHTML_NS}}}a")
    assert backlink.get("href") == f"../part1.xhtml#{ref.get('id')}"


def test_adjacent_placement_keeps_the_note_in_the_same_tree() -> None:
    data = _html("<p>Se fué.</p>")
    tree = parse(data)
    engine = FootnoteRenderer(placement="adjacent")
    engine.apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])
    assert not engine.new_documents
    assert "He is gone." in serialize(tree, original=data).decode()


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
    # for the data-etx marker attribute (HTML5-only).
    assert get_renderer("footnote").needs_epub3() is True
    assert get_renderer("inline").needs_epub3() is True


# ------------------------------------------------------- aside placement (Kobo)


def test_aside_lands_immediately_after_its_paragraph() -> None:  # noqa: D401
    """Adjacent placement still works mechanically.

    NB: adjacency is no longer believed to help Kobo — device testing showed an
    adjacent note renders inline with no popup. Kept because it is a valid
    placement and the insertion mechanics must stay correct.
    """
    data = _html("<p>Se fué.</p><p>Next paragraph.</p>")
    tree = parse(data)
    FootnoteRenderer(placement="adjacent").apply(
        tree, [_annotation("Se fué.", "He is gone.", index=1)]
    )

    body = tree.getroot().find(f"{{{XHTML_NS}}}body")
    kinds = [etree.QName(el).localname for el in body]
    assert kinds == ["p", "aside", "p"], kinds


def test_adjacent_placement_preserves_inter_paragraph_whitespace() -> None:
    """lxml's addnext() steals the tail — regression guard for that."""
    data = _html("<p>Se fué.</p>\n      <p>Next.</p>")
    tree = parse(data)
    FootnoteRenderer().apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])
    assert document_text(serialize(tree, original=data), exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )


def test_end_placement_still_available_for_diagnosis() -> None:
    data = _html("<p>Se fué.</p><p>Next paragraph.</p>")
    tree = parse(data)
    FootnoteRenderer(placement="end").apply(
        tree, [_annotation("Se fué.", "He is gone.", index=1)]
    )
    body = tree.getroot().find(f"{{{XHTML_NS}}}body")
    kinds = [etree.QName(el).localname for el in body]
    assert kinds == ["p", "p", "aside"], kinds


def test_end_placement_also_preserves_text() -> None:
    data = _html("<p>Se fué.</p>\n      <p>Next.</p>")
    tree = parse(data)
    FootnoteRenderer(placement="end").apply(
        tree, [_annotation("Se fué.", "He is gone.", index=1)]
    )
    assert document_text(serialize(tree, original=data), exclude_inserted=True) == document_text(
        data, exclude_inserted=False
    )


def test_bad_placement_rejected() -> None:
    tree = parse(_html("<p>Se fué.</p>"))
    with pytest.raises(RenderError, match="placement must be"):
        FootnoteRenderer(placement="sideways").apply(
            tree, [_annotation("Se fué.", "x", index=1)]
        )


def test_adjacent_placement_with_many_notes_keeps_pairs_together() -> None:
    data = _html("<p>Se fué.</p><p>English.</p><p>Está bien.</p>")
    tree = parse(data)
    FootnoteRenderer(placement="adjacent").apply(
        tree,
        [
            _annotation("Se fué.", "He is gone.", index=1),
            _annotation("Está bien.", "That's fine.", index=3),
        ],
    )
    body = tree.getroot().find(f"{{{XHTML_NS}}}body")
    kinds = [etree.QName(el).localname for el in body]
    assert kinds == ["p", "aside", "p", "p", "aside"], kinds
    # each aside must follow the paragraph that references it
    for i, el in enumerate(body):
        if etree.QName(el).localname != "aside":
            continue
        marker = body[i - 1].find(f"{{{XHTML_NS}}}a")
        assert marker.get("href") == f"#{el.get('id')}"


# ------------------------------------------------- footnote variants (Kobo A/B)


def test_every_variant_builds_and_preserves_text() -> None:
    """Whatever the reader quirk, the prose invariant is non-negotiable."""
    from etx.render.annotate import VARIANTS, get_renderer

    data = _html("<p>Se fué.</p>\n      <p>English.</p>\n      <p>Está bien.</p>")
    for key in VARIANTS:
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


def test_visible_variant_does_not_hide_the_note() -> None:
    """The suspected Kobo fix: no display:none anywhere in the CSS."""
    from etx.render.annotate import get_renderer

    css = get_renderer("A-visible").css()
    assert "display: none" not in css


def test_control_variant_reproduces_the_failing_css() -> None:
    """Kept deliberately so the bug can be reproduced on demand."""
    from etx.render.annotate import get_renderer

    assert "display: none" in get_renderer("Z-control-hidden").css()


def test_offscreen_variant_keeps_a_layout_box() -> None:
    from etx.render.annotate import get_renderer

    css = get_renderer("B-offscreen").css()
    assert "display: none" not in css
    assert "position: absolute" in css


def test_div_variant_emits_div_and_no_backlink() -> None:
    from etx.render.annotate import get_renderer

    data = _html("<p>Se fué.</p>")
    tree = parse(data)
    engine = get_renderer("C-div")
    engine.apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])
    # C-div is a file-placement variant, so the note body is a separate document
    note = engine.new_documents[0][1].decode()
    assert "<div" in note and "<aside" not in note
    assert 'epub:type="backlink"' not in note
    assert "He is gone." in note


def test_endnotes_variant_drops_noteref_and_collects_at_end() -> None:
    """Plain navigation instead of popup semantics — the reliable fallback."""
    from etx.render.annotate import get_renderer

    data = _html("<p>Se fué.</p><p>English.</p>")
    tree = parse(data)
    get_renderer("D-endnotes").apply(tree, [_annotation("Se fué.", "He is gone.", index=1)])
    out = serialize(tree, original=data).decode()
    assert 'epub:type="noteref"' not in out
    assert 'epub:type="footnote"' not in out
    body = tree.getroot().find(f"{{{XHTML_NS}}}body")
    assert etree.QName(body[-1]).localname == "aside", "note should be last in body"


def test_inline_variant_also_needs_epub3() -> None:
    """Not for the brackets — for the ``data-etx`` marker.

    ``data-*`` is HTML5. EPUB 2's XHTML 1.1 rejects it, so an un-upgraded inline
    build fails epubcheck with 'attribute "data-etx" not allowed here'. This was
    latent from the start and only surfaced once a variant build ran epubcheck on
    inline output.
    """
    from etx.render.annotate import get_renderer

    assert get_renderer("E-inline").needs_epub3() is True


def test_bad_hide_and_element_values_rejected() -> None:
    tree = parse(_html("<p>Se fué.</p>"))
    # note: "span" is a *valid* element now — Kobo's own spec example uses one
    for kwargs in ({"hide": "magic"}, {"element": "table"}, {"placement": "sideways"}):
        with pytest.raises(RenderError):
            FootnoteRenderer(**kwargs).apply(
                tree, [_annotation("Se fué.", "x", index=1)]
            )
