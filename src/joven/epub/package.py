"""Minimal OPF (package document) reading.

Only what the pipeline actually needs: where the OPF lives, the spine order, and
enough metadata for ``joven inspect``. The EPUB 2->3 upgrade in M3 will extend this.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from lxml import etree

from .archive import CONTAINER_PATH, EpubArchive, EpubError

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


@dataclass(slots=True)
class Package:
    """The parsed OPF: version, metadata, and spine hrefs (archive-relative)."""

    opf_path: str
    version: str
    metadata: dict[str, str] = field(default_factory=dict)
    spine_hrefs: list[str] = field(default_factory=list)
    manifest: dict[str, str] = field(default_factory=dict)  # id -> archive-relative href

    @property
    def is_epub3(self) -> bool:
        return self.version.startswith("3")


def _opf_path(archive: EpubArchive) -> str:
    root = etree.fromstring(archive.get(CONTAINER_PATH))
    rootfile = root.find(f".//{{{CONTAINER_NS}}}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise EpubError(f"{CONTAINER_PATH} does not declare a rootfile full-path")
    return rootfile.get("full-path")


def read_package(archive: EpubArchive) -> Package:
    opf_path = _opf_path(archive)
    if opf_path not in archive:
        raise EpubError(f"OPF declared at {opf_path!r} is missing from the archive")

    root = etree.fromstring(archive.get(opf_path))
    base = posixpath.dirname(opf_path)

    def resolve(href: str) -> str:
        return posixpath.normpath(posixpath.join(base, href)) if base else href

    metadata: dict[str, str] = {}
    for tag in ("title", "creator", "language", "publisher", "identifier"):
        el = root.find(f".//{{{DC_NS}}}{tag}")
        if el is not None and el.text:
            metadata[tag] = el.text.strip()

    manifest = {
        item.get("id"): resolve(item.get("href"))
        for item in root.findall(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item")
        if item.get("id") and item.get("href")
    }

    spine_hrefs = [
        manifest[idref]
        for ref in root.findall(f".//{{{OPF_NS}}}spine/{{{OPF_NS}}}itemref")
        if (idref := ref.get("idref")) in manifest
    ]

    return Package(
        opf_path=opf_path,
        version=root.get("version", "unknown"),
        metadata=metadata,
        spine_hrefs=spine_hrefs,
        manifest=manifest,
    )
