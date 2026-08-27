"""Sources that arrive already Kobo-converted.

The bug these cover was invisible to every file-level check and only showed on
the device: a marker inserted inside a ``koboSpan`` renders as a bare asterisk
with the annotated passage gone, because Kobo treats a koboSpan as a leaf text
unit and drops its text once it has an element child.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from joven.epub.archive import EpubArchive
from joven.epub.document import document_text
from joven.model import Annotation, Sidecar
from joven.render import render_epub
from joven.render.kobo import dekepubify, is_kepubified
from joven.verify import check_marker_span_nesting, check_roundtrip_identical, check_text_preserved

XHTML = "http://www.w3.org/1999/xhtml"


@pytest.fixture
def kepubified_epub(sample_epub: Path, tmp_path: Path) -> Path:
    """The synthetic fixture, converted the way Calibre's KePub plugin converts.

    Sentence-level spans, the wrapper divs, the style hack, and the injected
    script — including the redundant ``xmlns`` on every span, which is the
    fingerprint that distinguishes the Calibre plugin's output from kepubify's.
    """
    archive = EpubArchive.read(sample_epub)
    counter = 0
    for href in archive.xhtml_names():
        tree = etree.fromstring(archive.get(href))
        body = tree.find(f"{{{XHTML}}}body")
        if body is None:
            continue
        for paragraph in body.iter(f"{{{XHTML}}}p"):
            if not paragraph.text:
                continue
            counter += 1
            span = etree.SubElement(paragraph, f"{{{XHTML}}}span")
            span.set("class", "koboSpan")
            span.set("id", f"kobo.{counter}.1")
            span.text, paragraph.text = paragraph.text, None
        outer = etree.SubElement(body, f"{{{XHTML}}}div")
        outer.set("id", "book-columns")
        inner = etree.SubElement(outer, f"{{{XHTML}}}div")
        inner.set("id", "book-inner")
        for child in [c for c in body if c is not outer]:
            inner.append(child)
        head = tree.find(f"{{{XHTML}}}head")
        style = etree.SubElement(head, f"{{{XHTML}}}style")
        style.set("class", "kobostylehacks")
        style.text = "div#book-inner { margin-top: 0; }"
        script = etree.SubElement(head, f"{{{XHTML}}}script")
        script.set("src", "../js/kobo.js")
        script.text = ""
        archive.replace(href, etree.tostring(tree, xml_declaration=True, encoding="utf-8"))

    out = tmp_path / "kepubified.epub"
    archive.write(out)
    return out


@pytest.fixture
def sidecar() -> Sidecar:
    s = Sidecar(title="Test Book")
    s.annotations.append(
        Annotation.create(
            href="OEBPS/part1.xhtml",
            para_index=5,
            source_text="Cuántos años tienes? the old man said.",
            spans=[(0, 20)],
            translation="How old are you?",
        )
    )
    return s


def test_the_fixture_really_is_kepubified(kepubified_epub: Path) -> None:
    assert is_kepubified(EpubArchive.read(kepubified_epub))


def test_unwrapping_preserves_document_text_exactly(kepubified_epub: Path) -> None:
    """A koboSpan adds markup, never text — which is why offsets stay valid."""
    archive = EpubArchive.read(kepubified_epub)
    before = {href: document_text(archive.get(href)) for href in archive.xhtml_names()}

    assert dekepubify(archive)
    assert not is_kepubified(archive)

    after = {href: document_text(archive.get(href)) for href in archive.xhtml_names()}
    assert after == before


def test_the_kobo_layer_is_gone_after_normalizing(kepubified_epub: Path) -> None:
    archive = EpubArchive.read(kepubified_epub)
    dekepubify(archive)
    for href in archive.xhtml_names():
        body = archive.get(href)
        assert b"koboSpan" not in body
        assert b"book-columns" not in body
        assert b"kobostylehacks" not in body
        assert b"kobo.js" not in body


def test_js_kobo_stays_in_the_archive(kepubified_epub: Path) -> None:
    """Dropping an entry would break the no-entry-disappears invariant for nothing."""
    archive = EpubArchive.read(kepubified_epub)
    before = archive.names()
    dekepubify(archive)
    assert archive.names() == before


def test_rendering_a_kepubified_source_keeps_markers_out_of_kobospans(
    kepubified_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    """The regression test for the device-only failure."""
    result = render_epub(kepubified_epub, sidecar, tmp_path / "out", make_kepub=False)
    assert result.annotations_applied == 1
    assert result.normalized_kobo, "a kepubified source must be normalized first"

    produced = EpubArchive.read(result.epub_path)
    finding = check_marker_span_nesting(produced)
    assert finding.ok, finding.detail
    assert check_text_preserved(EpubArchive.read(kepubified_epub), produced).ok


def test_passthrough_of_a_kepubified_source_is_untouched(
    kepubified_epub: Path, tmp_path: Path
) -> None:
    """With nothing to annotate there is no reason to rewrite the book."""
    result = render_epub(kepubified_epub, None, tmp_path / "out", make_kepub=False)
    assert result.normalized_kobo == []
    assert check_roundtrip_identical(
        EpubArchive.read(kepubified_epub), EpubArchive.read(result.epub_path)
    ).ok


def test_the_check_catches_a_marker_nested_in_a_kobospan(tmp_path: Path) -> None:
    """Prove the guard fails on the exact shape that shipped to the device."""
    archive = EpubArchive.read(_bad_epub(tmp_path))
    finding = check_marker_span_nesting(archive)
    assert not finding.ok
    assert "vanish on the device" in finding.detail


def _bad_epub(tmp_path: Path) -> Path:
    """A minimal EPUB whose marker sits inside a koboSpan."""
    import zipfile

    doc = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>c</title></head><body>'
        '<p><span class="koboSpan" id="kobo.1.1">Se fué.'
        '<a href="n.xhtml#n1" id="joven-ref-1" epub:type="noteref" '
        'data-joven="marker">*</a> </span></p></body></html>'
    )
    path = tmp_path / "bad.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="i"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="i">u</dc:identifier>'
            "<dc:title>t</dc:title><dc:language>en</dc:language></metadata><manifest>"
            '<item id="c" href="ch.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c"/></spine></package>',
        )
        z.writestr("OEBPS/ch.xhtml", doc)
    return path
