"""Verification checks, especially the Kobo popup conditions."""

from __future__ import annotations

from pathlib import Path

from joven.epub.archive import EpubArchive
from joven.model import Annotation, Sidecar
from joven.render import render_epub
from joven.verify import check_kobo_popup_conditions, check_noterefs_resolve


def _sidecar() -> Sidecar:
    s = Sidecar()
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
            )
        )
    return s


def test_kobo_conditions_hold_for_every_variant(sample_epub: Path, tmp_path: Path) -> None:
    """The forward-reference and length conditions from Kobo's published spec."""
    from joven.render.annotate import VARIANTS

    for key in VARIANTS:
        result = render_epub(
            sample_epub, _sidecar(), tmp_path / key, renderer=key, make_kepub=False
        )
        produced = EpubArchive.read(result.epub_path)
        finding = check_kobo_popup_conditions(produced)
        assert finding.ok, f"{key}: {finding.detail}"


def test_noterefs_resolve_for_every_variant(sample_epub: Path, tmp_path: Path) -> None:
    """Includes the <span> variant, where the id sits on a child of the note."""
    from joven.render.annotate import VARIANTS

    for key in VARIANTS:
        result = render_epub(
            sample_epub, _sidecar(), tmp_path / f"nr-{key}", renderer=key, make_kepub=False
        )
        finding = check_noterefs_resolve(EpubArchive.read(result.epub_path))
        assert finding.ok, f"{key}: {finding.detail}"


def test_one_note_per_file_creates_isolated_documents(sample_epub: Path, tmp_path: Path) -> None:
    """The whole point of file placement: the preview cannot run into the next note."""
    result = render_epub(
        sample_epub, _sidecar(), tmp_path / "h", renderer="H-file-type", make_kepub=False
    )
    assert len(result.note_documents) == 2
    produced = EpubArchive.read(result.epub_path)
    for path in result.note_documents:
        body = produced.get(path).decode()
        assert body.count('data-joven="note"') == 1, f"{path} holds more than one note"


def test_note_documents_are_non_linear_spine_items(sample_epub: Path, tmp_path: Path) -> None:
    """epubcheck RSC-011 requires spine membership; linear="no" keeps them out of
    the reading order."""
    result = render_epub(
        sample_epub, _sidecar(), tmp_path / "h2", renderer="H-file-type", make_kepub=False
    )
    produced = EpubArchive.read(result.epub_path)
    opf = produced.get("content.opf").decode()
    assert opf.count('linear="no"') == 2
    assert opf.count('media-type="application/xhtml+xml"') >= 4
