"""Un-annotating: render, strip, and get the prose back exactly.

Found by running the browser UI against a library: the only copy of a book on
disk was a Joven output, and rendering onto it doubled every marker. Strip is the
way back; refusing to render onto an annotated file is the guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from joven.cli import app
from joven.epub.archive import EpubArchive
from joven.epub.document import document_text
from joven.epub.package import read_package
from joven.model import Annotation, Sidecar
from joven.render import render_epub
from joven.render.strip import count_markers, strip_annotations
from joven.verify import check_epubcheck, check_text_preserved, epubcheck_available

runner = CliRunner()


@pytest.fixture
def sidecar() -> Sidecar:
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
            )
        )
    return s


def _annotated(sample_epub: Path, tmp_path: Path, sidecar: Sidecar, **kw) -> Path:
    return render_epub(sample_epub, sidecar, tmp_path / "out", make_kepub=False, **kw).epub_path


def _texts(archive: EpubArchive) -> dict[str, str]:
    """Every document's text, *including* anything inserted -- the whole page."""
    return {
        name: document_text(archive.get(name), exclude_inserted=False)
        for name in archive.xhtml_names()
    }


def test_a_pristine_book_has_no_markers(sample_epub: Path) -> None:
    assert count_markers(EpubArchive.read(sample_epub)) == 0


def test_a_rendered_book_counts_one_marker_per_annotation(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    assert count_markers(EpubArchive.read(annotated)) == 2


def test_strip_returns_the_prose_and_removes_every_trace(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    archive = EpubArchive.read(annotated)
    original = EpubArchive.read(sample_epub)

    result = strip_annotations(archive)

    assert result.markers_removed == 2
    assert result.notes_removed == 2  # one note document per footnote
    assert result.documents_touched == ["OEBPS/part1.xhtml"]
    assert result.stylesheet == "stylesheet1.css"
    assert count_markers(archive) == 0
    assert not [n for n in archive.names() if "joven-notes" in n]
    assert b"joven-notes" not in archive.get(read_package(archive).opf_path)
    assert b"joven" not in archive.get("stylesheet1.css")
    # the prose, asterisks and all, is exactly the original's
    stripped_texts = _texts(archive)
    for name, text in _texts(original).items():
        assert stripped_texts[name] == text, name


def test_strip_survives_the_round_trip_through_a_file(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    archive = EpubArchive.read(annotated)
    strip_annotations(archive)
    stripped = tmp_path / "stripped.epub"
    archive.write(stripped)

    reread = EpubArchive.read(stripped)
    assert count_markers(reread) == 0
    assert check_text_preserved(EpubArchive.read(sample_epub), reread).ok


def test_a_stripped_book_renders_again_cleanly(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    """The point of the exercise: strip, then annotate as if from the original."""
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    archive = EpubArchive.read(annotated)
    strip_annotations(archive)
    stripped = tmp_path / "stripped.epub"
    archive.write(stripped)

    again = render_epub(stripped, sidecar, tmp_path / "again", make_kepub=False)
    produced = EpubArchive.read(again.epub_path)
    assert again.annotations_applied == 2
    assert count_markers(produced) == 2
    assert check_text_preserved(EpubArchive.read(stripped), produced).ok
    if epubcheck_available():
        finding = check_epubcheck(again.epub_path)
        assert finding.ok, finding.detail


@pytest.mark.epubcheck
def test_a_stripped_book_is_valid(sample_epub: Path, tmp_path: Path, sidecar: Sidecar) -> None:
    if not epubcheck_available():
        pytest.skip("epubcheck not available")
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    archive = EpubArchive.read(annotated)
    strip_annotations(archive)
    stripped = tmp_path / "stripped.epub"
    archive.write(stripped)
    finding = check_epubcheck(stripped)
    assert finding.ok, finding.detail


def test_strip_handles_the_inline_renderer_too(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar, renderer="inline")
    archive = EpubArchive.read(annotated)
    assert count_markers(archive) == 2
    strip_annotations(archive)
    assert count_markers(archive) == 0
    assert "[How old are you?]" not in document_text(
        archive.get("OEBPS/part1.xhtml"), exclude_inserted=False
    )
    assert _texts(archive)["OEBPS/part1.xhtml"] == _texts(EpubArchive.read(sample_epub))[
        "OEBPS/part1.xhtml"
    ]


# ------------------------------------------------------------------- the CLI


def test_render_refuses_a_joven_output(sample_epub: Path, tmp_path: Path, sidecar: Sidecar) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    path = tmp_path / "annotations.json"
    sidecar.save(path)
    result = runner.invoke(
        app, ["render", str(annotated), str(path), "-o", str(tmp_path / "twice"), "--no-kepub"]
    )
    assert result.exit_code == 1
    assert "already carries 2 Joven footnote markers" in result.output
    assert "joven strip" in result.output
    assert not (tmp_path / "twice").exists()


def test_passthrough_render_of_a_joven_output_is_still_allowed(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    """No sidecar, no insertion, no doubling -- the lossless copy is fine."""
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    result = runner.invoke(
        app, ["render", str(annotated), "-o", str(tmp_path / "copy"), "--no-kepub"]
    )
    assert result.exit_code == 0, result.output


def test_inspect_flags_a_joven_output(sample_epub: Path, tmp_path: Path, sidecar: Sidecar) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar)
    result = runner.invoke(app, ["inspect", str(annotated)])
    assert result.exit_code == 0, result.output
    assert "joven markers 2" in result.output
    assert "joven markers" not in runner.invoke(app, ["inspect", str(sample_epub)]).output


def test_strip_command_writes_a_clean_copy(
    sample_epub: Path, tmp_path: Path, sidecar: Sidecar
) -> None:
    annotated = _annotated(sample_epub, tmp_path, sidecar)  # sample.annotated.epub
    result = runner.invoke(app, ["strip", str(annotated), "-o", str(tmp_path / "clean")])
    assert result.exit_code == 0, result.output
    assert "removed 2 marker(s)" in result.output
    out = tmp_path / "clean" / "sample.stripped.epub"
    assert out.is_file()
    assert count_markers(EpubArchive.read(out)) == 0


def test_strip_command_on_a_pristine_book_does_nothing(sample_epub: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["strip", str(sample_epub), "-o", str(tmp_path / "clean")])
    assert result.exit_code == 0, result.output
    assert "nothing to strip" in result.output
    assert not (tmp_path / "clean").exists()
