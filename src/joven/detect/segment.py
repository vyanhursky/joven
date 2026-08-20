"""Sentence segmentation that preserves character offsets.

Offsets matter: the renderer inserts markers at exact character positions in the
paragraph, so a segmenter that returns bare strings is useless here — we need to
map every decision back to a span.

Hand-rolled rather than pulling in ``pysbd`` because the requirements are narrow
and specific to this book: McCarthy uses **no quotation marks**, so there is no
quote nesting to handle, but he does use Spanish honorifics (``Sr.``, ``Sra.``)
that must not end a sentence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Abbreviations that end in '.' but do not end a sentence. Lowercased, no dot.
ABBREVIATIONS = frozenset({
    # Spanish honorifics matter most here — McCarthy uses them in dialogue
    "sr", "sra", "srta",
    "dr", "mr", "mrs", "ms", "st", "jr",
    "capt", "gen", "col", "sgt", "lt", "rev", "prof",
    "no", "vs", "etc", "ie", "eg", "cf", "al", "ca",
})

# Candidate break: after . ! ? (optionally followed by closing punctuation),
# before whitespace.
_CANDIDATE = re.compile(r"[.!?]+[)\]'’”]*(?=\s)")
_WORD_BEFORE = re.compile(r"([^\W\d_]+)\.?$", re.UNICODE)
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Segment:
    """One sentence-ish unit with its offsets into the parent paragraph."""

    text: str
    start: int
    end: int
    index: int = 0

    @property
    def has_letters(self) -> bool:
        return bool(_HAS_LETTER.search(self.text))


def _is_abbreviation(text: str, dot_pos: int) -> bool:
    """True if the '.' at ``dot_pos`` belongs to a known abbreviation."""
    match = _WORD_BEFORE.search(text[:dot_pos + 1])
    if not match:
        return False
    return match.group(1).lower() in ABBREVIATIONS


def _is_initial(text: str, dot_pos: int) -> bool:
    """True for a single-letter initial like the 'J.' in 'J. Grady'."""
    match = _WORD_BEFORE.search(text[:dot_pos + 1])
    return bool(match) and len(match.group(1)) == 1


def segment(paragraph: str) -> list[Segment]:
    """Split a paragraph into sentence segments carrying their offsets.

    Guarantees, both covered by tests:

    * offsets are exact — ``paragraph[s.start:s.end] == s.text`` for every segment
    * nothing is lost — concatenating the inter-segment gaps and segments
      reproduces the paragraph
    """
    if not paragraph.strip():
        return []

    breaks: list[int] = []
    for match in _CANDIDATE.finditer(paragraph):
        dot = match.start()
        if _is_abbreviation(paragraph, dot) or _is_initial(paragraph, dot):
            continue
        breaks.append(match.end())

    segments: list[Segment] = []
    cursor = 0
    for boundary in [*breaks, len(paragraph)]:
        raw = paragraph[cursor:boundary]
        stripped = raw.strip()
        if stripped:
            offset = raw.index(stripped)
            start = cursor + offset
            segments.append(
                Segment(
                    text=stripped,
                    start=start,
                    end=start + len(stripped),
                    index=len(segments),
                )
            )
        cursor = boundary

    return segments


def merge_adjacent(segments: list[Segment], paragraph: str) -> list[tuple[int, int]]:
    """Collapse contiguous segments into merged ``(start, end)`` spans.

    Per DESIGN.md §4.5 the annotation unit is the paragraph, so a run of
    consecutive Spanish sentences becomes one footnote rather than three markers.
    Segments separated only by whitespace are treated as contiguous.
    """
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s.start)
    spans: list[list[int]] = [[ordered[0].start, ordered[0].end]]
    for seg in ordered[1:]:
        gap = paragraph[spans[-1][1]:seg.start]
        if gap.strip():
            spans.append([seg.start, seg.end])  # real text between them
        else:
            spans[-1][1] = seg.end
    return [(a, b) for a, b in spans]
