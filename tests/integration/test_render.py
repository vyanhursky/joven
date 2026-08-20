"""End-to-end rendering: EPUB 2 -> annotated EPUB 3 -> KEPUB.

M3 exit criteria. The two that matter most: the prose survives untouched, and the
result is valid EPUB 3 — because declaring version 3.0 makes the source book's
pre-existing Calibre defects into hard validation errors that we then own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from joven.epub.archive import EpubArchive
from joven.epub.document import document_text
from joven.epub.package import read_package
from joven.kepub import kepubify_available
from joven.model import Annotation, Sidecar, Status
from joven.render import render_epub
from joven.verify import (
    check_epubcheck,
    check_ids_unique,
    check_noterefs_resolve,
    check_roundtrip_identical,
    check_text_preserved,
    epubcheck_available,
)


@pytest.fixture
def sidecar() -> Sidecar:
    """Annotates the Spanish paragraphs of the synthetic fixture's part1."""
    s = Sidecar(title="Test Book")
    for index, text, spans, translation in [
        (2, "Se fué.", [(0, 7)], "He is gone."),
        (5, "Cuántos años tienes? the old man said.", [(0, 20)], "How old are you?"),
    ]:
        s.annotations.append(
            Annotation.create(
                href="OEBPS/part1.xhtml",
                para_index=index,
                source_text=text,
                spans=spans,
                translation=translation,
                detector_confidence=0.99,
                model="qwen3:8b",
            )
        )
    return s


def test_no_sidecar_is_a_pure_passthrough(sample_epub: Path, tmp_path: Path) -> None:
    result = render_epub(sample_epub, None, tmp_path / "out", make_kepub=False)
    assert result.annotations_applied == 0
    assert result.upgraded_to_epub3 is False
    finding = check_roundtrip_identical(
        EpubArchive.read(sample_epub), EpubArchive.read(result.epub_path)
    )
    assert finding.ok, finding.detail


def test_footnote_render_preserves_text(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False)
    assert result.annotations_applied == 2
    finding = check_text_preserved(
        EpubArchive.read(sample_epub), EpubArchive.read(result.epub_path)
    )
    assert finding.ok, finding.detail


def test_inline_render_preserves_text(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    result = render_epub(
        sample_epub, sidecar, tmp_path / "out", renderer="inline", make_kepub=False
    )
    produced = EpubArchive.read(result.epub_path)
    assert check_text_preserved(EpubArchive.read(sample_epub), produced).ok
    assert "[How old are you?]" in produced.get("OEBPS/part1.xhtml").decode()
    # Inline still upgrades the package: the data-joven marker is an HTML5 attribute
    # that EPUB 2 rejects (epubcheck: 'attribute "data-joven" not allowed here').
    assert result.upgraded_to_epub3 is True
    assert read_package(produced).version == "3.0"


def test_upgrade_produces_valid_epub3_package(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False)
    produced = EpubArchive.read(result.epub_path)
    package = read_package(produced)

    assert result.upgraded_to_epub3 is True
    assert package.version == "3.0"
    assert package.is_epub3
    assert "nav.xhtml" in produced
    opf = produced.get(package.opf_path).decode()
    assert 'properties="nav"' in opf
    assert "dcterms:modified" in opf
    # EPUB 2 refinement attributes are gone
    assert "opf:role" not in opf
    assert "opf:scheme" not in opf
    # toc.ncx is deliberately kept for Kobo / backward compatibility
    assert "toc.ncx" in produced


def test_content_type_meta_is_repaired(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    """Calibre's bogus http-equiv value is an EPUB 3 error; we fix it."""
    before = EpubArchive.read(sample_epub).get("OEBPS/part1.xhtml").decode()
    assert "http-equiv" not in before or "text/html" not in before

    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False)
    produced = EpubArchive.read(result.epub_path)
    for href in produced.xhtml_names():
        body = produced.get(href).decode()
        if "http-equiv" in body.lower():
            assert 'content="text/html; charset=utf-8"' in body, href


def test_render_is_idempotent(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    """Same inputs, same bytes — otherwise 'just re-render' isn't trustworthy."""
    first = render_epub(sample_epub, sidecar, tmp_path / "a", make_kepub=False)
    second = render_epub(sample_epub, sidecar, tmp_path / "b", make_kepub=False)

    a, b = EpubArchive.read(first.epub_path), EpubArchive.read(second.epub_path)
    assert a.names() == b.names()
    for name in a.names():
        if name.endswith(".opf"):
            continue  # carries a dcterms:modified timestamp
        assert a.get(name) == b.get(name), name


def test_rejected_annotations_are_not_rendered(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    sidecar.annotations[0].status = Status.REJECTED
    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False)
    assert result.annotations_applied == 1
    assert "He is gone." not in EpubArchive.read(result.epub_path).get(
        "OEBPS/part1.xhtml"
    ).decode()


def test_unknown_document_is_reported_not_fatal(sample_epub: Path, tmp_path: Path) -> None:
    s = Sidecar()
    s.annotations.append(
        Annotation.create(
            href="OEBPS/does-not-exist.xhtml",
            para_index=1,
            source_text="Se fué.",
            spans=[(0, 7)],
            translation="He is gone.",
        )
    )
    result = render_epub(sample_epub, s, tmp_path / "out", make_kepub=False)
    assert result.skipped == ["OEBPS/does-not-exist.xhtml"]
    assert result.annotations_applied == 0


def test_noterefs_and_ids_are_sound(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False)
    produced = EpubArchive.read(result.epub_path)
    assert check_noterefs_resolve(produced).ok
    assert check_ids_unique(produced).ok


@pytest.mark.epubcheck
@pytest.mark.skipif(not epubcheck_available(), reason="epubcheck not installed")
def test_annotated_output_is_valid_epub3(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False)
    finding = check_epubcheck(result.epub_path)
    assert finding.ok, finding.detail


@pytest.mark.skipif(not kepubify_available(), reason="kepubify not installed")
def test_kepub_keeps_footnote_markup_and_prose(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    """kepubify injects koboSpan elements — our markers must survive that."""
    result = render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=True)
    assert result.kepub_path is not None

    epub = EpubArchive.read(result.epub_path)
    kepub = EpubArchive.read(result.kepub_path)

    doc = next(n for n in kepub.xhtml_names() if n.endswith("part1.xhtml"))
    body = kepub.get(doc).decode()
    assert body.count('epub:type="noteref"') == 2
    assert "koboSpan" in body  # kepubify really did run
    # the note bodies live in their own documents under the default recipe
    notes = [n for n in kepub.xhtml_names() if "joven-notes/" in n]
    assert len(notes) == 2
    assert all('epub:type="footnote"' in kepub.get(n).decode() for n in notes)

    for href in epub.xhtml_names():
        name = Path(href).name
        match = next(n for n in kepub.xhtml_names() if Path(n).name == name)
        assert "".join(document_text(epub.get(href)).split()) == "".join(
            document_text(kepub.get(match)).split()
        ), name


# ------------------------------------------------------------- the real book


@pytest.mark.realbook
def test_real_book_annotated_render(real_epub, spanish_units, tmp_path: Path) -> None:
    """Annotate real Spanish passages in whichever book is under test.

    Targets come from the ``spanish_units`` fixture rather than being named here:
    an earlier version looked up three exact phrases from *The Crossing*, which
    made the test a check on one book instead of on the pipeline.
    """
    archive = EpubArchive.read(real_epub)

    s = Sidecar(title=read_package(archive).metadata.get("title", ""))
    for href, para_index, source_text, span in spanish_units:
        s.annotations.append(
            Annotation.create(
                href=href,
                para_index=para_index,
                source_text=source_text,
                spans=[span],
                translation="(translation under test)",
            )
        )

    result = render_epub(real_epub, s, tmp_path / "out", make_kepub=False)
    assert result.annotations_applied == len(spanish_units)
    assert result.upgraded_to_epub3 is True

    produced = EpubArchive.read(result.epub_path)
    finding = check_text_preserved(archive, produced)
    assert finding.ok, finding.detail
    assert check_noterefs_resolve(produced).ok
    assert check_ids_unique(produced).ok


@pytest.mark.realbook
@pytest.mark.skipif(not epubcheck_available(), reason="epubcheck not installed")
def test_real_book_annotated_output_is_valid_epub3(
    real_epub, spanish_units, tmp_path: Path
) -> None:
    """The annotated output must validate as EPUB 3 on a real publisher's file.

    This is the gate that catches what synthetic fixtures cannot — the Knopf
    edition's same-line XML declaration corrupted all 14 documents, and only a
    real file surfaced it. It previously selected its target by Calibre filename
    (``part2_split_000``) and so died with StopIteration on any other edition.
    """
    href, para_index, source_text, span = spanish_units[0]

    s = Sidecar()
    s.annotations.append(
        Annotation.create(
            href=href,
            para_index=para_index,
            source_text=source_text,
            spans=[span],
            translation="(translation under test)",
        )
    )

    result = render_epub(real_epub, s, tmp_path / "out", make_kepub=False)
    finding = check_epubcheck(result.epub_path)
    assert finding.ok, finding.detail


@pytest.mark.epubcheck
@pytest.mark.skipif(not epubcheck_available(), reason="epubcheck not installed")
def test_every_variant_passes_epubcheck(sample_epub: Path, tmp_path: Path, sidecar) -> None:
    """Regression: the inline variant shipped invalid EPUB 2 for its whole life.

    ``data-joven`` is HTML5 and EPUB 2 rejects it, but no test had ever run
    epubcheck against a non-footnote renderer, so nothing noticed.
    """
    from joven.render.annotate import VARIANTS

    for key in VARIANTS:
        result = render_epub(
            sample_epub, sidecar, tmp_path / key, renderer=key, make_kepub=False
        )
        finding = check_epubcheck(result.epub_path)
        assert finding.ok, f"{key}: {finding.detail}"
