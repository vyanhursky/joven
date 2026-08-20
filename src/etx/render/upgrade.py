"""EPUB 2.0 -> 3.0 package upgrade.

Popup footnotes are an EPUB 3 feature (``epub:type="noteref"`` /
``epub:type="footnote"``), and the target book is Calibre-produced EPUB 2.0, so
the package has to be lifted before the renderer's markup means anything.

Deliberately minimal — the upgrade touches only what EPUB 3 *requires*:

* ``<package version="3.0">``
* a ``dcterms:modified`` metadata property (mandatory in 3.0)
* a nav document, generated from the existing ``toc.ncx``
* ``properties="nav"`` on that manifest item

``toc.ncx`` is **kept**: EPUB 3 permits it for backward compatibility and Kobo
reads it. Removing it would be a bigger change with nothing to gain.
"""

from __future__ import annotations

import posixpath
from datetime import UTC, datetime

from lxml import etree

from ..epub.archive import EpubArchive
from ..epub.document import XHTML_NS, parse, serialize
from ..epub.package import DC_NS, OPF_NS, Package

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
NAV_FILENAME = "nav.xhtml"

# The footnote CSS is no longer a constant: how the note is hidden turned out to
# be reader-dependent (a display:none target has no position for Kobo to navigate
# to), so each renderer supplies its own rules via ``Renderer.css()``.
ETX_CSS_MARKER = "etx-note"


def _timestamp() -> str:
    """EPUB 3 requires dcterms:modified as UTC with no fractional seconds."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_nav(archive: EpubArchive, package: Package) -> bytes:
    """Generate an EPUB 3 nav document from ``toc.ncx``, or from the spine."""
    entries: list[tuple[str, str]] = []  # (href relative to nav, label)

    ncx_path = next((h for h in package.manifest.values() if h.endswith(".ncx")), None)
    if ncx_path and ncx_path in archive:
        root = etree.fromstring(archive.get(ncx_path))
        ncx_base = posixpath.dirname(ncx_path)
        for point in root.iter(f"{{{NCX_NS}}}navPoint"):
            label = point.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
            content = point.find(f"{{{NCX_NS}}}content")
            if content is None or not content.get("src"):
                continue
            src = content.get("src")
            absolute = posixpath.normpath(posixpath.join(ncx_base, src)) if ncx_base else src
            entries.append((absolute, (label.text or "").strip() if label is not None else "—"))

    if not entries:  # no usable ncx — fall back to the spine
        entries = [(href, posixpath.basename(href)) for href in package.spine_hrefs]

    # nav.xhtml sits alongside the OPF, so hrefs are relative to the OPF directory
    opf_base = posixpath.dirname(package.opf_path)

    def relative(target: str) -> str:
        return posixpath.relpath(target, opf_base) if opf_base else target

    items = "\n".join(
        f'        <li><a href="{relative(href)}">{_escape(label)}</a></li>'
        for href, label in entries
    )
    title = _escape(package.metadata.get("title", "Contents"))

    return f"""<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head>
    <title>{title}</title>
  </head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>{title}</h1>
      <ol>
{items}
      </ol>
    </nav>
  </body>
</html>
""".encode()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fix_content_type_meta(archive: EpubArchive, package: Package) -> list[str]:
    """Repair Calibre's malformed ``http-equiv="Content-Type"`` meta elements.

    Calibre 2.x emitted::

        <meta content="http://www.w3.org/1999/xhtml; charset=utf-8"
              http-equiv="Content-Type"/>

    which is meaningless (that's a namespace URI, not a MIME type). EPUB 2 never
    checked; EPUB 3 is HTML5-based and requires the value to be exactly
    ``text/html; charset=utf-8``, so epubcheck rejects all eight documents:

        ERROR(RSC-005): The meta element in encoding declaration state
        (http-equiv='content-type') must have the value "text/html; charset=utf-8"

    We introduced this failure by declaring EPUB 3, so we fix it rather than
    tolerate it — an integrity gate that reports known-failing output stops being
    a gate, and real errors would hide in the noise.

    Note this touches documents we otherwise would not, which is unavoidable: the
    package version is a book-wide property. The text invariant is unaffected
    (``<meta>`` lives in ``<head>`` and carries no prose).
    """
    fixed: list[str] = []
    correct = "text/html; charset=utf-8"

    for href in archive.xhtml_names():
        if href not in archive:  # pragma: no cover - defensive
            continue
        original = archive.get(href)
        tree = parse(original)
        changed = False
        for meta in tree.getroot().iter(f"{{{XHTML_NS}}}meta"):
            equiv = (meta.get("http-equiv") or "").lower()
            if equiv == "content-type" and meta.get("content") != correct:
                meta.set("content", correct)
                changed = True
            # Adobe Digital Editions toolchain leftover, present in the Knopf
            # edition on all 14 documents:
            #
            #     <meta name="Adept.resource" value="urn:uuid:e195acf2-..."/>
            #
            # `value` is not a permitted attribute on <meta> — XHTML wants
            # `content` — so epubcheck rejects every document (RSC-005). The
            # element is a dead identifier from Adobe's production pipeline (the
            # book carries no encryption.xml and is not DRM-protected), so it is
            # dropped rather than rewritten: preserving a meaningless UUID under a
            # corrected attribute name would be tidier markup carrying the same
            # noise.
            if meta.get("value") is not None and meta.get("content") is None:
                meta.getparent().remove(meta)
                changed = True
        if changed:
            archive.replace(href, serialize(tree, original=original))
            fixed.append(href)
    return fixed


def prune_stale_guide(archive: EpubArchive, opf: etree._Element, opf_base: str) -> list[str]:
    """Drop ``<guide>`` references to resources that are missing or not content.

    The Knopf edition points its guide at a table-of-contents document that is not
    in the archive at all, and at a JPEG as though it were a content document:

        ERROR(RSC-007): Referenced resource "…_epub_toc_r1.htm" could not be
        found in the EPUB.
        ERROR(OPF-032): Guide references "…_epub_cvt_r1.jpg" which is not a
        valid "OPS Content Document".

    Both are safe to remove rather than repair. ``<guide>`` is superseded in EPUB 3
    by the navigation document, which :func:`build_nav` already generates — so the
    landmarks it was carrying are preserved by the replacement, and a reference to
    a file that does not exist carries nothing to preserve in the first place.
    """
    removed: list[str] = []
    for guide in list(opf.iter(f"{{{OPF_NS}}}guide")):
        for ref in list(guide):
            href = (ref.get("href") or "").split("#", 1)[0]
            if not href:
                continue
            target = posixpath.normpath(posixpath.join(opf_base, href)) if opf_base else href
            missing = target not in archive
            not_content = href.lower().endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".svg", ".css")
            )
            if missing or not_content:
                guide.remove(ref)
                removed.append(href)
        if len(guide) == 0:
            parent = guide.getparent()
            if parent is not None:
                parent.remove(guide)
    return removed


# EPUB 3 requires manifest items to advertise these features.
_FEATURE_NAMESPACES = {
    "svg": "http://www.w3.org/2000/svg",
    "mathml": "http://www.w3.org/1998/Math/MathML",
}


def declare_manifest_properties(
    archive: EpubArchive, opf: etree._Element, opf_base: str
) -> list[str]:
    """Add ``properties="svg"`` / ``"mathml"`` where a document uses them.

    The book's cover page embeds SVG, which EPUB 3 requires the manifest to
    declare:

        ERROR(OPF-014): The property "svg" should be declared in the OPF file.
    """
    declared: list[str] = []
    manifest = opf.find(f"{{{OPF_NS}}}manifest")
    if manifest is None:  # pragma: no cover - validated by caller
        return declared

    for item in manifest.findall(f"{{{OPF_NS}}}item"):
        if item.get("media-type") != "application/xhtml+xml":
            continue
        href = item.get("href") or ""
        archive_path = posixpath.normpath(posixpath.join(opf_base, href)) if opf_base else href
        if archive_path not in archive:
            continue
        body = archive.get(archive_path)
        existing = set((item.get("properties") or "").split())
        for feature, namespace in _FEATURE_NAMESPACES.items():
            if namespace.encode() in body and feature not in existing:
                existing.add(feature)
                declared.append(f"{archive_path}:{feature}")
        if existing:
            item.set("properties", " ".join(sorted(existing)))
    return declared


def modernize_metadata(metadata: etree._Element) -> list[str]:
    """Remove EPUB 2 ``opf:*`` attributes, which are errors in EPUB 3.

    EPUB 2 hung refinements directly on Dublin Core elements
    (``<dc:creator opf:role="aut" opf:file-as="McCarthy, Cormac">``). EPUB 3
    replaced that with ``<meta property="..." refines="#id">`` and epubcheck
    rejects the old form outright:

        ERROR(RSC-005): attribute "opf:role" not allowed here
        ERROR(RSC-005): found attribute "opf:scheme", but no attributes allowed here

    We **strip** rather than convert. Converting would mean minting ids for every
    Dublin Core element and emitting matching ``refines`` metas — more code, more
    to get wrong, for attributes that are purely informational on a reading
    device: ``role="aut"`` is the default assumption, ``scheme="uuid"``/``"ISBN"``
    describes an identifier nobody resolves, and Kobo derives its own sort keys
    from the title and author strings.

    Returns the attribute names removed, for reporting.
    """
    removed: list[str] = []
    for element in metadata.iter():
        for name in list(element.attrib):
            if name.startswith(f"{{{OPF_NS}}}"):
                local = name.split("}", 1)[1]
                # 'refines' and 'property' are legitimate EPUB 3 spellings
                if local in {"refines", "property", "scheme-3"}:
                    continue
                del element.attrib[name]
                removed.append(f"opf:{local}")
    return removed


def upgrade_package(archive: EpubArchive, package: Package) -> bool:
    """Lift the OPF to EPUB 3.0 and register a nav document, in place.

    Returns True if anything changed. Safe to call on a book that is already
    EPUB 3 — it will only fill in a missing nav or ``dcterms:modified``.
    """
    opf = etree.fromstring(archive.get(package.opf_path))
    changed = False

    if opf.get("version") != "3.0":
        opf.set("version", "3.0")
        changed = True

    metadata = opf.find(f"{{{OPF_NS}}}metadata")
    if metadata is None:
        metadata = etree.SubElement(opf, f"{{{OPF_NS}}}metadata")
        metadata.set(f"{{{DC_NS}}}dummy", "")  # pragma: no cover - degenerate OPF
        changed = True

    if modernize_metadata(metadata):
        changed = True

    # dcterms:modified is mandatory in EPUB 3 and must appear exactly once
    existing = [
        el
        for el in metadata.findall(f"{{{OPF_NS}}}meta")
        if el.get("property") == "dcterms:modified"
    ]
    if not existing:
        meta = etree.SubElement(metadata, f"{{{OPF_NS}}}meta")
        meta.set("property", "dcterms:modified")
        meta.text = _timestamp()
        changed = True

    manifest = opf.find(f"{{{OPF_NS}}}manifest")
    if manifest is None:
        raise ValueError(f"{package.opf_path} has no manifest")

    opf_base = posixpath.dirname(package.opf_path)
    if declare_manifest_properties(archive, opf, opf_base):
        changed = True

    if prune_stale_guide(archive, opf, opf_base):
        changed = True

    # EPUB 3 requires exactly one manifest item with properties="nav"
    has_nav = any(
        "nav" in (item.get("properties") or "").split()
        for item in manifest.findall(f"{{{OPF_NS}}}item")
    )
    if not has_nav:
        nav_archive_path = posixpath.join(opf_base, NAV_FILENAME) if opf_base else NAV_FILENAME
        if nav_archive_path not in archive:
            archive.add(nav_archive_path, build_nav(archive, package), after=package.opf_path)
        item = etree.SubElement(manifest, f"{{{OPF_NS}}}item")
        item.set("id", "etx-nav")
        item.set("href", NAV_FILENAME)
        item.set("media-type", "application/xhtml+xml")
        item.set("properties", "nav")
        changed = True

    if changed:
        archive.replace(
            package.opf_path,
            serialize(etree.ElementTree(opf), original=archive.get(package.opf_path)),
        )
    return changed


def ensure_epub_namespace(root: etree._Element) -> bool:
    """Make sure ``epub:`` is declared on an XHTML root element.

    lxml cannot add a namespace declaration to an existing element, so the tree
    has to be rebuilt with the new nsmap. Returns True if a rebuild is needed.
    """
    return "epub" not in (root.nsmap or {})


def add_epub_namespace(tree: etree._ElementTree) -> etree._ElementTree:
    """Return an equivalent tree with ``xmlns:epub`` declared on the root."""
    root = tree.getroot()
    nsmap = dict(root.nsmap or {})
    nsmap["epub"] = "http://www.idpf.org/2007/ops"
    nsmap.setdefault(None, XHTML_NS)

    rebuilt = etree.Element(root.tag, attrib=dict(root.attrib), nsmap=nsmap)
    rebuilt.text = root.text
    rebuilt.tail = root.tail
    for child in root:
        rebuilt.append(child)
    return etree.ElementTree(rebuilt)


def register_stylesheet(archive: EpubArchive, package: Package, css: str) -> str | None:
    """Append the renderer's footnote CSS to the book's first stylesheet.

    Appending to the existing sheet rather than adding a new file keeps the
    manifest untouched, which keeps the diff (and epubcheck's opinion) small.

    ``css`` comes from the active renderer, because the correct way to keep a note
    out of the reading flow is reader-dependent — see ``FootnoteRenderer.hide``.
    """
    css_paths = [h for h in package.manifest.values() if h.lower().endswith(".css")]
    if not css_paths:
        return None
    target = css_paths[0]
    current = archive.get(target)
    if ETX_CSS_MARKER.encode() in current:
        return target  # already applied
    archive.replace(target, current.rstrip() + b"\n" + css.encode())
    return target


def register_documents(
    archive: EpubArchive, package: Package, documents: list[tuple[str, bytes]]
) -> list[str]:
    """Add generated XHTML documents to the archive and the OPF manifest.

    Used by the one-note-per-file placement.

    They go in the manifest **and** the spine with ``linear="no"``. Manifest alone
    is not enough — epubcheck rejects a link from a spine document to a
    non-spine resource:

        ERROR(RSC-011): Found a reference to a resource that is not a spine item.

    ``linear="no"`` is precisely the mechanism for this: the document is part of
    the publication and reachable by link, but excluded from the linear reading
    order, so hundreds of note stubs do not appear as pages you can page into.
    """
    if not documents:
        return []

    opf = etree.fromstring(archive.get(package.opf_path))
    manifest = opf.find(f"{{{OPF_NS}}}manifest")
    if manifest is None:
        raise ValueError(f"{package.opf_path} has no manifest")

    spine = opf.find(f"{{{OPF_NS}}}spine")
    if spine is None:
        raise ValueError(f"{package.opf_path} has no spine")

    opf_base = posixpath.dirname(package.opf_path)
    existing = {item.get("href") for item in manifest.findall(f"{{{OPF_NS}}}item")}
    added: list[str] = []

    for index, (archive_path, body) in enumerate(documents):
        if archive_path in archive:
            continue
        archive.add(archive_path, body)
        href = posixpath.relpath(archive_path, opf_base) if opf_base else archive_path
        if href in existing:
            continue
        item_id = f"etx-note-{index}"
        item = etree.SubElement(manifest, f"{{{OPF_NS}}}item")
        item.set("id", item_id)
        item.set("href", href)
        item.set("media-type", "application/xhtml+xml")

        itemref = etree.SubElement(spine, f"{{{OPF_NS}}}itemref")
        itemref.set("idref", item_id)
        itemref.set("linear", "no")

        existing.add(href)
        added.append(archive_path)

    archive.replace(
        package.opf_path,
        serialize(etree.ElementTree(opf), original=archive.get(package.opf_path)),
    )
    return added
