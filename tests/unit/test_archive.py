"""Archive-level guarantees: lossless round-trip and OCF conformance."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from etx.epub.archive import EpubArchive, EpubError


def test_reads_all_entries(sample_epub: Path) -> None:
    archive = EpubArchive.read(sample_epub)
    assert "mimetype" in archive.names()
    assert "content.opf" in archive.names()
    assert archive.get("mimetype") == b"application/epub+zip"


def test_roundtrip_preserves_every_entry_byte_for_byte(sample_epub: Path, tmp_path: Path) -> None:
    original = EpubArchive.read(sample_epub)
    out = tmp_path / "out.epub"
    original.write(out)
    produced = EpubArchive.read(out)

    assert produced.names() == original.names(), "entry order changed"
    for name in original.names():
        assert produced.get(name) == original.get(name), f"content changed: {name}"


def test_roundtrip_preserves_compression_per_entry(sample_epub: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    EpubArchive.read(sample_epub).write(out)

    with zipfile.ZipFile(sample_epub) as a, zipfile.ZipFile(out) as b:
        before = {i.filename: i.compress_type for i in a.infolist()}
        after = {i.filename: i.compress_type for i in b.infolist()}
    assert after == before


def test_mimetype_is_first_and_stored(sample_epub: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    EpubArchive.read(sample_epub).write(out)
    with zipfile.ZipFile(out) as zf:
        first = zf.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED


def test_mimetype_forced_first_even_if_input_has_it_last(tmp_path: Path) -> None:
    """A book with mimetype in the wrong place must come out correct."""
    src = tmp_path / "wrong-order.epub"
    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.opf", "<package/>")
        zf.writestr("META-INF/container.xml", "<container/>")
        zf.writestr("mimetype", "application/epub+zip")  # last, and compressed

    out = tmp_path / "fixed.epub"
    EpubArchive.read(src).write(out)

    with zipfile.ZipFile(out) as zf:
        first = zf.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED


def test_replace_keeps_position(sample_epub: Path, tmp_path: Path) -> None:
    archive = EpubArchive.read(sample_epub)
    before = archive.names()
    archive.replace("OEBPS/part1.xhtml", b"<html/>")
    assert archive.names() == before
    assert archive.get("OEBPS/part1.xhtml") == b"<html/>"


def test_add_after_inserts_in_place(sample_epub: Path) -> None:
    archive = EpubArchive.read(sample_epub)
    archive.add("nav.xhtml", b"<html/>", after="content.opf")
    names = archive.names()
    assert names[names.index("content.opf") + 1] == "nav.xhtml"


def test_add_rejects_duplicate(sample_epub: Path) -> None:
    archive = EpubArchive.read(sample_epub)
    with pytest.raises(EpubError, match="already exists"):
        archive.add("content.opf", b"x")


def test_write_is_atomic_leaves_no_partial(sample_epub: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    EpubArchive.read(sample_epub).write(out)
    assert not list(tmp_path.glob("*.partial"))


def test_rejects_drm_encrypted(drm_epub: Path) -> None:
    with pytest.raises(EpubError, match="DRM"):
        EpubArchive.read(drm_epub)


def test_rejects_non_zip(tmp_path: Path) -> None:
    bad = tmp_path / "nope.epub"
    bad.write_bytes(b"this is not a zip file")
    with pytest.raises(EpubError, match="not a zip"):
        EpubArchive.read(bad)


def test_rejects_missing_container(tmp_path: Path) -> None:
    src = tmp_path / "no-container.epub"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
    with pytest.raises(EpubError, match="container.xml"):
        EpubArchive.read(src)


def test_xhtml_names(sample_epub: Path) -> None:
    archive = EpubArchive.read(sample_epub)
    assert archive.xhtml_names() == ["OEBPS/part1.xhtml", "OEBPS/part2.xhtml"]
