"""The two-tier detection pipeline, fully traced.

Per paragraph:

1. segment into sentences (offsets preserved)
2. **Tier 1** — statistical triage per segment: confident Spanish, confident
   English, or abstain
3. **Tier 2** — the abstention band only, adjudicated by an LLM with surrounding
   English context
4. merge contiguous Spanish runs into one annotation per paragraph (§4.5)

Every segment produces a :class:`~joven.trace.Decision`, whether or not it becomes
an annotation, so any missing footnote can be explained after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..epub.archive import EpubArchive
from ..epub.document import iter_text_units
from ..epub.package import read_package
from ..model import Annotation, Sidecar, file_sha256, occurrence_indices
from ..trace import Decision, Outcome, ResumeKey, Tracer
from ..translate import Translator, is_normalization
from ..translate import Verdict as LLMVerdict
from .segment import Segment, merge_adjacent, segment
from .triage import Triager, Verdict, is_embedded_loanword

CONTEXT_CHARS = 400


@dataclass(slots=True)
class DetectResult:
    annotations: list[Annotation] = field(default_factory=list)
    paragraphs_scanned: int = 0
    segments_scanned: int = 0
    merge_stats: dict[str, int] = field(default_factory=dict)
    llm_recalled: int = 0
    """Escalations answered from a prior trace instead of the model."""


def _context_for(units: list, position: int) -> str:
    """A little preceding English prose, so bare fragments are adjudicable.

    "Bastante." means nothing alone; with the paragraph before it, an LLM can tell
    whether it is Spanish dialogue or an English word it does not recognise.
    """
    parts: list[str] = []
    for unit in reversed(units[max(0, position - 3):position]):
        parts.append(unit.text.strip())
        if sum(len(p) for p in parts) > CONTEXT_CHARS:
            break
    return " ".join(reversed(parts))[-CONTEXT_CHARS:]


def _narrow(seg: Segment, spanish_text: str) -> Segment:
    """Shrink a segment to just the Spanish the model identified.

    Tier 1 accepts whole *sentences*, so ``Gracias, he said.`` was annotated
    entire and the marker landed after the English dialogue tag — inconsistent
    with the escalated path, where ``Cuántos años tienes?* the old man said.``
    puts the marker before it. The translate call already reports the Spanish
    span, so use it when it genuinely occurs in the segment.

    Falls back to the full segment on any mismatch: a marker in the right place
    matters less than a marker in a *valid* place.
    """
    candidate = (spanish_text or "").strip()
    if not candidate or candidate == seg.text:
        return seg
    offset = seg.text.find(candidate)
    if offset < 0:
        return seg
    start = seg.start + offset
    return Segment(text=candidate, start=start, end=start + len(candidate), index=seg.index)


def _recall(
    recorded: dict[ResumeKey, Decision], decision: Decision
) -> LLMVerdict | None:
    """The model's prior answer for this exact segment, if a trace holds one.

    Matched on address, text *and* Tier-1 verdict. The first two are obvious: the
    address alone would hand a stale answer to a re-edited book, and silently
    translating one paragraph as another is worse than spending the 1.7 seconds
    again.

    The verdict is the subtle one, and the Latin veto is exactly what makes it
    matter. Tier 1's two outcomes call *different* prompts -- an accept gets
    ``translate`` ("this is Spanish, render it"), the band gets ``adjudicate``
    ("is this Spanish at all?") -- and a translate-only answer always says yes. So
    resuming across a Tier-1 change without checking the verdict would hand a
    "yes, Spanish" answer to a segment the new code wants adjudicated, quietly
    reinstating the very footnote the change was made to prevent.

    Reported with zero latency, because this run did not spend it.
    """
    prior = recorded.get((decision.href, decision.para_index, decision.segment_index))
    if prior is None or prior.text != decision.text:
        return None
    if prior.tier1_verdict != decision.tier1_verdict:
        return None
    return LLMVerdict(
        is_spanish=bool(prior.tier2_is_spanish),
        spanish_text=prior.tier2_spanish_text,
        translation=prior.tier2_translation,
        model=prior.tier2_model,
        latency_s=0.0,
        raw=prior.tier2_raw,
    )


def detect(
    source: str | Path,
    *,
    triager: Triager | None = None,
    translator: Translator | None = None,
    tracer: Tracer | None = None,
    limit: int | None = None,
    resume: dict[ResumeKey, Decision] | None = None,
) -> tuple[Sidecar, DetectResult]:
    """Scan a book and produce candidate annotations plus a full decision trace.

    ``translator=None`` runs Tier 1 only — instant, free, and enough to answer
    "would this paragraph have been escalated?".

    ``resume`` supplies previously recorded model answers (see
    :func:`joven.trace.reusable_answers`). Tier 1 and every gate still run over
    the whole book — only the model calls are skipped — so a resumed run picks up
    threshold and gate changes rather than replaying stale conclusions.
    """
    source = Path(source)
    triager = triager or Triager()
    tracer = tracer or Tracer(keep_in_memory=True)
    recorded = resume or {}

    archive = EpubArchive.read(source)
    package = read_package(archive)
    sidecar = Sidecar(
        source_sha256=file_sha256(source),
        title=package.metadata.get("title", source.stem),
    )
    result = DetectResult()

    for href in package.spine_hrefs:
        if href not in archive:
            continue
        units = iter_text_units(archive.get(href), href)
        occurrences = occurrence_indices(u.text for u in units)

        for position, (unit, occurrence) in enumerate(zip(units, occurrences, strict=True)):
            if limit is not None and result.paragraphs_scanned >= limit:
                break
            result.paragraphs_scanned += 1

            segments = segment(unit.text)
            spanish: list[Segment] = []
            translations: list[str] = []
            confidences: list[float] = []
            models: set[str] = set()
            context: str | None = None

            for seg in segments:
                result.segments_scanned += 1
                decision = Decision(
                    href=href,
                    para_index=unit.index,
                    segment_index=seg.index,
                    text=seg.text,
                    start=seg.start,
                    end=seg.end,
                )

                if not seg.has_letters:
                    decision.outcome = Outcome.TIER1_EMPTY
                    tracer.record(decision)
                    continue

                triaged = triager.classify(seg.text)
                decision.tier1_language = triaged.language
                decision.tier1_confidence = triaged.confidence
                decision.tier1_verdict = str(triaged.verdict)
                decision.tier1_reason = triaged.reason
                decision.tier1_stripped = triaged.stripped

                if triaged.verdict is Verdict.ENGLISH:
                    decision.outcome = Outcome.TIER1_ENGLISH
                    tracer.record(decision)
                    continue

                if triaged.verdict is Verdict.SPANISH:
                    # Tier 1 decided *whether*; we still need the translation, or
                    # this renders as an empty popup on the device.
                    if translator is None:
                        spanish.append(seg)
                        confidences.append(triaged.confidence)
                        decision.outcome = Outcome.ANNOTATED
                        tracer.record(decision)
                        continue
                    verdict = _recall(recorded, decision)
                    if verdict is None:
                        if context is None:
                            context = _context_for(units, position)
                        verdict = translator.translate(seg.text, context)
                    else:
                        result.llm_recalled += 1
                    decision.tier2_used = True
                    decision.tier2_model = verdict.model
                    decision.tier2_latency_s = verdict.latency_s
                    decision.tier2_raw = verdict.raw
                    decision.tier2_error = verdict.error
                    decision.tier2_is_spanish = verdict.is_spanish
                    decision.tier2_spanish_text = verdict.spanish_text
                    decision.tier2_translation = verdict.translation

                    if not verdict.ok or not verdict.translation:
                        decision.outcome = Outcome.TIER2_ERROR
                        decision.tier2_error = verdict.error or "no translation returned"
                    elif is_normalization(seg.text, verdict.translation):
                        decision.outcome = Outcome.TIER2_VETOED
                    elif is_embedded_loanword(seg.text, verdict.spanish_text):
                        decision.outcome = Outcome.EMBEDDED_LOANWORD
                    else:
                        spanish.append(_narrow(seg, verdict.spanish_text))
                        translations.append(verdict.translation)
                        confidences.append(triaged.confidence)
                        models.add(verdict.model)
                        decision.outcome = Outcome.ANNOTATED
                    tracer.record(decision)
                    continue

                # --- abstention band: escalate
                if translator is None:
                    decision.outcome = Outcome.TIER2_REJECTED
                    decision.tier1_reason += " (no translator; not escalated)"
                    tracer.record(decision)
                    continue

                verdict = _recall(recorded, decision)
                if verdict is None:
                    if context is None:
                        context = _context_for(units, position)
                    verdict = translator.adjudicate(seg.text, context)
                else:
                    result.llm_recalled += 1
                decision.tier2_used = True
                decision.tier2_model = verdict.model
                decision.tier2_latency_s = verdict.latency_s
                decision.tier2_raw = verdict.raw
                decision.tier2_error = verdict.error
                decision.tier2_is_spanish = verdict.is_spanish
                decision.tier2_spanish_text = verdict.spanish_text
                decision.tier2_translation = verdict.translation

                if not verdict.ok:
                    decision.outcome = Outcome.TIER2_ERROR
                elif (
                    verdict.is_spanish
                    and verdict.translation
                    and is_normalization(verdict.spanish_text or seg.text, verdict.translation)
                ):
                    # dialect English rewritten as standard English, not translated
                    decision.outcome = Outcome.TIER2_VETOED
                elif (
                    verdict.is_spanish
                    and verdict.translation
                    and is_embedded_loanword(seg.text, verdict.spanish_text)
                ):
                    # policy: a lone Spanish word inside English prose gets no note
                    decision.outcome = Outcome.EMBEDDED_LOANWORD
                elif verdict.is_spanish and verdict.translation:
                    spanish.append(seg)
                    translations.append(verdict.translation)
                    confidences.append(triaged.confidence)
                    models.add(verdict.model)
                    decision.outcome = Outcome.ANNOTATED
                else:
                    decision.outcome = Outcome.TIER2_REJECTED
                tracer.record(decision)

            if not spanish:
                continue

            spans = merge_adjacent(spanish, unit.text)
            annotation = Annotation.create(
                href=href,
                para_index=unit.index,
                source_text=unit.text,
                spans=spans,
                translation=" ".join(translations) if translations else "",
                occurrence=occurrence,
                detector_confidence=min(confidences) if confidences else 0.0,
                model=", ".join(sorted(models)),
            )
            result.annotations.append(annotation)

        if limit is not None and result.paragraphs_scanned >= limit:
            break

    result.merge_stats = sidecar.merge(result.annotations)
    return sidecar, result
