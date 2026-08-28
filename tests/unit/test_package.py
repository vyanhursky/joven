"""OPF reading — above all, which ``dc:identifier`` is the book's identity.

This file exists because getting that wrong is not inert: the EPUB 2->3 upgrade
syncs the legacy NCX's ``dtb:uid`` to whatever ``read_package`` reports, so the
wrong answer rewrites a correct NCX and *introduces* an epubcheck error into a
book that had none.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from joven.epub.archive import EpubArchive
from joven.epub.package import read_package

CONTAINER = """<?xml version='1.0' encoding='utf-8'?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _book(tmp_path: Path, metadata: str, *, unique: str = "uuid_id") -> Path:
    opf = f"""<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="{unique}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>T</dc:title>
    <dc:language>en</dc:language>
{metadata}
  </metadata>
  <manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="c1"/></spine>
</package>
"""
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("content.opf", opf)
        zf.writestr(
            "c1.xhtml",
            '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
            "<body><p>x</p></body></html>",
        )
    return path


def _identifier(tmp_path: Path, metadata: str, **kwargs) -> str | None:
    archive = EpubArchive.read(_book(tmp_path, metadata, **kwargs))
    return read_package(archive).metadata.get("identifier")


def test_single_identifier_is_used(tmp_path: Path) -> None:
    assert (
        _identifier(tmp_path, '    <dc:identifier id="uuid_id">urn:uuid:abc</dc:identifier>')
        == "urn:uuid:abc"
    )


def test_unique_identifier_wins_over_document_order(tmp_path: Path) -> None:
    """*All the Pretty Horses* in miniature: the ISBN comes first, and is not it.

    Taking the first ``dc:identifier`` here rewrote a correct NCX to the ISBN and
    gave a clean book an NCX-001 error. The OPF says which one counts; believe it.
    """
    identifier = _identifier(
        tmp_path,
        '    <dc:identifier opf:scheme="ISBN">9780679744399</dc:identifier>\n'
        '    <dc:identifier id="uuid_id">07553c70-06f6-4bba</dc:identifier>',
    )
    assert identifier == "07553c70-06f6-4bba"


def test_falls_back_to_the_first_when_unique_identifier_dangles(tmp_path: Path) -> None:
    """A wrong-but-present value still beats none for ``joven inspect``."""
    identifier = _identifier(
        tmp_path,
        '    <dc:identifier opf:scheme="ISBN">9780679744399</dc:identifier>\n'
        '    <dc:identifier id="other">07553c70</dc:identifier>',
        unique="nonexistent",
    )
    assert identifier == "9780679744399"


def test_no_identifier_is_absent_rather_than_empty(tmp_path: Path) -> None:
    assert _identifier(tmp_path, "") is None


def _ncx_uid(archive: EpubArchive, package) -> str | None:
    from lxml import etree

    ncx_path = next((h for h in package.manifest.values() if h.endswith(".ncx")), None)
    if ncx_path is None or ncx_path not in archive:
        return None
    root = etree.fromstring(archive.get(ncx_path))
    for el in root.iter("{http://www.daisy.org/z3986/2005/ncx/}meta"):
        if el.get("name") == "dtb:uid":
            return el.get("content")
    return None


@pytest.mark.realbook
def test_real_book_ncx_agreement_never_gets_worse(real_epub: Path, tmp_path: Path) -> None:
    """Rendering must not break an NCX that agreed with its OPF.

    Deliberately "no worse" rather than "correct". Sources disagree with their own
    NCX all the time -- *The Crossing* ships an ISBN there and a UUID in the OPF,
    and epubcheck says so about the source itself -- and repairing that is
    ``sync_ncx_identifier``'s job on the upgrade path, not a promise made about
    every book.

    What must never happen is the reverse, and it did: reading the first
    ``dc:identifier`` instead of the one ``@unique-identifier`` names took *All the
    Pretty Horses*, whose NCX was correct, and rewrote it to the ISBN -- turning a
    clean book into an NCX-001 error.
    """
    from joven.render import render_epub

    source = EpubArchive.read(real_epub)
    source_package = read_package(source)
    source_uid = _ncx_uid(source, source_package)
    if source_uid is None:
        pytest.skip(f"{real_epub.name} has no NCX dtb:uid")
    if source_uid != source_package.metadata.get("identifier"):
        pytest.skip(f"{real_epub.name} already disagrees with its own NCX")

    result = render_epub(real_epub, None, tmp_path / "out", make_kepub=False)
    produced = EpubArchive.read(result.epub_path)
    produced_package = read_package(produced)
    assert _ncx_uid(produced, produced_package) == produced_package.metadata.get("identifier")
