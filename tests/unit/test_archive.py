"""Archive-level guarantees: lossless round-trip and OCF conformance."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from joven.epub.archive import EpubArchive, EpubError


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
    with pytest.raises(EpubError, match="DRM") as caught:
        EpubArchive.read(drm_epub)
    # naming the resource is what makes the message actionable
    assert "OEBPS/part1.xhtml" in str(caught.value)


def test_accepts_idpf_font_obfuscation(idpf_obfuscated_epub: Path) -> None:
    """Obfuscated fonts are not DRM — the book is perfectly readable.

    `encryption.xml` is shared by both purposes, so keying on the file's presence
    rejected ordinary trade EPUBs with advice to strip DRM that was never applied.
    """
    archive = EpubArchive.read(idpf_obfuscated_epub)
    assert "OEBPS/part1.xhtml" in archive


def test_accepts_adobe_font_obfuscation(adobe_obfuscated_epub: Path) -> None:
    archive = EpubArchive.read(adobe_obfuscated_epub)
    assert "OEBPS/part1.xhtml" in archive


def test_obfuscated_fonts_survive_the_roundtrip_untouched(
    idpf_obfuscated_epub: Path, tmp_path: Path
) -> None:
    """We never deobfuscate, so the reader gets back exactly what it expects."""
    original = EpubArchive.read(idpf_obfuscated_epub)
    out = tmp_path / "out.epub"
    original.write(out)
    produced = EpubArchive.read(out)
    assert produced.get("META-INF/encryption.xml") == original.get("META-INF/encryption.xml")


def test_rejects_mixed_obfuscation_and_real_encryption(tmp_path: Path) -> None:
    """One obfuscated font does not excuse an encrypted chapter."""
    from tests.conftest import _encryption_xml, build_epub

    book = build_epub(
        tmp_path / "mixed.epub",
        extra={
            "META-INF/encryption.xml": _encryption_xml(
                ("http://www.idpf.org/2008/embedding", "OEBPS/fonts/Sabon.otf"),
                ("http://www.w3.org/2001/04/xmlenc#aes128-cbc", "OEBPS/part2.xhtml"),
            )
        },
    )
    with pytest.raises(EpubError, match="encrypted") as caught:
        EpubArchive.read(book)
    assert "OEBPS/part2.xhtml" in str(caught.value)
    assert "Sabon.otf" not in str(caught.value), "the font is not the problem"


def test_rejects_unreadable_encryption_manifest(tmp_path: Path) -> None:
    """Refuse rather than guess when we cannot tell what is encrypted."""
    from tests.conftest import build_epub

    book = build_epub(
        tmp_path / "broken-enc.epub",
        extra={"META-INF/encryption.xml": "<encryption><unclosed></encryption>"},
    )
    with pytest.raises(EpubError, match="unreadable"):
        EpubArchive.read(book)


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
