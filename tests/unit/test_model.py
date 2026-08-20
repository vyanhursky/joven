"""Sidecar identity, merge semantics, and round-tripping.

The merge rules are the correction workflow: human decisions must survive
re-detection, or the tool destroys your work every time you re-run it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from etx.model import Annotation, Sidecar, Status, annotation_id, normalize


def _ann(text: str = "Se fué.", *, index: int = 1, translation: str = "He is gone.", **kw):
    return Annotation.create(
        href="OEBPS/part1.xhtml",
        para_index=index,
        source_text=text,
        spans=[(0, len(text))],
        translation=translation,
        **kw,
    )


# ------------------------------------------------------------------ identity


def test_id_is_stable_across_calls() -> None:
    assert annotation_id("a.xhtml", "Se fué.") == annotation_id("a.xhtml", "Se fué.")


def test_id_ignores_whitespace_differences() -> None:
    """Re-flowed markup must not change identity."""
    assert annotation_id("a.xhtml", "Se  fué.\n") == annotation_id("a.xhtml", "Se fué.")


def test_id_is_position_independent() -> None:
    """The whole point: re-detection may renumber paragraphs, ids must not move."""
    assert _ann(index=5).id == _ann(index=900).id


def test_id_varies_by_document_and_text_and_occurrence() -> None:
    base = annotation_id("a.xhtml", "Se fué.", 0)
    assert base != annotation_id("b.xhtml", "Se fué.", 0)
    assert base != annotation_id("a.xhtml", "Está bien.", 0)
    assert base != annotation_id("a.xhtml", "Se fué.", 1)


def test_normalize_collapses_whitespace() -> None:
    assert normalize("  a \n\t b  ") == "a b"


# ---------------------------------------------------------------- annotation


def test_marker_defaults_to_end_of_last_span() -> None:
    annotation = Annotation.create(
        href="a.xhtml",
        para_index=1,
        source_text="Cuántos años tienes? the old man said.",
        spans=[(0, 20)],
        translation="How old are you?",
    )
    assert annotation.marker_offset == 20


def test_spanish_text_joins_spans() -> None:
    source = "Escúchame, joven, he said. Yo no sé nada."
    annotation = Annotation.create(
        href="a.xhtml",
        para_index=1,
        source_text=source,
        spans=[(0, 17), (27, len(source))],
        translation="x",
    )
    # span (0, 17) ends just past the comma: "Escúchame, joven,"
    assert annotation.spanish_text == "Escúchame, joven, Yo no sé nada."


def test_annotation_requires_a_span() -> None:
    with pytest.raises(ValueError, match="at least one span"):
        Annotation.create(
            href="a.xhtml", para_index=1, source_text="x", spans=[], translation="y"
        )


def test_validate_rejects_drift_and_bad_spans() -> None:
    annotation = _ann()
    annotation.validate_against("Se fué.")  # exact
    annotation.validate_against("Se  fué.\n")  # whitespace-insensitive

    with pytest.raises(ValueError, match="drifted"):
        annotation.validate_against("Something else.")

    annotation.spans = [(0, 999)]
    with pytest.raises(ValueError, match="outside"):
        annotation.validate_against("Se fué.")


def test_status_semantics() -> None:
    assert Status.AUTO.is_human is False
    assert all(s.is_human for s in (Status.APPROVED, Status.EDITED, Status.REJECTED))
    assert Status.REJECTED.renders is False
    assert all(s.renders for s in (Status.AUTO, Status.APPROVED, Status.EDITED))


# -------------------------------------------------------------------- merge


def test_merge_adds_new() -> None:
    sidecar = Sidecar()
    stats = sidecar.merge([_ann()])
    assert stats["added"] == 1
    assert len(sidecar.annotations) == 1


def test_merge_overwrites_auto() -> None:
    sidecar = Sidecar(annotations=[_ann(translation="old")])
    stats = sidecar.merge([_ann(translation="new")])
    assert stats["updated"] == 1
    assert sidecar.annotations[0].translation == "new"


@pytest.mark.parametrize("status", [Status.EDITED, Status.APPROVED])
def test_merge_never_touches_human_translations(status: Status) -> None:
    kept = _ann(translation="my careful wording")
    kept.status = status
    sidecar = Sidecar(annotations=[kept])

    stats = sidecar.merge([_ann(translation="machine output")])
    assert stats["kept_human"] == 1
    assert sidecar.annotations[0].translation == "my careful wording"
    assert sidecar.annotations[0].status is status


def test_merge_never_resurrects_rejected() -> None:
    """A rejected passage stays rejected however confident the detector becomes."""
    rejected = _ann()
    rejected.status = Status.REJECTED
    sidecar = Sidecar(annotations=[rejected])

    stats = sidecar.merge([_ann(detector_confidence=1.0)])
    assert stats["suppressed"] == 1
    assert sidecar.annotations[0].status is Status.REJECTED
    assert sidecar.renderable() == []


def test_merge_is_idempotent() -> None:
    sidecar = Sidecar()
    incoming = [_ann(), _ann(text="Está bien.", index=2, translation="Fine.")]
    sidecar.merge(incoming)
    before = [a.to_dict() for a in sidecar.annotations]
    sidecar.merge(incoming)
    assert [a.to_dict() for a in sidecar.annotations] == before


def test_renderable_excludes_only_rejected() -> None:
    annotations = []
    for i, status in enumerate(Status, start=1):
        a = _ann(text=f"Texto {i}.", index=i)
        a.status = status
        annotations.append(a)
    sidecar = Sidecar(annotations=annotations)
    assert len(sidecar.renderable()) == len(list(Status)) - 1


def test_by_document_groups_and_orders() -> None:
    a = _ann(text="Uno.", index=3)
    b = _ann(text="Dos.", index=1)
    sidecar = Sidecar(annotations=[a, b])
    grouped = sidecar.by_document()
    assert [x.para_index for x in grouped["OEBPS/part1.xhtml"]] == [1, 3]


def test_counts() -> None:
    a, b = _ann(), _ann(text="Otra.", index=2)
    b.status = Status.EDITED
    assert Sidecar(annotations=[a, b]).counts() == {
        "auto": 1,
        "approved": 0,
        "edited": 1,
        "rejected": 0,
    }


# ---------------------------------------------------------------------- i/o


def test_save_load_round_trip(tmp_path: Path) -> None:
    original = Sidecar(source_sha256="abc", title="Test")
    original.annotations = [_ann(), _ann(text="Está bien.", index=2, translation="Fine.")]
    original.annotations[1].status = Status.EDITED

    path = tmp_path / "annotations.json"
    original.save(path)
    loaded = Sidecar.load(path)

    assert loaded.source_sha256 == "abc"
    assert loaded.title == "Test"
    assert [a.to_dict() for a in loaded.annotations] == [
        a.to_dict() for a in original.annotations
    ]


def test_save_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "annotations.json"
    Sidecar().save(path)
    assert not list(tmp_path.glob("*.partial"))


def test_load_rejects_future_version(tmp_path: Path) -> None:
    path = tmp_path / "annotations.json"
    path.write_text('{"version": 99, "annotations": []}')
    with pytest.raises(ValueError, match="newer than this tool"):
        Sidecar.load(path)


def test_non_ascii_survives_json(tmp_path: Path) -> None:
    path = tmp_path / "annotations.json"
    sidecar = Sidecar(annotations=[_ann(text="Ándale, güero. ¿Qué?", translation="Come on.")])
    sidecar.save(path)
    assert "Ándale" in path.read_text(encoding="utf-8")  # not \u-escaped
    assert Sidecar.load(path).annotations[0].source_text == "Ándale, güero. ¿Qué?"


# ------------------------------------------------------- occurrence numbering


def test_occurrence_indices_numbers_repeats() -> None:
    from etx.model import occurrence_indices

    assert occurrence_indices(["Sí.", "No.", "Sí.", "Sí."]) == [0, 0, 1, 2]


def test_occurrence_indices_ignores_whitespace_variation() -> None:
    from etx.model import occurrence_indices

    assert occurrence_indices(["Sí.", " Sí. ", "Sí.\n"]) == [0, 1, 2]


def test_repeated_paragraphs_get_distinct_ids() -> None:
    """The 245-collision bug: repeated dialogue must not collapse to one id."""
    from etx.model import occurrence_indices

    texts = ["Yessir."] * 3
    ids = {
        annotation_id("part3.xhtml", text, occ)
        for text, occ in zip(texts, occurrence_indices(texts), strict=True)
    }
    assert len(ids) == 3


def test_merge_keeps_all_repeated_paragraphs() -> None:
    """Without occurrence numbering this silently kept 1 of 3."""
    from etx.model import occurrence_indices

    texts = ["Sí."] * 3
    incoming = [
        Annotation.create(
            href="part3.xhtml",
            para_index=i * 10,
            source_text=text,
            spans=[(0, 3)],
            translation="Yes.",
            occurrence=occ,
        )
        for i, (text, occ) in enumerate(zip(texts, occurrence_indices(texts), strict=True))
    ]
    sidecar = Sidecar()
    stats = sidecar.merge(incoming)
    assert stats["added"] == 3
    assert len(sidecar.annotations) == 3
