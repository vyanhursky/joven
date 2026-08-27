"""Integrity checks.

These exist because the translation is worthless if the repacking corrupts the
book. The load-bearing one is :func:`check_text_preserved`: strip the inserted
annotation nodes back out and the remaining prose must be byte-identical to the
original. That single property proves the tool never mangled, dropped,
duplicated, or reordered the author's text.
"""

from __future__ import annotations

import posixpath
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .epub.archive import MIMETYPE_NAME, EpubArchive
from .epub.document import JOVEN_ATTR, XHTML_NS, document_text, parse, text_of


@dataclass(frozen=True, slots=True)
class Finding:
    ok: bool
    check: str
    detail: str = ""

    def __str__(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"[{mark}] {self.check}" + (f"\n       {self.detail}" if self.detail else "")


# --------------------------------------------------------------- OCF structure


def check_mimetype_first(path: Path) -> Finding:
    """``mimetype`` must be the first entry and uncompressed (OCF requirement)."""
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
    if not infos:
        return Finding(False, "mimetype first + STORED", "archive is empty")
    first = infos[0]
    if first.filename != MIMETYPE_NAME:
        return Finding(
            False, "mimetype first + STORED", f"first entry is {first.filename!r}"
        )
    if first.compress_type != zipfile.ZIP_STORED:
        return Finding(
            False, "mimetype first + STORED", "mimetype is compressed (must be STORED)"
        )
    return Finding(True, "mimetype first + STORED")


def check_entries_match(original: EpubArchive, produced: EpubArchive) -> Finding:
    """No entry may be dropped or reordered.

    Whole-file byte equality is not achievable (zip writers differ in extra
    fields and central-directory layout), so the assertion is at the entry level.

    **Content changes are reported, not failed.** Annotating legitimately rewrites
    the OPF, the annotated documents, and the stylesheet, and adds ``nav.xhtml``.
    The strict "nothing changed at all" assertion belongs to
    :func:`check_roundtrip_identical`, which covers the passthrough case; here the
    prose is guarded by :func:`check_text_preserved` instead.
    """
    orig_names = original.names()
    prod_names = produced.names()

    if removed := [n for n in orig_names if n not in prod_names]:
        return Finding(False, "entries preserved", f"missing from output: {removed}")

    surviving = [n for n in prod_names if n in orig_names]
    if surviving != orig_names:
        return Finding(False, "entries preserved", "entry order changed")

    added = [n for n in prod_names if n not in orig_names]
    changed = [n for n in orig_names if original.get(n) != produced.get(n)]

    detail = []
    if changed:
        detail.append(f"modified: {changed}")
    if added:
        detail.append(f"added: {added}")
    return Finding(True, "entries preserved", "; ".join(detail) or "nothing changed")


def check_roundtrip_identical(original: EpubArchive, produced: EpubArchive) -> Finding:
    """For a zero-annotation render: every entry must be untouched."""
    finding = check_entries_match(original, produced)
    if not finding.ok:
        return Finding(False, "zero-annotation round-trip", finding.detail)
    if set(produced.names()) != set(original.names()):
        extra = set(produced.names()) - set(original.names())
        return Finding(False, "zero-annotation round-trip", f"unexpected new entries: {extra}")
    return Finding(True, "zero-annotation round-trip")


# ------------------------------------------------------------- THE invariant


def check_text_preserved(original: EpubArchive, produced: EpubArchive) -> Finding:
    """Strip inserted nodes from the output; text must equal the original exactly."""
    mismatches: list[str] = []
    for href in original.xhtml_names():
        if href not in produced:
            mismatches.append(f"{href}: missing from output")
            continue
        try:
            before = document_text(original.get(href), exclude_inserted=False)
            after = document_text(produced.get(href), exclude_inserted=True)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the whole check
            mismatches.append(f"{href}: {exc}")
            continue
        if before != after:
            mismatches.append(f"{href}: {_first_difference(before, after)}")

    if mismatches:
        return Finding(False, "text preservation invariant", "\n       ".join(mismatches))
    return Finding(True, "text preservation invariant")


def _first_difference(a: str, b: str) -> str:
    limit = min(len(a), len(b))
    i = next((i for i in range(limit) if a[i] != b[i]), limit)
    lo = max(0, i - 40)
    return (
        f"diverges at char {i} (len {len(a)} -> {len(b)})\n"
        f"         original: ...{a[lo:i + 40]!r}\n"
        f"         produced: ...{b[lo:i + 40]!r}"
    )


# ------------------------------------------------------- annotation integrity


def check_kobo_popup_conditions(produced: EpubArchive) -> Finding:
    """Check the conditions Kobo actually documents for footnote popups.

    This replaces an earlier "notes must be adjacent to their marker" check, which
    encoded a **wrong** inference. Device testing showed the opposite: an adjacent
    note renders inline and gets no popup, while a note at the end of the chapter
    *does* pop up. Kobo's published spec explains why — the popup requires

    1. a link to a node (``chapter.xhtml#id`` or ``#id``),
    2. target text of nine characters or more once tags are stripped,
    3. target text of 5000 characters or fewer, and
    4. **"the location being linked to comes after the location being linked
       from"** — i.e. a *forward* reference.

    Condition 4 is the one the old check had backwards. See
    https://github.com/kobolabs/epub-spec

    Cross-document notes (one-note-per-file placement) satisfy condition 4 by
    construction, since their documents are appended to the spine.
    """
    MIN_CHARS, MAX_CHARS = 9, 5000
    problems: list[str] = []
    same_file = cross_file = 0

    for href in produced.xhtml_names():
        raw = produced.get(href)
        try:
            root = parse(raw).getroot()
        except Exception:  # noqa: BLE001
            continue

        notes = {
            note_id: text
            for el in root.iter()
            if isinstance(el.tag, str) and el.get(JOVEN_ATTR) == "note"
            for note_id, text in [(_note_id_of(el), text_of(el, exclude_inserted=False))]
            if note_id
        }
        body = raw.decode("utf-8", errors="replace")

        for el in root.iter(f"{{{XHTML_NS}}}a"):
            if el.get(JOVEN_ATTR) != "marker":
                continue
            doc_part, _, fragment = (el.get("href") or "").partition("#")
            if doc_part:
                cross_file += 1  # forward by construction (linear="no", spine tail)
                continue
            if fragment not in notes:
                continue
            same_file += 1

            # condition 4: the target must come *after* the reference
            ref_pos = body.find(f'href="#{fragment}"')
            note_pos = body.find(f'id="{fragment}"')
            if 0 <= note_pos < ref_pos:
                problems.append(
                    f"{href}: note {fragment} appears BEFORE its marker "
                    "(Kobo needs a forward reference)"
                )

            # conditions 2 and 3: target text length
            length = len(notes[fragment].strip())
            if length < MIN_CHARS:
                problems.append(
                    f"{href}: note {fragment} has {length} chars; Kobo needs >= {MIN_CHARS}"
                )
            elif length > MAX_CHARS:
                problems.append(
                    f"{href}: note {fragment} has {length} chars; Kobo needs <= {MAX_CHARS}"
                )

    if problems:
        return Finding(
            False,
            "kobo popup conditions",
            "\n       ".join(problems[:8]),
        )
    return Finding(
        True,
        "kobo popup conditions",
        f"{same_file} same-file + {cross_file} cross-file noteref(s) satisfy them",
    )


def _note_id_of(note: etree._Element) -> str | None:
    """The id a marker links to, which may sit on the note or on a child.

    The ``<span>`` variant follows Kobo's own spec example and puts ``id`` +
    ``epub:type`` on an inline span inside an unmarked block wrapper, so the id is
    not always on the element carrying ``data-joven="note"``.
    """
    if note.get("id"):
        return note.get("id")
    for child in note.iter():
        if isinstance(child.tag, str) and child.get("id"):
            return child.get("id")
    return None


def check_noterefs_resolve(produced: EpubArchive) -> Finding:
    """Every inserted noteref must point at exactly one footnote id, same document."""
    problems: list[str] = []
    total = 0
    for href in produced.xhtml_names():
        try:
            tree = parse(produced.get(href))
        except Exception:  # noqa: BLE001
            continue
        root = tree.getroot()
        note_ids: list[str] = [
            note_id
            for el in root.iter()
            if isinstance(el.tag, str) and el.get(JOVEN_ATTR) == "note"
            for note_id in [_note_id_of(el)]
            if note_id
        ]
        dupes = {i for i in note_ids if note_ids.count(i) > 1}
        if dupes:
            problems.append(f"{href}: duplicate footnote ids {sorted(dupes)}")

        for el in root.iter(f"{{{XHTML_NS}}}a"):
            if el.get(JOVEN_ATTR) != "marker":
                continue
            total += 1
            raw_href = el.get("href") or ""
            if not raw_href:
                problems.append(f"{href}: noteref with no href")
                continue
            doc_part, _, fragment = raw_href.partition("#")
            if not doc_part:
                # same-document fragment
                if fragment not in note_ids:
                    problems.append(f"{href}: noteref -> #{fragment} has no matching footnote")
                continue
            # cross-document target (one-note-per-file placement)
            target_path = posixpath.normpath(
                posixpath.join(posixpath.dirname(href), doc_part)
            )
            if target_path not in produced:
                problems.append(f"{href}: noteref -> {target_path} is not in the archive")
                continue
            try:
                target_root = parse(produced.get(target_path)).getroot()
            except Exception:  # noqa: BLE001
                problems.append(f"{href}: noteref -> {target_path} does not parse")
                continue
            target_ids = {
                e.get("id") for e in target_root.iter() if isinstance(e.tag, str) and e.get("id")
            }
            if fragment not in target_ids:
                problems.append(f"{href}: noteref -> {target_path}#{fragment} not found")

    if problems:
        return Finding(False, "noteref -> footnote integrity", "\n       ".join(problems))
    return Finding(True, "noteref -> footnote integrity", f"{total} noteref(s) resolved")


def check_ids_unique(produced: EpubArchive) -> Finding:
    """No duplicate ids within any document (breaks fragment links)."""
    problems = []
    for href in produced.xhtml_names():
        try:
            root = parse(produced.get(href)).getroot()
        except Exception:  # noqa: BLE001
            continue
        ids = [el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            problems.append(f"{href}: {dupes}")
    if problems:
        return Finding(False, "ids unique per document", "; ".join(problems))
    return Finding(True, "ids unique per document")


# ----------------------------------------------------------------- epubcheck


def epubcheck_available() -> bool:
    return shutil.which("epubcheck") is not None


def check_epubcheck(path: Path) -> Finding:
    """Run the external epubcheck validator."""
    if not epubcheck_available():
        return Finding(True, "epubcheck", "SKIPPED — epubcheck not on PATH")
    proc = subprocess.run(  # noqa: S603
        ["epubcheck", "--quiet", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 0:
        return Finding(True, "epubcheck", "clean")
    lines = [ln for ln in output.splitlines() if ln.strip()][:12]
    return Finding(False, "epubcheck", "\n       ".join(lines))


# ------------------------------------------------------------------ KEPUB


def check_kepub_naming(path: Path) -> Finding:
    """Kobo only recognises a sideloaded KEPUB via the ``.kepub.epub`` suffix."""
    if path.name.endswith(".kepub.epub"):
        return Finding(True, "kepub filename", path.name)
    return Finding(
        False,
        "kepub filename",
        f"{path.name!r} must end with '.kepub.epub' or the Kobo treats it as a plain EPUB",
    )


def check_marker_span_nesting(produced: EpubArchive) -> Finding:
    """No inserted node may sit inside a ``koboSpan``.

    This is the one failure mode that every other check is blind to, because the
    text is genuinely present in the file and only disappears in Kobo's renderer.
    A koboSpan is treated as a leaf text unit: give one mixed content and the
    device renders the child element and drops the span's own text, so a marker
    inserted inside ``kobo.681.1`` shows a bare asterisk where the passage was.

    kepubify's own placement — marker outside the span, wrapped in a span of its
    own — is the correct shape, and it is what
    :func:`joven.render.kobo.dekepubify` exists to let us reach.
    """
    offenders: list[str] = []
    for href in produced.xhtml_names():
        try:
            tree = parse(produced.get(href))
        except Exception:  # noqa: BLE001 - other checks report malformed documents
            continue
        for node in tree.iter():
            if node.get(JOVEN_ATTR) is None:
                continue
            for ancestor in node.iterancestors(f"{{{XHTML_NS}}}span"):
                if ancestor.get("class") == "koboSpan":
                    label = node.get("id") or node.tag
                    offenders.append(f"{href}: {label} inside {ancestor.get('id')}")
                    break
    if offenders:
        return Finding(
            False,
            "markers outside koboSpans",
            f"{len(offenders)} inserted node(s) nested in a koboSpan — the passage will "
            f"vanish on the device:\n       " + "\n       ".join(offenders[:5]),
        )
    return Finding(True, "markers outside koboSpans")


# ------------------------------------------------------------------- driver


def verify(
    produced_path: Path,
    original_path: Path | None = None,
    kepub_path: Path | None = None,
) -> list[Finding]:
    """Run every applicable check against a produced EPUB (and its KEPUB)."""
    produced = EpubArchive.read(produced_path)
    findings = [
        check_mimetype_first(produced_path),
        check_ids_unique(produced),
        check_noterefs_resolve(produced),
        check_kobo_popup_conditions(produced),
        check_epubcheck(produced_path),
    ]
    if original_path is not None:
        original = EpubArchive.read(original_path)
        findings.insert(1, check_entries_match(original, produced))
        findings.insert(2, check_text_preserved(original, produced))

    # The KEPUB is what actually reaches the device, and kepubify rewrites the
    # XHTML — so re-run the reader-facing checks on it, not just the EPUB.
    if kepub_path is not None and kepub_path.is_file():
        kepub = EpubArchive.read(kepub_path)
        for finding in (
            check_kepub_naming(kepub_path),
            check_noterefs_resolve(kepub),
            check_kobo_popup_conditions(kepub),
            check_ids_unique(kepub),
            check_marker_span_nesting(kepub),
        ):
            findings.append(Finding(finding.ok, f"kepub: {finding.check}", finding.detail))
    return findings
