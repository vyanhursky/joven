"""The calibration generator must produce exactly the lengths it claims.

If a rung is mislabelled, the device reading is worthless — so this is checked
rather than eyeballed.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tools")
from make_calibration import LENGTHS, TARGETS, calibration_text  # noqa: E402


@pytest.mark.parametrize("length", LENGTHS)
def test_text_is_exactly_the_requested_length(length: int) -> None:
    assert len(calibration_text(length)) == length


@pytest.mark.parametrize("length", LENGTHS)
def test_text_states_its_own_length(length: int) -> None:
    """The popup has to be self-describing, or you cannot read off the threshold."""
    assert calibration_text(length).startswith(f"{length:03d}:")


def test_ladder_brackets_the_observed_transition() -> None:
    """25 chars popped up, 99 jumped — the ladder must have rungs either side."""
    assert min(LENGTHS) <= 25
    assert any(25 < n < 99 for n in LENGTHS)
    assert max(LENGTHS) > 99


def test_ladder_is_ascending_and_matches_targets() -> None:
    assert sorted(LENGTHS) == LENGTHS
    assert len(LENGTHS) == len(TARGETS)


def test_every_rung_clears_kobos_nine_char_minimum() -> None:
    """Kobo needs >= 9 chars of target text or it will not preview at all."""
    assert all(n >= 9 for n in LENGTHS)


def test_too_short_to_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="too short"):
        calibration_text(2)
