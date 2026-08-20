"""Text extraction, the inserted-node exclusion rule, and text-unit addressing."""

from __future__ import annotations

from joven.epub.document import document_text, iter_text_units, parse, serialize

XML_DECL = "<?xml version='1.0' encoding='utf-8'?>"


def _html(body: str) -> bytes:
    return f"""{XML_DECL}
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>t</title></head>
  <body>{body}</body>
</html>
""".encode()


def test_extracts_paragraph_text() -> None:
    data = _html('<p class="calibre4">Vaya con Dios.</p>')
    assert "Vaya con Dios." in document_text(data)


def test_marker_excluded_but_its_tail_kept() -> None:
    """The invariant's core case: an inline noteref splits a text node."""
    original = _html('<p>Cuántos años tienes? the old man said.</p>')
    annotated = _html(
        '<p>Cuántos años tienes?'
        '<a data-joven="marker" href="#n1" id="r1">*</a>'
        " the old man said.</p>"
    )
    assert document_text(annotated, exclude_inserted=True) == document_text(
        original, exclude_inserted=False
    )


def test_note_body_and_its_tail_both_excluded() -> None:
    original = _html("<p>Se fué.</p>")
    annotated = _html(
        '<p>Se fué.<a data-joven="marker" href="#n1" id="r1">*</a></p>'
        '<aside data-joven="note" id="n1"><p>He is gone.</p></aside>\n'
    )
    assert document_text(annotated, exclude_inserted=True) == document_text(
        original, exclude_inserted=False
    )


def test_multi_span_paragraph_roundtrips() -> None:
    """Case B/D from DESIGN.md §1.1 — Spanish runs split by an English tag."""
    original = _html(
        "<p>Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.</p>"
    )
    annotated = _html(
        "<p>Escúchame, joven, he said. Yo no sé nada. Esto es la verdad."
        '<a data-joven="marker" href="#n2" id="r2">*</a></p>'
        '<aside data-joven="note" id="n2"><p>Listen to me, young man...</p></aside>'
    )
    assert document_text(annotated, exclude_inserted=True) == document_text(
        original, exclude_inserted=False
    )


def test_exclude_inserted_false_includes_our_nodes() -> None:
    annotated = _html('<p>Hola<a data-joven="marker" href="#n1">*</a></p>')
    assert "*" in document_text(annotated, exclude_inserted=False)
    assert "*" not in document_text(annotated, exclude_inserted=True)


def test_comments_do_not_break_extraction() -> None:
    data = _html("<p>before<!-- a comment -->after</p>")
    assert document_text(data) == "beforeafter"


def test_text_units_are_addressable_and_ordered() -> None:
    data = _html("<p>One.</p><p>Two.</p><p>   </p><p>Three.</p>")
    units = iter_text_units(data, "OEBPS/part1.xhtml")
    assert [u.text for u in units] == ["One.", "Two.", "Three."]
    assert units[0].address == "OEBPS/part1.xhtml#1"
    # the blank paragraph consumes an index, so addresses stay stable
    assert units[2].index == 4


def test_container_divs_do_not_duplicate_paragraphs() -> None:
    data = _html("<div><p>Inner.</p></div>")
    units = iter_text_units(data, "x.xhtml")
    assert [u.text for u in units] == ["Inner."]


def test_serialize_preserves_single_quoted_xml_declaration() -> None:
    original = _html("<p>Hola.</p>")
    out = serialize(parse(original), original=original)
    assert out.startswith(b"<?xml version='1.0' encoding='utf-8'?>")


def test_serialize_roundtrip_preserves_text(sample_epub) -> None:
    from joven.epub.archive import EpubArchive

    archive = EpubArchive.read(sample_epub)
    for href in archive.xhtml_names():
        data = archive.get(href)
        again = serialize(parse(data), original=data)
        assert document_text(again) == document_text(data)


# ------------------------------------------------ XML declaration preservation


def test_declaration_on_the_same_line_as_the_root_element() -> None:
    """The Knopf edition writes ``<?xml …?><html …>`` with no newline between.

    An earlier ``serialize`` took "everything before the first newline" as the
    declaration, which on this shape swallowed the whole ``<html>`` open tag and
    prepended it to a body that already had one — two opens, one close, on every
    document in the book:

        DocumentError: malformed XHTML: Premature end of data in tag html line 1
    """
    source = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Se fu\xc3\xa9.</p></body></html>'
    )
    out = serialize(parse(source), original=source)

    assert out.count(b"<html") == 1, "duplicated the root element"
    parse(out)  # must survive a second pass
    assert out.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="no"?><html')


def test_declaration_on_its_own_line_keeps_its_newline() -> None:
    """Calibre's shape — the newline is part of the declaration and must survive."""
    source = (
        b"<?xml version='1.0' encoding='utf-8'?>\n"
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hi.</p></body></html>'
    )
    out = serialize(parse(source), original=source)
    assert out.startswith(b"<?xml version='1.0' encoding='utf-8'?>\n<html")
    assert out == source, "own-line declarations should round-trip byte-identically"


def test_single_quoted_declaration_is_not_rewritten_to_double() -> None:
    source = b"<?xml version='1.0' encoding='utf-8'?>\n<html><body><p>x</p></body></html>"
    assert b"version='1.0'" in serialize(parse(source), original=source)


def test_missing_declaration_gets_a_default() -> None:
    source = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>'
    out = serialize(parse(source), original=source)
    assert out.startswith(b"<?xml")
    assert out.count(b"<html") == 1


def test_crlf_in_the_body_normalizes_to_lf() -> None:
    """XML parsers must normalize CRLF in content, so this is expected, not a bug.

    The Knopf edition is a CRLF file, so every document we reserialize comes out a
    few bytes shorter. Documented as a test so that difference is not mistaken for
    corruption later. The declaration's *own* line ending is preserved verbatim —
    that is style, not content.
    """
    source = (
        b"<?xml version='1.0' encoding='utf-8'?>\r\n"
        b"<html><body>\r\n<p>x</p>\r\n</body></html>"
    )
    out = serialize(parse(source), original=source)

    assert out.startswith(b"<?xml version='1.0' encoding='utf-8'?>\r\n"), "style preserved"
    assert b"\r" not in out[40:], "body CRs normalized away by the parser"
    parse(out)
