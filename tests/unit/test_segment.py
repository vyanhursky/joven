"""Sentence segmentation with exact offsets.

Offsets are load-bearing: the renderer inserts markers at character positions, so
a segmenter that loses them silently misplaces every footnote.
"""

from __future__ import annotations

import pytest

from joven.detect.segment import merge_adjacent, segment


def test_offsets_are_exact() -> None:
    para = "Se fué. Ay. Ándale, joven."
    for seg in segment(para):
        assert para[seg.start : seg.end] == seg.text


@pytest.mark.parametrize(
    "para",
    [
        "Se fué.",
        "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
        "Cuántos años tienes? the old man said.",
        "One. Two! Three? Four.",
        "  leading and trailing  ",
        "No terminal punctuation",
    ],
)
def test_nothing_is_lost(para: str) -> None:
    """Every non-space character must appear in exactly one segment."""
    segs = segment(para)
    joined = "".join(s.text for s in segs)
    assert "".join(joined.split()) == "".join(para.split())


def test_splits_the_three_sentence_paragraph() -> None:
    para = "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad."
    assert [s.text for s in segment(para)] == [
        "Escúchame, joven, he said.",
        "Yo no sé nada.",
        "Esto es la verdad.",
    ]


def test_does_not_split_spanish_honorifics() -> None:
    para = "Buenos días, Sr. Sanders. Cómo está?"
    assert [s.text for s in segment(para)] == ["Buenos días, Sr. Sanders.", "Cómo está?"]


def test_does_not_split_initials() -> None:
    assert len(segment("He met J. Grady there.")) == 1


def test_question_mark_splits() -> None:
    para = "Cuántos años tienes? Dieciseis."
    assert [s.text for s in segment(para)] == ["Cuántos años tienes?", "Dieciseis."]


def test_empty_and_blank() -> None:
    assert segment("") == []
    assert segment("   \n ") == []


def test_segment_indices_are_sequential() -> None:
    segs = segment("One. Two. Three.")
    assert [s.index for s in segs] == [0, 1, 2]


def test_has_letters_flags_punctuation_only() -> None:
    segs = segment("Real text. ...")
    assert segs[0].has_letters
    assert not segs[-1].has_letters


# ------------------------------------------------------------------- merging


def test_merge_joins_contiguous_segments() -> None:
    para = "Se fué. Está bien."
    segs = segment(para)
    assert merge_adjacent(segs, para) == [(0, len(para))]


def test_merge_keeps_gaps_when_english_intervenes() -> None:
    para = "Yo no sé nada. He said that. Esto es la verdad."
    segs = segment(para)
    spanish = [segs[0], segs[2]]
    spans = merge_adjacent(spanish, para)
    assert len(spans) == 2
    assert para[spans[0][0] : spans[0][1]] == "Yo no sé nada."
    assert para[spans[1][0] : spans[1][1]] == "Esto es la verdad."


def test_merge_of_nothing() -> None:
    assert merge_adjacent([], "text") == []
