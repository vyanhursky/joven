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

**The paragraph is the unit of work.** :func:`_scan_paragraph` does everything
above for one paragraph and returns its decisions without touching the tracer.
The caller then *finishes* paragraphs strictly in book order — recording the
trace, collecting the annotation, reporting progress — whether they were scanned
one at a time or on a pool of workers. That is what keeps ``trace.jsonl`` and
``--resume`` identical at any worker count: the model may be asked about
paragraph 12 before paragraph 11 has come back, but nothing is written down until
11 has.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from itertools import islice
from pathlib import Path

from ..epub.archive import EpubArchive
from ..epub.document import iter_text_units
from ..epub.package import read_package
from ..model import Annotation, Sidecar, file_sha256, occurrence_indices
from ..trace import Decision, Outcome, ResumeKey, Tracer
from ..translate import SIMILARITY_VETO, Translator, is_normalization
from ..translate import Verdict as LLMVerdict
from .segment import Segment, merge_adjacent, segment
from .triage import Triager, Verdict, is_embedded_loanword

CONTEXT_CHARS = 400


@dataclass(slots=True)
class DetectResult:
    annotations: list[Annotation] = field(default_factory=list)
    paragraphs_scanned: int = 0
    paragraphs_total: int = 0
    """How many paragraphs the run set out to scan, after --limit / --href / --range."""
    segments_scanned: int = 0
    merge_stats: dict[str, int] = field(default_factory=dict)
    llm_recalled: int = 0
    """Escalations answered from a prior trace instead of the model."""


@dataclass(frozen=True, slots=True)
class Progress:
    """A snapshot after each finished paragraph, for a progress bar or a status page."""

    paragraphs_done: int
    paragraphs_total: int
    segments: int
    escalated: int
    annotated: int
    errors: int
    recalled: int
    last_latency_s: float
    """The most recent real model call, 0.0 when the paragraph needed none."""


@dataclass(frozen=True, slots=True)
class _Job:
    """One paragraph to scan, with enough of its document to build context from."""

    href: str
    units: list
    position: int
    occurrence: int

    @property
    def unit(self):  # noqa: ANN201 - TextUnit, kept untyped like every other caller
        return self.units[self.position]


@dataclass(slots=True)
class _Scanned:
    """What one paragraph produced, not yet recorded."""

    decisions: list[Decision]
    annotation: Annotation | None
    escalated: int = 0
    errors: int = 0
    recalled: int = 0
    last_latency_s: float = 0.0


def _context_for(units: list, position: int, limit: int = CONTEXT_CHARS) -> str:
    """A little preceding English prose, so bare fragments are adjudicable.

    "Bastante." means nothing alone; with the paragraph before it, an LLM can tell
    whether it is Spanish dialogue or an English word it does not recognise.
    """
    parts: list[str] = []
    for unit in reversed(units[max(0, position - 3):position]):
        parts.append(unit.text.strip())
        if sum(len(p) for p in parts) > limit:
            break
    return " ".join(reversed(parts))[-limit:]


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


def _note_verdict(decision: Decision, verdict: LLMVerdict) -> None:
    decision.tier2_used = True
    decision.tier2_model = verdict.model
    decision.tier2_latency_s = verdict.latency_s
    decision.tier2_raw = verdict.raw
    decision.tier2_error = verdict.error
    decision.tier2_is_spanish = verdict.is_spanish
    decision.tier2_spanish_text = verdict.spanish_text
    decision.tier2_translation = verdict.translation


def _scan_paragraph(
    job: _Job,
    *,
    triager: Triager,
    translator: Translator | None,
    recorded: dict[ResumeKey, Decision],
    context_chars: int,
    similarity_veto: float,
) -> _Scanned:
    """Both tiers and every gate, for one paragraph. Records nothing itself."""
    unit = job.unit
    out = _Scanned(decisions=[], annotation=None)
    spanish: list[Segment] = []
    translations: list[str] = []
    confidences: list[float] = []
    models: set[str] = set()
    context: str | None = None

    def ask(decision: Decision, call: Callable[[str, str], LLMVerdict]) -> LLMVerdict:
        nonlocal context
        verdict = _recall(recorded, decision)
        if verdict is None:
            if context is None:
                context = _context_for(job.units, job.position, context_chars)
            verdict = call(decision.text, context)
            out.escalated += 1
            out.last_latency_s = verdict.latency_s
        else:
            out.recalled += 1
        _note_verdict(decision, verdict)
        return verdict

    for seg in segment(unit.text):
        decision = Decision(
            href=job.href,
            para_index=unit.index,
            segment_index=seg.index,
            text=seg.text,
            start=seg.start,
            end=seg.end,
        )
        out.decisions.append(decision)

        if not seg.has_letters:
            decision.outcome = Outcome.TIER1_EMPTY
            continue

        triaged = triager.classify(seg.text)
        decision.tier1_language = triaged.language
        decision.tier1_confidence = triaged.confidence
        decision.tier1_verdict = str(triaged.verdict)
        decision.tier1_reason = triaged.reason
        decision.tier1_stripped = triaged.stripped

        if triaged.verdict is Verdict.ENGLISH:
            decision.outcome = Outcome.TIER1_ENGLISH
            continue

        if triaged.verdict is Verdict.SPANISH:
            # Tier 1 decided *whether*; we still need the translation, or this
            # renders as an empty popup on the device.
            if translator is None:
                spanish.append(seg)
                confidences.append(triaged.confidence)
                decision.outcome = Outcome.ANNOTATED
                continue
            verdict = ask(decision, translator.translate)
            if not verdict.ok or not verdict.translation:
                decision.outcome = Outcome.TIER2_ERROR
                decision.tier2_error = verdict.error or "no translation returned"
                out.errors += 1
            elif is_normalization(seg.text, verdict.translation, threshold=similarity_veto):
                decision.outcome = Outcome.TIER2_VETOED
            elif is_embedded_loanword(seg.text, verdict.spanish_text):
                decision.outcome = Outcome.EMBEDDED_LOANWORD
            else:
                spanish.append(_narrow(seg, verdict.spanish_text))
                translations.append(verdict.translation)
                confidences.append(triaged.confidence)
                models.add(verdict.model)
                decision.outcome = Outcome.ANNOTATED
            continue

        # --- abstention band: escalate
        if translator is None:
            decision.outcome = Outcome.TIER2_REJECTED
            decision.tier1_reason += " (no translator; not escalated)"
            continue

        verdict = ask(decision, translator.adjudicate)
        if not verdict.ok:
            decision.outcome = Outcome.TIER2_ERROR
            out.errors += 1
        elif (
            verdict.is_spanish
            and verdict.translation
            and is_normalization(
                verdict.spanish_text or seg.text, verdict.translation, threshold=similarity_veto
            )
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

    if spanish:
        out.annotation = Annotation.create(
            href=job.href,
            para_index=unit.index,
            source_text=unit.text,
            spans=merge_adjacent(spanish, unit.text),
            translation=" ".join(translations) if translations else "",
            occurrence=job.occurrence,
            detector_confidence=min(confidences) if confidences else 0.0,
            model=", ".join(sorted(models)),
        )
    return out


def _plan(
    archive: EpubArchive, spine_hrefs: list[str], hrefs: Collection[str] | None
) -> list[_Job]:
    """Every paragraph the run will look at, in book order."""
    work: list[_Job] = []
    for href in spine_hrefs:
        if href not in archive or (hrefs is not None and href not in hrefs):
            continue
        units = iter_text_units(archive.get(href), href)
        occurrences = occurrence_indices(u.text for u in units)
        work.extend(
            _Job(href=href, units=units, position=position, occurrence=occurrence)
            for position, occurrence in enumerate(occurrences)
        )
    return work


def _run_pooled(
    work: list[_Job],
    scan: Callable[[_Job], _Scanned],
    finish: Callable[[_Scanned], None],
    workers: int,
    should_stop: Callable[[], bool],
) -> None:
    """Scan on a pool, finish in order.

    A sliding window of ``2 × workers`` paragraphs is in flight; the head of the
    window is finished as soon as it is done and the next paragraph takes its
    place. Results therefore land in book order however the model schedules them,
    and a Ctrl-C cancels what has not started rather than draining the queue.
    """
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="joven")
    pending: deque[Future[_Scanned]] = deque()
    jobs = iter(work)
    try:
        for job in islice(jobs, workers * 2):
            pending.append(pool.submit(scan, job))
        while pending:
            finish(pending.popleft().result())
            # A stop request drains what is in flight -- those calls are already
            # being paid for -- and submits nothing more.
            if not should_stop() and (following := next(jobs, None)) is not None:
                pending.append(pool.submit(scan, following))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


def detect(
    source: str | Path,
    *,
    triager: Triager | None = None,
    translator: Translator | None = None,
    tracer: Tracer | None = None,
    limit: int | None = None,
    resume: dict[ResumeKey, Decision] | None = None,
    hrefs: Collection[str] | None = None,
    paragraph_range: tuple[int | None, int | None] | None = None,
    workers: int = 1,
    context_chars: int = CONTEXT_CHARS,
    similarity_veto: float = SIMILARITY_VETO,
    on_progress: Callable[[Progress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[Sidecar, DetectResult]:
    """Scan a book and produce candidate annotations plus a full decision trace.

    ``translator=None`` runs Tier 1 only — instant, free, and enough to answer
    "would this paragraph have been escalated?".

    ``resume`` supplies previously recorded model answers (see
    :func:`joven.trace.reusable_answers`). Tier 1 and every gate still run over
    the whole book — only the model calls are skipped — so a resumed run picks up
    threshold and gate changes rather than replaying stale conclusions.

    ``hrefs`` restricts the scan to those spine documents; ``paragraph_range`` is
    a ``(start, stop)`` slice over the paragraphs that remain, counted the way
    ``limit`` counts them, so ``limit=N`` and ``paragraph_range=(0, N)`` agree.

    ``workers`` above 1 scans that many paragraphs concurrently. The trace, the
    sidecar and ``on_progress`` see paragraphs in book order regardless.

    ``should_stop`` is polled between paragraphs; when it returns True the scan
    ends early and returns what it has. Everything already answered is in the
    trace, so a stopped run resumes like an interrupted one.
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

    work = _plan(archive, package.spine_hrefs, hrefs)
    if paragraph_range is not None:
        start, stop = paragraph_range
        work = work[slice(start, stop)]
    if limit is not None:
        work = work[:limit]
    result.paragraphs_total = len(work)

    scan = partial(
        _scan_paragraph,
        triager=triager,
        translator=translator,
        recorded=recorded,
        context_chars=context_chars,
        similarity_veto=similarity_veto,
    )

    escalated = errors = 0

    def finish(scanned: _Scanned) -> None:
        nonlocal escalated, errors
        for decision in scanned.decisions:
            tracer.record(decision)
        if scanned.annotation is not None:
            result.annotations.append(scanned.annotation)
        result.paragraphs_scanned += 1
        result.segments_scanned += len(scanned.decisions)
        result.llm_recalled += scanned.recalled
        escalated += scanned.escalated
        errors += scanned.errors
        if on_progress is not None:
            on_progress(
                Progress(
                    paragraphs_done=result.paragraphs_scanned,
                    paragraphs_total=result.paragraphs_total,
                    segments=result.segments_scanned,
                    escalated=escalated,
                    annotated=len(result.annotations),
                    errors=errors,
                    recalled=result.llm_recalled,
                    last_latency_s=scanned.last_latency_s,
                )
            )

    halt = should_stop or (lambda: False)
    if workers <= 1:
        for job in work:
            if halt():
                break
            finish(scan(job))
    else:
        # Build the detectors on this thread first. Their caches are not locked,
        # and the first paragraphs would otherwise each build their own.
        triager.classify("Vaya con Dios.")
        _run_pooled(work, scan, finish, workers, halt)

    result.merge_stats = sidecar.merge(result.annotations)
    return sidecar, result
