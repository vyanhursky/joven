"""Shared fixtures.

The synthetic EPUB deliberately mirrors the quirks of the real target book (see
DESIGN.md §1): EPUB 2.0, flat ``<p class="calibre4">`` paragraphs with no inline
markup, single-quoted XML declarations, and a Calibre-style split spine. Tests
run against this so the copyrighted book never needs to be committed.
"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

import pytest

XML_DECL = "<?xml version='1.0' encoding='utf-8'?>"

CONTAINER_XML = f"""{XML_DECL}
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CONTENT_OPF = f"""{XML_DECL}
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uuid_id">
  <metadata xmlns:opf="http://www.idpf.org/2007/opf"
            xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Test Book</dc:title>
    <dc:creator opf:role="aut">Testy McTest</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="uuid_id" opf:scheme="uuid"
      >urn:uuid:5b5f8a1e-2c4d-4f8a-9b3e-7d6c1a2f4e90</dc:identifier>
  </metadata>
  <manifest>
    <item href="OEBPS/part1.xhtml" id="part1" media-type="application/xhtml+xml"/>
    <item href="OEBPS/part2.xhtml" id="part2" media-type="application/xhtml+xml"/>
    <item href="toc.ncx" id="ncx" media-type="application/x-dtbncx+xml"/>
    <item href="stylesheet1.css" id="css1" media-type="text/css"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="part1"/>
    <itemref idref="part2"/>
  </spine>
</package>
"""

TOC_NCX = f"""{XML_DECL}
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta content="urn:uuid:5b5f8a1e-2c4d-4f8a-9b3e-7d6c1a2f4e90" name="dtb:uid"/></head>
  <docTitle><text>Test Book</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1">
      <navLabel><text>One</text></navLabel>
      <content src="OEBPS/part1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""

STYLESHEET = ".calibre4 { margin: 0; text-indent: 1em; }\n"


def _doc(paragraphs: list[str]) -> str:
    body = "\n".join(f'      <p class="calibre4">{p}</p>' for p in paragraphs)
    return f"""{XML_DECL}
<html xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <title>Test Book</title>
    <link href="../stylesheet1.css" type="text/css" rel="stylesheet"/>
  </head>
  <body class="calibre3">
{body}
  </body>
</html>
"""


# Mirrors the four mixing patterns from DESIGN.md §1.1
PART1_PARAGRAPHS = [
    "He turned the horse out along the rutted track and rode on.",
    "Se fué.",  # A: whole-paragraph Spanish
    "Ay. Ándale, joven. Ándale pues.",
    "The boy withdrew his hand and he rose.",
    "Cuántos años tienes? the old man said.",  # B: Spanish + English tag
    "Dieciseis.",
]

PART2_PARAGRAPHS = [
    # C: English prose with a Spanish loanword — must NOT be annotated
    "The matríz will not help you, the old man said. He said that the boy should "
    "find that place where acts of God and those of man are of a piece.",
    # D: Spanish opener then English narration — only the opener is Spanish
    "Escúchame, joven, the old man wheezed. If you could breathe a breath so "
    "strong you could blow out the wolf.",
    "Y cómo se encuentra?",
    "Yes mam.",  # English, but statistically Spanish-leaning
    "Go on.",  # English, but statistically Spanish-leaning
]

ENTRIES: list[tuple[str, str, int]] = [
    ("mimetype", "application/epub+zip", zipfile.ZIP_STORED),
    ("META-INF/container.xml", CONTAINER_XML, zipfile.ZIP_DEFLATED),
    ("content.opf", CONTENT_OPF, zipfile.ZIP_DEFLATED),
    ("OEBPS/part1.xhtml", _doc(PART1_PARAGRAPHS), zipfile.ZIP_DEFLATED),
    ("OEBPS/part2.xhtml", _doc(PART2_PARAGRAPHS), zipfile.ZIP_DEFLATED),
    ("toc.ncx", TOC_NCX, zipfile.ZIP_DEFLATED),
    ("stylesheet1.css", STYLESHEET, zipfile.ZIP_DEFLATED),
]


def build_epub(path: Path, *, extra: dict[str, str] | None = None) -> Path:
    """Write a minimal, spec-valid EPUB 2.0 to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, text, compress in ENTRIES:
            info = zipfile.ZipInfo(filename=name, date_time=(2010, 7, 28, 0, 0, 0))
            info.compress_type = compress
            info.external_attr = 0o644 << 16
            zf.writestr(info, text.encode("utf-8"))
        for name, text in (extra or {}).items():
            zf.writestr(name, text.encode("utf-8"))
    return path


@pytest.fixture
def sample_epub(tmp_path: Path) -> Path:
    return build_epub(tmp_path / "sample.epub")


@pytest.fixture
def drm_epub(tmp_path: Path) -> Path:
    return build_epub(
        tmp_path / "drm.epub",
        extra={"META-INF/encryption.xml": '<encryption xmlns="urn:x"/>'},
    )


@pytest.fixture
def real_epub() -> Path:
    """The actual target book, opt-in via JOVEN_TEST_EPUB (never committed)."""
    raw = os.environ.get("JOVEN_TEST_EPUB")
    if not raw:
        pytest.skip("set JOVEN_TEST_EPUB to run tests against the real book")
    path = Path(raw)
    if not path.is_file():
        pytest.skip(f"JOVEN_TEST_EPUB does not point at a file: {path}")
    return path


# --------------------------------------------------- book-agnostic passage lookup

# Spanish orthography that English never uses. Deliberately *not* the project's
# own Triager: a render test that depended on the detector would fail for the
# wrong reason whenever detection changed, and would be circular besides.
_SPANISH_ORTHOGRAPHY = re.compile(r"[áéíóúüñÁÉÍÓÚÜÑ¿¡]")


def find_spanish_units(
    archive, package, *, count: int = 3, max_chars: int = 140
) -> list[tuple[str, int, str, tuple[int, int]]]:
    """Locate paragraphs *containing* Spanish orthography, without naming any.

    Matches on the presence of Spanish characters, not on the paragraph being
    wholly Spanish — so mixed paragraphs like ``She looked at him. El señor? she
    said.`` qualify. That is deliberate: mixed paragraphs are §1.1's patterns C and
    D, the realistic hard case for offset arithmetic, and exactly what a render
    test should be inserting into.

    Returns ``(href, para_index, source_text, span)`` tuples.

    The real-book tests used to hardcode their targets — one by Calibre filename
    (``part2_split_000``), one by exact phrase (``"Se fué."``). Both are properties
    of one *edition* of one *book*, but the fixture's contract is "whatever EPUB
    ``JOVEN_TEST_EPUB`` points at". So the filename lookup died with StopIteration on
    a differently-produced edition of the same novel, and the phrase lookup only
    survived because those words happen to appear in both — it would raise KeyError
    on any other book.

    Deriving the targets instead makes the gate mean what it claims: this pipeline
    works on a real publisher's EPUB, whichever one you hand it.
    """
    from joven.epub.document import iter_text_units

    found: list[tuple[str, int, str, tuple[int, int]]] = []
    for href in package.spine_hrefs:
        if href not in archive:
            continue
        for unit in iter_text_units(archive.get(href), href):
            stripped = unit.text.strip()
            # Short paragraphs keep the assertions readable and are overwhelmingly
            # dialogue, which is where this book's Spanish lives.
            if not stripped or len(stripped) > max_chars:
                continue
            if not _SPANISH_ORTHOGRAPHY.search(stripped):
                continue
            start = unit.text.find(stripped)
            found.append((href, unit.index, unit.text, (start, start + len(stripped))))
            if len(found) >= count:
                return found
    return found


@pytest.fixture
def spanish_units(real_epub: Path):
    """Three paragraphs containing Spanish, from whichever book is under test.

    Skips rather than fails when a book has none — that is a property of the book,
    not a defect in the code being tested.
    """
    from joven.epub.archive import EpubArchive
    from joven.epub.package import read_package

    archive = EpubArchive.read(real_epub)
    units = find_spanish_units(archive, read_package(archive))
    if len(units) < 3:
        pytest.skip(f"{real_epub.name} has no Spanish passages to annotate")
    return units
