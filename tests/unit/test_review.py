"""The review pass: ordering, persistence, and sticky human decisions."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from etx.model import Annotation, Sidecar, Status
from etx.review import ReviewState, _payload, _segments, build_context


def _ann(text: str, translation: str, *, conf: float, index: int) -> Annotation:
    return Annotation.create(
        href="OEBPS/part1.xhtml",
        para_index=index,
        source_text=text,
        spans=[(0, len(text))],
        translation=translation,
        detector_confidence=conf,
    )


@pytest.fixture
def state(tmp_path: Path) -> ReviewState:
    sidecar = Sidecar(
        annotations=[
            _ann("Se fué.", "He is gone.", conf=1.0, index=1),
            _ann("Tan horrible.", "So horrible.", conf=0.50, index=2),
            _ann("Está bien.", "It is all right.", conf=0.88, index=3),
            # highest confidence, but its translation keeps a Spanish word — the
            # case the confidence sort put last and this one puts first
            _ann("Del ejido?", "From the ejido?", conf=1.0, index=4),
        ]
    )
    path = tmp_path / "annotations.json"
    sidecar.save(path)
    return ReviewState(sidecar=sidecar, path=path, context={}, lock=threading.Lock())


# --------------------------------------------------------------------- ordering


def test_suspect_annotations_come_first(state: ReviewState) -> None:
    """Confidence is not a predictor of error; an untranslated carry-through is.

    The fixture's ``Del ejido?`` keeps a Spanish word in its English, so it sorts
    ahead of clean translations regardless of its confidence being the highest.
    """
    order = state.ordered()
    assert order[0].source_text == "Del ejido?"
    assert state.suspicions(order[0])


def test_the_remainder_is_in_book_order(state: ReviewState) -> None:
    """Narrative order is where a translation that's wrong *for the scene* shows."""
    clean = [a for a in state.ordered() if not state.suspicions(a)]
    assert [a.para_index for a in clean] == sorted(a.para_index for a in clean)


def test_reviewed_items_sink_below_unreviewed(state: ReviewState) -> None:
    suspect = state.ordered()[0]
    suspect.status = Status.APPROVED
    order = state.ordered()
    assert order[0].id != suspect.id
    assert order[-1].status is Status.APPROVED


# ------------------------------------------------------------------ persistence


def test_approve_writes_through_to_disk(state: ReviewState) -> None:
    target = state.ordered()[0]
    state.update(target.id, "approved", None)

    reloaded = Sidecar.load(state.path)
    assert next(a for a in reloaded.annotations if a.id == target.id).status is Status.APPROVED


def test_editing_the_translation_marks_it_edited(state: ReviewState) -> None:
    """Even if the button said 'approve' — a changed translation is an edit."""
    target = state.ordered()[0]
    state.update(target.id, "approved", "My own wording.")

    reloaded = Sidecar.load(state.path)
    saved = next(a for a in reloaded.annotations if a.id == target.id)
    assert saved.translation == "My own wording."
    assert saved.status is Status.EDITED


def test_unchanged_translation_keeps_the_requested_status(state: ReviewState) -> None:
    target = state.ordered()[0]
    state.update(target.id, "approved", target.translation)
    assert target.status is Status.APPROVED


def test_reject_persists_and_stops_rendering(state: ReviewState) -> None:
    target = state.ordered()[0]
    state.update(target.id, "rejected", None)
    reloaded = Sidecar.load(state.path)
    assert target.id not in {a.id for a in reloaded.renderable()}


def test_unknown_id_raises(state: ReviewState) -> None:
    with pytest.raises(KeyError):
        state.update("nope", "approved", None)


def test_review_decisions_survive_redetection(state: ReviewState) -> None:
    """The whole reason reviewing is worth doing: merge must not clobber it."""
    target = state.sidecar.annotations[1]  # "Tan horrible."
    state.update(target.id, "rejected", None)

    fresh = _ann("Tan horrible.", "machine output", conf=0.99, index=2)
    stats = state.sidecar.merge([fresh])
    assert stats["suppressed"] == 1
    assert state.sidecar.annotations[1].status is Status.REJECTED


# ---------------------------------------------------------------- highlighting


def test_segments_split_spanish_from_english() -> None:
    source = "Cuántos años tienes? the old man said."
    a = Annotation.create(
        href="x.xhtml",
        para_index=1,
        source_text=source,
        spans=[(0, 20)],
        translation="How old are you?",
    )
    segs = _segments(a)
    assert [s["spanish"] for s in segs] == [True, False]
    assert segs[0]["text"] == "Cuántos años tienes?"
    assert segs[1]["text"] == " the old man said."


def test_segments_reconstruct_the_paragraph_exactly() -> None:
    """The review must show the real prose, not an approximation of it."""
    source = "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad."
    a = Annotation.create(
        href="x.xhtml",
        para_index=1,
        source_text=source,
        spans=[(0, 17), (27, 41)],
        translation="…",
    )
    assert "".join(s["text"] for s in _segments(a)) == source


def test_segments_handle_a_gap_before_the_spanish() -> None:
    source = "She held open the door. Gracias, he said."
    a = Annotation.create(
        href="x.xhtml",
        para_index=1,
        source_text=source,
        spans=[(24, len(source))],
        translation="Thanks, he said.",
    )
    segs = _segments(a)
    assert segs[0]["spanish"] is False
    assert segs[1]["text"] == "Gracias, he said."
    assert "".join(s["text"] for s in segs) == source


# --------------------------------------------------------------------- payload


def test_payload_is_json_serialisable_and_counts_progress(state: ReviewState) -> None:
    total = len(state.sidecar.annotations)
    state.update(state.ordered()[0].id, "approved", None)
    payload = _payload(state)
    json.dumps(payload)  # must not raise
    assert payload["total"] == total
    assert payload["reviewed"] == 1
    assert len(payload["annotations"]) == total
    assert all("segments" in a for a in payload["annotations"])


def test_context_is_optional(state: ReviewState) -> None:
    assert build_context(None, state.sidecar) == {}


def test_context_pulls_preceding_prose(sample_epub: Path) -> None:
    """Short fragments are unjudgeable without the paragraph before them."""
    sidecar = Sidecar(
        annotations=[
            Annotation.create(
                href="OEBPS/part1.xhtml",
                para_index=5,
                source_text="Cuántos años tienes? the old man said.",
                spans=[(0, 20)],
                translation="How old are you?",
            )
        ]
    )
    context = build_context(sample_epub, sidecar)
    text = context[sidecar.annotations[0].id]
    assert "withdrew his hand" in text
