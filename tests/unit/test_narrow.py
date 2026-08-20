"""Marker placement: the narrowing fix for tier-1 accepts."""

from __future__ import annotations

from etx.detect.pipeline import _narrow
from etx.detect.segment import segment


def _seg(paragraph: str, index: int = 0):
    return segment(paragraph)[index]


def test_marker_moves_before_the_english_dialogue_tag() -> None:
    """``Gracias, he said.`` used to put the marker after the tag."""
    para = "Gracias, he said."
    seg = _seg(para)
    narrowed = _narrow(seg, "Gracias,")
    assert para[narrowed.start : narrowed.end] == "Gracias,"
    assert narrowed.end < seg.end


def test_offsets_stay_absolute_within_the_paragraph() -> None:
    para = "She held open the door. Gracias, he said."
    seg = segment(para)[1]
    narrowed = _narrow(seg, "Gracias,")
    assert para[narrowed.start : narrowed.end] == "Gracias,"


def test_full_segment_is_left_alone() -> None:
    seg = _seg("Se fué.")
    assert _narrow(seg, "Se fué.") is seg


def test_unmatched_span_falls_back_to_the_whole_segment() -> None:
    """A marker in the right place matters less than one in a valid place."""
    seg = _seg("Se fué.")
    assert _narrow(seg, "something the model invented") is seg


def test_empty_span_falls_back() -> None:
    seg = _seg("Se fué.")
    assert _narrow(seg, "") is seg
    assert _narrow(seg, "   ") is seg


def test_narrowed_segment_keeps_its_index() -> None:
    seg = segment("One. Dos, he said.")[1]
    assert _narrow(seg, "Dos,").index == seg.index
