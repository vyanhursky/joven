"""Lossless EPUB archive read/write.

The whole tool rests on one promise: *change only what must change*. This module
keeps that promise at the zip level.

Why not `ebooklib`: it re-serializes XHTML it was never asked to touch, silently
reflowing markup. Here every entry is carried through as raw bytes and only the
documents that actually gained annotations are ever replaced.

Two OCF rules that are easy to break and annoying to debug:

1. ``mimetype`` must be the **first** entry and **STORED** (uncompressed).
2. Entry order and per-entry compression should otherwise be preserved, so a
   diff of the output against the input shows only intended changes.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

MIMETYPE_NAME = "mimetype"
EPUB_MIMETYPE = b"application/epub+zip"
ENCRYPTION_PATH = "META-INF/encryption.xml"
CONTAINER_PATH = "META-INF/container.xml"

XMLENC_NS = "http://www.w3.org/2001/04/xmlenc#"

# `encryption.xml` is not a DRM marker. It is also how the two font-obfuscation
# schemes declare themselves, and unencumbered trade EPUBs carry it routinely to
# scramble an embedded typeface. Refusing on the file's presence rejects those
# books with advice to strip DRM that was never there.
#
# The honest test is the algorithm. Obfuscation is a fixed pair of well-known
# URIs; everything else -- AES and friends -- is real encryption we cannot read
# through. Testing the algorithm rather than guessing from the resource path also
# avoids having to decide what "looks like a font", and keeps this layer from
# needing to know what the spine is.
OBFUSCATION_ALGORITHMS = frozenset({
    "http://www.idpf.org/2008/embedding",  # IDPF / EPUB font obfuscation
    "http://ns.adobe.com/pdf/enc#RC",      # Adobe font obfuscation
})


class EpubError(Exception):
    """Raised when an EPUB, or something inside it, is not usable."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One zip member, carried through verbatim unless explicitly replaced."""

    name: str
    data: bytes
    compress_type: int = zipfile.ZIP_DEFLATED
    date_time: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)
    external_attr: int = 0
    create_system: int = 3  # unix
    comment: bytes = b""

    @property
    def is_mimetype(self) -> bool:
        return self.name == MIMETYPE_NAME


class EpubArchive:
    """An EPUB held in memory as an ordered list of entries.

    Read it, optionally ``replace``/``add`` a few documents, write it back. Any
    entry you don't touch is written out with byte-identical content.
    """

    def __init__(self, entries: list[Entry]) -> None:
        self._entries = list(entries)

    # ---------------------------------------------------------------- reading

    @classmethod
    def read(cls, path: str | Path) -> EpubArchive:
        path = Path(path)
        if not path.is_file():
            raise EpubError(f"not a file: {path}")
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise EpubError(f"not a zip archive: {path}") from exc

        with zf:
            if (bad := zf.testzip()) is not None:
                raise EpubError(f"corrupt archive: first bad entry is {bad!r}")
            entries = [
                Entry(
                    name=info.filename,
                    data=zf.read(info),
                    compress_type=info.compress_type,
                    date_time=info.date_time,
                    external_attr=info.external_attr,
                    create_system=info.create_system,
                    comment=info.comment,
                )
                for info in zf.infolist()
                if not info.is_dir()
            ]

        archive = cls(entries)
        archive._validate()
        return archive

    def _validate(self) -> None:
        names = self.names()
        if MIMETYPE_NAME not in names:
            raise EpubError("missing 'mimetype' entry — not an EPUB")
        mimetype = self.get(MIMETYPE_NAME).strip()
        if mimetype != EPUB_MIMETYPE:
            raise EpubError(f"unexpected mimetype: {mimetype!r}")
        if CONTAINER_PATH not in names:
            raise EpubError(f"missing {CONTAINER_PATH} — not an EPUB")
        if ENCRYPTION_PATH in names:
            self._validate_encryption(self.get(ENCRYPTION_PATH))

    def _validate_encryption(self, data: bytes) -> None:
        """Refuse real encryption; carry obfuscated fonts through untouched.

        Obfuscated fonts need no special handling here — every entry this tool
        does not annotate is copied out byte for byte, so a scrambled font stays
        exactly as scrambled as the reader expects it to be.
        """
        from lxml import etree  # local: keeps the zip layer importable without lxml

        try:
            root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False))
        except etree.XMLSyntaxError as exc:
            raise EpubError(f"{ENCRYPTION_PATH} is present but unreadable: {exc}") from exc

        encrypted: list[str] = []
        for element in root.iter(f"{{{XMLENC_NS}}}EncryptedData"):
            method = element.find(f"{{{XMLENC_NS}}}EncryptionMethod")
            algorithm = method.get("Algorithm", "") if method is not None else ""
            if algorithm in OBFUSCATION_ALGORITHMS:
                continue
            reference = element.find(
                f"{{{XMLENC_NS}}}CipherData/{{{XMLENC_NS}}}CipherReference"
            )
            target = reference.get("URI", "?") if reference is not None else "?"
            encrypted.append(f"{target} ({algorithm or 'unspecified algorithm'})")

        if encrypted:
            listed = "\n         ".join(encrypted[:5])
            more = f"\n         ... and {len(encrypted) - 5} more" if len(encrypted) > 5 else ""
            raise EpubError(
                "this EPUB is encrypted — the following resources cannot be read:\n"
                f"         {listed}{more}\n"
                "Remove the DRM first; this tool will not produce usable output."
            )

    # ---------------------------------------------------------------- access

    def names(self) -> list[str]:
        return [e.name for e in self._entries]

    def __contains__(self, name: str) -> bool:
        return any(e.name == name for e in self._entries)

    def get(self, name: str) -> bytes:
        for entry in self._entries:
            if entry.name == name:
                return entry.data
        raise KeyError(name)

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    def xhtml_names(self) -> list[str]:
        return [n for n in self.names() if n.lower().endswith((".xhtml", ".html", ".htm"))]

    # ---------------------------------------------------------------- mutation

    def replace(self, name: str, data: bytes) -> None:
        """Swap one entry's bytes, keeping its position and compression."""
        for i, entry in enumerate(self._entries):
            if entry.name == name:
                self._entries[i] = replace(entry, data=data)
                return
        raise KeyError(name)

    def add(self, name: str, data: bytes, *, after: str | None = None) -> None:
        """Insert a new entry, optionally directly after an existing one."""
        if name in self:
            raise EpubError(f"entry already exists: {name}")
        new = Entry(name=name, data=data)
        if after is None:
            self._entries.append(new)
            return
        for i, entry in enumerate(self._entries):
            if entry.name == after:
                self._entries.insert(i + 1, new)
                return
        raise KeyError(after)

    # ---------------------------------------------------------------- writing

    def write(self, path: str | Path) -> None:
        """Write the archive, enforcing the OCF mimetype rule.

        Written to a temp file and moved into place so an interrupted run can't
        leave a half-written book behind.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")

        ordered = self._mimetype_first()
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for entry in ordered:
                    info = zipfile.ZipInfo(filename=entry.name, date_time=entry.date_time)
                    # mimetype must be STORED per OCF; everything else keeps its original setting
                    info.compress_type = (
                        zipfile.ZIP_STORED if entry.is_mimetype else entry.compress_type
                    )
                    info.external_attr = entry.external_attr
                    info.create_system = entry.create_system
                    info.comment = entry.comment
                    zf.writestr(info, entry.data)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def _mimetype_first(self) -> list[Entry]:
        mimetype = [e for e in self._entries if e.is_mimetype]
        rest = [e for e in self._entries if not e.is_mimetype]
        if not mimetype:
            raise EpubError("cannot write an EPUB without a 'mimetype' entry")
        return mimetype + rest
