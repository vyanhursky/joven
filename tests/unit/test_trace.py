"""The decision trace — the thing that makes detection debuggable."""

from __future__ import annotations

from pathlib import Path

from joven.detect.pipeline import detect
from joven.trace import Decision, Outcome, Tracer, load_trace
from joven.translate import StubTranslator


def test_tracer_writes_jsonl_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    with Tracer(path=path) as tracer:
        tracer.record(
            Decision(
                href="a.xhtml",
                para_index=1,
                segment_index=0,
                text="Se fué.",
                start=0,
                end=7,
                tier1_confidence=1.0,
                tier1_language="SPANISH",
                outcome=Outcome.ANNOTATED,
            )
        )
    loaded = load_trace(path)
    assert len(loaded) == 1
    assert loaded[0].text == "Se fué."
    assert loaded[0].outcome == Outcome.ANNOTATED


def test_trace_records_every_segment_not_just_annotated(sample_epub: Path) -> None:
    """The whole point: dropped segments must be explainable too."""
    with Tracer() as tracer:
        _, result = detect(sample_epub, translator=StubTranslator(), tracer=tracer)

    outcomes = {r.outcome for r in tracer.records}
    assert Outcome.ANNOTATED in outcomes
    assert Outcome.TIER1_ENGLISH in outcomes, "English drops must be recorded"
    assert len(tracer.records) > len(result.annotations)


def test_trace_captures_tier1_scores(sample_epub: Path) -> None:
    with Tracer() as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)
    scored = [r for r in tracer.records if r.outcome != Outcome.TIER1_EMPTY]
    assert all(r.tier1_language for r in scored)
    assert all(0.0 <= r.tier1_confidence <= 1.0 for r in scored)
    assert all(r.tier1_verdict for r in scored)


def test_trace_marks_which_segments_reached_the_llm(sample_epub: Path) -> None:
    with Tracer() as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)
    escalated = [r for r in tracer.records if r.tier2_used]
    assert escalated, "the fixture contains band cases"
    # Both paths call the LLM: the band for adjudication, tier-1 accepts for the
    # translation itself (otherwise those render as empty popups).
    assert all(r.tier1_verdict in {"uncertain", "spanish"} for r in escalated)
    assert all(r.tier2_model for r in escalated)


def test_trace_records_llm_verdict_and_raw_output(sample_epub: Path) -> None:
    with Tracer() as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)
    escalated = [r for r in tracer.records if r.tier2_used]
    assert any(r.tier2_is_spanish is not None for r in escalated)


def test_tier1_only_run_never_calls_the_llm(sample_epub: Path) -> None:
    with Tracer() as tracer:
        detect(sample_epub, translator=None, tracer=tracer)
    assert not any(r.tier2_used for r in tracer.records)


def test_summary_and_escalation_rate(sample_epub: Path) -> None:
    with Tracer() as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)
    summary = tracer.summary()
    assert sum(summary.values()) == len(tracer.records)
    assert 0.0 <= tracer.escalation_rate() <= 1.0
    assert "segments considered" in tracer.format_report()


def test_band_samples_are_least_confident_first(sample_epub: Path) -> None:
    with Tracer() as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)
    band = tracer.band_samples(10)
    assert band == sorted(band, key=lambda r: r.tier1_confidence)


def test_detect_produces_usable_annotations(sample_epub: Path) -> None:
    sidecar, result = detect(sample_epub, translator=StubTranslator())
    assert result.annotations
    assert sidecar.annotations
    # spans must be valid against the paragraph they came from
    for annotation in sidecar.annotations:
        annotation.validate_against(annotation.source_text)


def test_detect_merges_contiguous_spanish_into_one_annotation(sample_epub: Path) -> None:
    """§4.5: one footnote per paragraph, not one per sentence."""
    _, result = detect(sample_epub, translator=StubTranslator())
    per_paragraph = {(a.href, a.para_index) for a in result.annotations}
    assert len(per_paragraph) == len(result.annotations)


def test_detect_limit_is_respected(sample_epub: Path) -> None:
    _, result = detect(sample_epub, translator=StubTranslator(), limit=3)
    assert result.paragraphs_scanned == 3


def test_vetoed_segments_are_traced_distinctly(sample_epub: Path) -> None:
    """A veto must be visible in the trace, not silently indistinguishable."""
    from dataclasses import dataclass

    from joven.translate import Verdict

    @dataclass
    class NormalizingTranslator:
        """Mimics the observed failure: rewrites dialect English as 'translation'."""

        name: str = "normalizer"

        def adjudicate(self, text: str, context: str = "") -> Verdict:
            return Verdict(
                is_spanish=True,
                spanish_text=text,
                translation=text.replace("Yes mam", "Yes ma'am"),
                model="normalizer",
            )

        def translate(self, text: str, context: str = "") -> Verdict:
            return self.adjudicate(text, context)

    with Tracer() as tracer:
        _, result = detect(sample_epub, translator=NormalizingTranslator(), tracer=tracer)

    vetoed = [r for r in tracer.records if r.outcome == Outcome.TIER2_VETOED]
    assert vetoed, "the normalizing translator must trip the veto"
    assert all(r.tier2_is_spanish for r in vetoed), "veto happens after the LLM said yes"


def test_tier1_accepts_still_get_a_translation(sample_epub: Path) -> None:
    """Regression: tier-1 accepts used to render as empty popups.

    Tier 1 decides *whether* to annotate; something still has to produce the
    English text. 32 of 45 annotations in a real run had no translation at all
    before this was fixed.
    """
    sidecar, _ = detect(sample_epub, translator=StubTranslator())
    assert sidecar.annotations
    empty = [a for a in sidecar.annotations if not a.translation.strip()]
    assert not empty, f"{len(empty)} annotation(s) would render as blank popups"


def test_no_translator_still_produces_annotations(sample_epub: Path) -> None:
    """Tier-1-only mode is a diagnostic, so empty translations are acceptable there."""
    sidecar, _ = detect(sample_epub, translator=None)
    assert sidecar.annotations


# ------------------------------------------------------------------- resume


def test_trace_is_flushed_per_record(tmp_path: Path) -> None:
    """The trace is the resume log, so it must be on disk before the run ends.

    Buffered, an interruption at minute 70 of 73 loses every model answer.
    """
    from joven.trace import Decision, Tracer

    path = tmp_path / "trace.jsonl"
    with Tracer(path=path) as tracer:
        tracer.record(Decision(href="a.xhtml", para_index=1, segment_index=0,
                               text="Se fué.", start=0, end=7))
        assert path.read_text(encoding="utf-8").count("\n") == 1, "not yet on disk"


def test_reusable_answers_keeps_only_model_answers() -> None:
    from joven.trace import Decision, reusable_answers

    tier1_only = Decision(href="a.xhtml", para_index=1, segment_index=0,
                          text="He rode on.", start=0, end=11)
    answered = Decision(href="a.xhtml", para_index=2, segment_index=0,
                        text="Se fué.", start=0, end=7,
                        tier2_used=True, tier2_translation="He is gone.")

    index = reusable_answers([tier1_only, answered])
    assert list(index) == [("a.xhtml", 2, 0)]


def test_reusable_answers_drops_errors_so_they_are_retried() -> None:
    """A run that died probably died here; resuming must not inherit the failure."""
    from joven.trace import Decision, reusable_answers

    failed = Decision(href="a.xhtml", para_index=2, segment_index=0,
                      text="Se fué.", start=0, end=7,
                      tier2_used=True, tier2_error="ReadTimeout: ...")
    assert reusable_answers([failed]) == {}


def test_reusable_answers_prefers_the_latest_record() -> None:
    from joven.trace import Decision, reusable_answers

    def at(translation: str) -> Decision:
        return Decision(href="a.xhtml", para_index=2, segment_index=0,
                        text="Se fué.", start=0, end=7,
                        tier2_used=True, tier2_translation=translation)

    index = reusable_answers([at("old"), at("new")])
    assert index[("a.xhtml", 2, 0)].tier2_translation == "new"


def test_resume_reuses_answers_instead_of_calling_the_model(
    sample_epub: Path, tmp_path: Path
) -> None:
    """The whole point: a resumed run must not pay for work already done."""
    from joven.detect.pipeline import detect
    from joven.trace import Tracer, reusable_answers
    from joven.translate import StubTranslator, Verdict

    class CountingTranslator(StubTranslator):
        calls: int = 0

        def adjudicate(self, text: str, context: str = "") -> Verdict:
            type(self).calls += 1
            return super().adjudicate(text, context)

        def translate(self, text: str, context: str = "") -> Verdict:
            type(self).calls += 1
            return super().translate(text, context)

    first_trace = tmp_path / "first.jsonl"
    with Tracer(path=first_trace) as tracer:
        _, first = detect(sample_epub, translator=CountingTranslator(), tracer=tracer)
    assert CountingTranslator.calls > 0
    spent = CountingTranslator.calls

    CountingTranslator.calls = 0
    _, second = detect(
        sample_epub,
        translator=CountingTranslator(),
        resume=reusable_answers(load_trace(first_trace)),
    )

    assert CountingTranslator.calls == 0, "resumed run still called the model"
    assert second.llm_recalled == spent
    # and the result is the same book, not a degraded one
    assert [a.id for a in second.annotations] == [a.id for a in first.annotations]


def test_resume_ignores_answers_whose_text_no_longer_matches(
    sample_epub: Path, tmp_path: Path
) -> None:
    """A stale answer at the right address is worse than paying for a fresh one."""
    from joven.detect.pipeline import detect
    from joven.trace import Tracer, reusable_answers
    from joven.translate import StubTranslator

    trace_path = tmp_path / "t.jsonl"
    with Tracer(path=trace_path) as tracer:
        detect(sample_epub, translator=StubTranslator(), tracer=tracer)

    recorded = reusable_answers(load_trace(trace_path))
    for decision in recorded.values():
        decision.text = "something else entirely"

    _, result = detect(sample_epub, translator=StubTranslator(), resume=recorded)
    assert result.llm_recalled == 0
