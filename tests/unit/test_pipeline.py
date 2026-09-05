"""The pipeline's shape: workers, progress, and scoping a scan.

Everything here uses the stub backend, so it is offline and deterministic — which
is exactly what makes "four workers produce the same trace as one" testable.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from joven.detect.pipeline import Progress, detect
from joven.epub.archive import EpubArchive
from joven.epub.document import iter_text_units
from joven.trace import Outcome, Tracer
from joven.translate import StubTranslator, Verdict


class SlowStub(StubTranslator):
    """The stub, with a random delay so a pool actually reorders completions."""

    def adjudicate(self, text: str, context: str = "") -> Verdict:
        time.sleep(random.uniform(0, 0.01))
        return super().adjudicate(text, context)

    def translate(self, text: str, context: str = "") -> Verdict:
        time.sleep(random.uniform(0, 0.01))
        return super().translate(text, context)


def _trace_shape(tracer: Tracer) -> list[tuple[str, int, int, str]]:
    return [(d.href, d.para_index, d.segment_index, d.outcome) for d in tracer.records]


# --------------------------------------------------------------------- workers


def test_workers_do_not_change_the_result_or_the_trace_order(sample_epub: Path) -> None:
    """The model may answer out of order; nothing is recorded out of order."""
    with Tracer() as one:
        sidecar_one, _ = detect(sample_epub, translator=SlowStub(), tracer=one, workers=1)
    with Tracer() as four:
        sidecar_four, _ = detect(sample_epub, translator=SlowStub(), tracer=four, workers=4)

    assert _trace_shape(one) == _trace_shape(four)
    assert [(a.id, a.translation) for a in sidecar_one.annotations] == [
        (a.id, a.translation) for a in sidecar_four.annotations
    ]
    assert sidecar_one.annotations, "the fixture must produce something to compare"


def test_more_workers_than_paragraphs_is_fine(sample_epub: Path) -> None:
    sidecar, result = detect(sample_epub, translator=StubTranslator(), limit=2, workers=16)
    assert result.paragraphs_scanned == 2


# -------------------------------------------------------------------- progress


def test_progress_is_reported_once_per_paragraph_in_order(sample_epub: Path) -> None:
    seen: list[Progress] = []
    _, result = detect(sample_epub, translator=StubTranslator(), on_progress=seen.append)

    assert len(seen) == result.paragraphs_total == result.paragraphs_scanned
    assert [p.paragraphs_done for p in seen] == list(range(1, len(seen) + 1))
    assert all(p.paragraphs_total == result.paragraphs_total for p in seen)
    assert seen[-1].annotated == len(result.annotations)
    assert seen[-1].segments == result.segments_scanned
    assert seen[-1].escalated > 0, "the stub is called for accepts and the band alike"


def test_progress_stays_in_order_on_a_pool(sample_epub: Path) -> None:
    seen: list[Progress] = []
    detect(sample_epub, translator=SlowStub(), workers=4, on_progress=seen.append)
    assert [p.paragraphs_done for p in seen] == list(range(1, len(seen) + 1))


def test_progress_counts_recalled_answers(sample_epub: Path) -> None:
    from joven.trace import reusable_answers

    with Tracer() as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)
    recorded = reusable_answers(tracer.records)

    seen: list[Progress] = []
    _, result = detect(
        sample_epub, translator=StubTranslator(), resume=recorded, on_progress=seen.append
    )
    assert seen[-1].recalled == result.llm_recalled == len(recorded)
    assert seen[-1].escalated == 0, "everything was answered from the trace"


# ----------------------------------------------------------------------- scope


def test_hrefs_restricts_the_scan_to_those_documents(sample_epub: Path) -> None:
    archive = EpubArchive.read(sample_epub)
    part2 = "OEBPS/part2.xhtml"
    expected = len(iter_text_units(archive.get(part2), part2))

    with Tracer() as tracer:
        _, result = detect(sample_epub, translator=StubTranslator(), tracer=tracer, hrefs={part2})

    assert result.paragraphs_total == result.paragraphs_scanned == expected
    assert {d.href for d in tracer.records} == {part2}


def test_range_counts_the_way_limit_does(sample_epub: Path) -> None:
    _, by_limit = detect(sample_epub, translator=StubTranslator(), limit=3)
    _, by_range = detect(sample_epub, translator=StubTranslator(), paragraph_range=(0, 3))
    assert by_range.paragraphs_scanned == by_limit.paragraphs_scanned == 3
    assert [a.id for a in by_range.annotations] == [a.id for a in by_limit.annotations]


def test_range_is_a_slice_over_the_scanned_paragraphs(sample_epub: Path) -> None:
    with Tracer() as whole:
        detect(sample_epub, translator=StubTranslator(), tracer=whole)
    with Tracer() as part:
        _, result = detect(
            sample_epub, translator=StubTranslator(), tracer=part, paragraph_range=(2, 5)
        )

    assert result.paragraphs_scanned == 3
    addresses = sorted({(d.href, d.para_index) for d in whole.records})
    assert sorted({(d.href, d.para_index) for d in part.records}) == addresses[2:5]


def test_open_ended_range(sample_epub: Path) -> None:
    _, everything = detect(sample_epub, translator=StubTranslator())
    _, tail = detect(sample_epub, translator=StubTranslator(), paragraph_range=(4, None))
    assert tail.paragraphs_scanned == everything.paragraphs_scanned - 4


def test_progress_total_reflects_the_scope(sample_epub: Path) -> None:
    seen: list[Progress] = []
    detect(sample_epub, translator=StubTranslator(), limit=2, on_progress=seen.append)
    assert [p.paragraphs_total for p in seen] == [2, 2]


# ------------------------------------------------------------------ thresholds


class EchoTranslator(StubTranslator):
    """Says Spanish and hands the text straight back — a no-op "translation"."""

    def adjudicate(self, text: str, context: str = "") -> Verdict:
        return Verdict(is_spanish=True, spanish_text=text, translation=text, model="echo")

    translate = adjudicate


def test_similarity_veto_threshold_is_threaded_through(sample_epub: Path) -> None:
    with Tracer() as default:
        detect(sample_epub, translator=EchoTranslator(), tracer=default)
    with Tracer() as lenient:
        detect(sample_epub, translator=EchoTranslator(), tracer=lenient, similarity_veto=1.01)

    assert Outcome.TIER2_VETOED in {d.outcome for d in default.records}
    assert Outcome.TIER2_VETOED not in {d.outcome for d in lenient.records}
