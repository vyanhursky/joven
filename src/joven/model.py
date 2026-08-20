"""The annotation sidecar — the project's durable artifact.

The EPUB is never edited in place. ``annotations.json`` holds every decision, and
rendering is a pure idempotent function of (original epub + sidecar). That is what
makes corrections cheap: edit the sidecar, re-render, never re-translate.

Two properties carry the design:

**Content-derived IDs.** An annotation's id is a hash of (document, normalized
source text, occurrence). Positional indices would scramble the moment detection
settings changed, destroying every manual fix; content hashes survive re-runs.

**Sticky human edits.** Re-running detection overwrites ``auto`` entries only.
``approved`` / ``edited`` / ``rejected`` are never touched, so manual work
accumulates monotonically and re-detection is always safe.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

SIDECAR_VERSION = 1

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Whitespace-collapsed form used for identity and matching."""
    return _WS.sub(" ", text).strip()


def annotation_id(href: str, source_text: str, occurrence: int = 0) -> str:
    """Stable, position-independent identity for one annotation.

    ``occurrence`` disambiguates repeated text and is **not optional in practice**
    — see :func:`occurrence_indices`.
    """
    payload = f"{href}\x00{normalize(source_text)}\x00{occurrence}".encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def occurrence_indices(texts: Iterable[str]) -> list[int]:
    """Number repeated paragraph texts within a document, in document order.

    McCarthy repeats short dialogue relentlessly: in *The Crossing*, ``Yessir.``
    appears 21 times in one document, ``No.`` 11 times, ``Yes mam.`` 10 times —
    **245 paragraphs share their text with another paragraph in the same file.**

    Without an occurrence number every one of those collapses to a single id, and
    because :meth:`Sidecar.merge` keys on id, detecting ``Sí.`` twelve times would
    keep one annotation and silently discard eleven. The bug would surface as
    "most of my footnotes are missing" with nothing in the logs.

    The ordinal is position-independent in the way that matters: the *n*-th
    ``Sí.`` stays the *n*-th ``Sí.`` when detection thresholds change, so ids —
    and therefore human edits — survive re-runs.
    """
    seen: Counter[str] = Counter()
    indices = []
    for text in texts:
        key = normalize(text)
        indices.append(seen[key])
        seen[key] += 1
    return indices


class Status(StrEnum):
    """Lifecycle of a single annotation.

    Only ``AUTO`` is machine-owned. The other three are human decisions and are
    preserved across re-detection.
    """

    AUTO = "auto"          # produced by the pipeline, safe to overwrite
    APPROVED = "approved"  # human confirmed as-is
    EDITED = "edited"      # human rewrote the translation
    REJECTED = "rejected"  # human said "this is not Spanish, never annotate it"

    @property
    def is_human(self) -> bool:
        return self is not Status.AUTO

    @property
    def renders(self) -> bool:
        return self is not Status.REJECTED


@dataclass(slots=True)
class Annotation:
    """One footnote: a paragraph, the Spanish runs inside it, and a translation."""

    id: str
    href: str
    para_index: int
    source_text: str
    spans: list[tuple[int, int]]
    marker_offset: int
    translation: str
    detector_confidence: float = 0.0
    model: str = ""
    status: Status = Status.AUTO
    note: str = ""

    @classmethod
    def create(
        cls,
        *,
        href: str,
        para_index: int,
        source_text: str,
        spans: list[tuple[int, int]],
        translation: str,
        marker_offset: int | None = None,
        occurrence: int = 0,
        **kwargs: object,
    ) -> Annotation:
        if not spans:
            raise ValueError("an annotation needs at least one span")
        offset = marker_offset if marker_offset is not None else max(e for _, e in spans)
        return cls(
            id=annotation_id(href, source_text, occurrence),
            href=href,
            para_index=para_index,
            source_text=source_text,
            spans=[tuple(s) for s in spans],  # type: ignore[misc]
            marker_offset=offset,
            translation=translation,
            **kwargs,  # type: ignore[arg-type]
        )

    @property
    def spanish_text(self) -> str:
        """The Spanish runs joined, for display in review."""
        return " ".join(self.source_text[a:b].strip() for a, b in self.spans)

    def validate_against(self, paragraph_text: str) -> None:
        """Fail loudly if the sidecar no longer matches the book.

        Guards against a sidecar built from a different edition, or spans that
        would slice mid-character after an upstream change.
        """
        if normalize(paragraph_text) != normalize(self.source_text):
            raise ValueError(
                f"{self.id}: source text drifted at {self.href}#{self.para_index}\n"
                f"  sidecar: {normalize(self.source_text)[:80]!r}\n"
                f"  book:    {normalize(paragraph_text)[:80]!r}"
            )
        limit = len(paragraph_text)
        for start, end in self.spans:
            if not (0 <= start <= end <= limit):
                raise ValueError(f"{self.id}: span ({start}, {end}) outside 0..{limit}")
        if not 0 <= self.marker_offset <= limit:
            raise ValueError(f"{self.id}: marker_offset {self.marker_offset} outside 0..{limit}")

    # ------------------------------------------------------------- json shape

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "href": self.href,
            "para_index": self.para_index,
            "source_text": self.source_text,
            "spans": [list(s) for s in self.spans],
            "marker_offset": self.marker_offset,
            "translation": self.translation,
            "detector_confidence": round(self.detector_confidence, 4),
            "model": self.model,
            "status": str(self.status),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Annotation:
        return cls(
            id=raw["id"],
            href=raw["href"],
            para_index=int(raw["para_index"]),
            source_text=raw["source_text"],
            spans=[tuple(s) for s in raw["spans"]],
            marker_offset=int(raw["marker_offset"]),
            translation=raw["translation"],
            detector_confidence=float(raw.get("detector_confidence", 0.0)),
            model=raw.get("model", ""),
            status=Status(raw.get("status", "auto")),
            note=raw.get("note", ""),
        )


@dataclass(slots=True)
class Sidecar:
    """The whole annotation set for one book."""

    source_sha256: str = ""
    title: str = ""
    annotations: list[Annotation] = field(default_factory=list)
    version: int = SIDECAR_VERSION

    # ---------------------------------------------------------------- queries

    def renderable(self) -> list[Annotation]:
        return [a for a in self.annotations if a.status.renders]

    def by_document(self) -> dict[str, list[Annotation]]:
        grouped: dict[str, list[Annotation]] = {}
        for annotation in self.renderable():
            grouped.setdefault(annotation.href, []).append(annotation)
        for items in grouped.values():
            # render order matters: later offsets first would corrupt earlier ones,
            # so keep document order here and let the renderer walk it deliberately
            items.sort(key=lambda a: (a.para_index, a.marker_offset))
        return grouped

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys((str(s) for s in Status), 0)
        for annotation in self.annotations:
            counts[str(annotation.status)] += 1
        return counts

    # ---------------------------------------------------------------- merging

    def merge(self, incoming: list[Annotation]) -> dict[str, int]:
        """Fold a fresh detection run into this sidecar without losing human work.

        ``auto`` entries are replaced, human-owned entries are left alone, and a
        ``rejected`` id is never re-added no matter how confident the detector is.
        """
        existing = {a.id: a for a in self.annotations}
        stats = {"added": 0, "updated": 0, "kept_human": 0, "suppressed": 0}

        for candidate in incoming:
            current = existing.get(candidate.id)
            if current is None:
                existing[candidate.id] = candidate
                stats["added"] += 1
            elif current.status is Status.REJECTED:
                stats["suppressed"] += 1
            elif current.status.is_human:
                stats["kept_human"] += 1
            else:
                existing[candidate.id] = replace(candidate, status=Status.AUTO)
                stats["updated"] += 1

        self.annotations = sorted(existing.values(), key=lambda a: (a.href, a.para_index))
        return stats

    # ------------------------------------------------------------------- i/o

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "source": {"sha256": self.source_sha256, "title": self.title},
            "annotations": [a.to_dict() for a in self.annotations],
        }
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> Sidecar:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        version = int(raw.get("version", 1))
        if version > SIDECAR_VERSION:
            raise ValueError(
                f"sidecar version {version} is newer than this tool supports "
                f"({SIDECAR_VERSION}) — upgrade joven"
            )
        source = raw.get("source", {})
        return cls(
            source_sha256=source.get("sha256", ""),
            title=source.get("title", ""),
            annotations=[Annotation.from_dict(a) for a in raw.get("annotations", [])],
            version=version,
        )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
