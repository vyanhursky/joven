"""M1 exit criteria: lossless round-trip, the text invariant, and epubcheck.

These are the tests that make every later guarantee believable. If they fail, no
amount of translation quality matters — the book is corrupt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from joven.epub.archive import EpubArchive
from joven.epub.package import read_package
from joven.kepub import kepub_name, kepubify, kepubify_available
from joven.verify import (
    check_epubcheck,
    check_kepub_naming,
    check_mimetype_first,
    check_roundtrip_identical,
    check_text_preserved,
    epubcheck_available,
)


def test_zero_annotation_render_is_identical(sample_epub: Path, tmp_path: Path) -> None:
    original = EpubArchive.read(sample_epub)
    out = tmp_path / "out.epub"
    original.write(out)

    finding = check_roundtrip_identical(original, EpubArchive.read(out))
    assert finding.ok, finding.detail


def test_text_invariant_holds_on_passthrough(sample_epub: Path, tmp_path: Path) -> None:
    original = EpubArchive.read(sample_epub)
    out = tmp_path / "out.epub"
    original.write(out)

    finding = check_text_preserved(original, EpubArchive.read(out))
    assert finding.ok, finding.detail


def test_text_invariant_catches_dropped_text(sample_epub: Path, tmp_path: Path) -> None:
    """A deliberately corrupted output must be caught — the check has teeth."""
    original = EpubArchive.read(sample_epub)
    broken = EpubArchive.read(sample_epub)
    mangled = original.get("OEBPS/part1.xhtml").replace(b"Se fu\xc3\xa9.", b"")
    broken.replace("OEBPS/part1.xhtml", mangled)

    finding = check_text_preserved(original, broken)
    assert not finding.ok
    assert "part1" in finding.detail


def test_text_invariant_catches_altered_text(sample_epub: Path) -> None:
    original = EpubArchive.read(sample_epub)
    broken = EpubArchive.read(sample_epub)
    broken.replace(
        "OEBPS/part1.xhtml",
        original.get("OEBPS/part1.xhtml").replace(b"Dieciseis", b"Sixteen"),
    )
    assert not check_text_preserved(original, broken).ok


def test_mimetype_check_on_output(sample_epub: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    EpubArchive.read(sample_epub).write(out)
    assert check_mimetype_first(out).ok


def test_package_is_parsed(sample_epub: Path) -> None:
    package = read_package(EpubArchive.read(sample_epub))
    assert package.version == "2.0"
    assert package.is_epub3 is False
    assert package.metadata["title"] == "Test Book"
    assert package.spine_hrefs == ["OEBPS/part1.xhtml", "OEBPS/part2.xhtml"]


@pytest.mark.epubcheck
@pytest.mark.skipif(not epubcheck_available(), reason="epubcheck not installed")
def test_output_passes_epubcheck(sample_epub: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    EpubArchive.read(sample_epub).write(out)
    finding = check_epubcheck(out)
    assert finding.ok, finding.detail


def test_kepub_naming_rule() -> None:
    assert kepub_name(Path("book.annotated.epub")) == "book.annotated.kepub.epub"
    assert kepub_name(Path("book.kepub.epub")) == "book.kepub.epub"
    assert check_kepub_naming(Path("a/book.kepub.epub")).ok
    assert not check_kepub_naming(Path("a/book.epub")).ok


@pytest.mark.skipif(not kepubify_available(), reason="kepubify not installed")
def test_kepubify_produces_kobo_file(sample_epub: Path, tmp_path: Path) -> None:
    staged = tmp_path / "staged.epub"
    EpubArchive.read(sample_epub).write(staged)

    out = kepubify(staged, tmp_path / "out")
    assert out.is_file()
    assert out.name.endswith(".kepub.epub")
    assert check_kepub_naming(out).ok

    # KEPUB must still be a readable EPUB container
    kepub_archive = EpubArchive.read(out)
    assert "mimetype" in kepub_archive.names()


@pytest.mark.skipif(not kepubify_available(), reason="kepubify not installed")
def test_kepubify_preserves_prose(sample_epub: Path, tmp_path: Path) -> None:
    """kepubify injects koboSpan elements; the actual words must survive."""
    staged = tmp_path / "staged.epub"
    original = EpubArchive.read(sample_epub)
    original.write(staged)

    kepub_archive = EpubArchive.read(kepubify(staged, tmp_path / "out"))

    from joven.epub.document import document_text

    for href in original.xhtml_names():
        before = "".join(document_text(original.get(href)).split())
        candidates = [n for n in kepub_archive.xhtml_names() if n.endswith(Path(href).name)]
        assert candidates, f"{href} vanished from the KEPUB"
        after = "".join(document_text(kepub_archive.get(candidates[0])).split())
        assert before == after, f"kepubify altered prose in {href}"


# ------------------------------------------------------------- the real book


@pytest.mark.realbook
def test_real_book_roundtrip(real_epub: Path, tmp_path: Path) -> None:
    original = EpubArchive.read(real_epub)
    out = tmp_path / "real-out.epub"
    original.write(out)
    produced = EpubArchive.read(out)

    assert check_roundtrip_identical(original, produced).ok
    finding = check_text_preserved(original, produced)
    assert finding.ok, finding.detail


@pytest.mark.realbook
@pytest.mark.skipif(not epubcheck_available(), reason="epubcheck not installed")
def test_real_book_output_epubcheck_no_worse(real_epub: Path, tmp_path: Path) -> None:
    """Our output must not introduce *new* epubcheck problems.

    The source is a Calibre-produced EPUB 2 and may already have warnings, so the
    bar is "no worse than the input", not "clean".
    """
    out = tmp_path / "real-out.epub"
    EpubArchive.read(real_epub).write(out)

    before = check_epubcheck(real_epub)
    after = check_epubcheck(out)
    if before.ok:
        assert after.ok, after.detail
    else:
        assert after.detail.count("ERROR") <= before.detail.count("ERROR"), (
            f"new errors introduced\nbefore:\n{before.detail}\nafter:\n{after.detail}"
        )
