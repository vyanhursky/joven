"""Decision trace — one record per segment, so detection is never a black box.

Without this, a book with missing footnotes gives you no way to tell whether
Tier 1 dismissed the passage, the LLM rejected it, the segmenter never produced
it, or it was suppressed as ``rejected`` in the sidecar. Every one of those has a
different fix.

The trace records **every** segment considered, not just the ones that became
annotations, and it records the LLM's raw response verbatim so a bad translation
can be traced to what the model actually said rather than to what we did with it.

Written as JSONL (one JSON object per line) so it stays greppable and streams
without holding the whole book in memory:

.. code-block:: console

    # why was this passage skipped?
    grep 'Escúchame' trace.jsonl | jq '{outcome, tier1_verdict, tier1_confidence}'

    # everything the LLM rejected
    jq -c 'select(.tier2_used and .tier2_is_spanish == false)' trace.jsonl

    # the abstention band, worst first
    jq -c 'select(.outcome == "escalated") | [.tier1_confidence, .text]' trace.jsonl | sort
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType


class Outcome:
    """Terminal state of one segment. Deliberately strings, for grep-ability."""

    ANNOTATED = "annotated"          # became part of a footnote
    TIER1_ENGLISH = "tier1_english"  # confidently English, never escalated
    TIER1_EMPTY = "tier1_empty"      # no letters (whitespace, punctuation)
    TIER2_REJECTED = "tier2_rejected"  # escalated, LLM said not Spanish
    TIER2_ERROR = "tier2_error"      # escalated, LLM failed
    TIER2_VETOED = "tier2_vetoed"    # LLM said Spanish, but "translation" was a rewording
    EMBEDDED_LOANWORD = "embedded_loanword"  # Spanish word inside an English clause (policy)
    SUPPRESSED = "suppressed"        # human marked it rejected in the sidecar


@dataclass(slots=True)
class Decision:
    """Everything that happened to one segment."""

    href: str
    para_index: int
    segment_index: int
    text: str
    start: int
    end: int

    # --- tier 1: statistical triage
    tier1_language: str = ""
    tier1_confidence: float = 0.0
    tier1_verdict: str = ""
    tier1_reason: str = ""
    tier1_stripped: str | None = None

    # --- tier 2: LLM adjudication (only when tier 1 abstained)
    tier2_used: bool = False
    tier2_model: str = ""
    tier2_is_spanish: bool | None = None
    tier2_spanish_text: str = ""
    tier2_translation: str = ""
    tier2_latency_s: float = 0.0
    tier2_raw: str = ""
    tier2_error: str = ""

    # --- result
    outcome: str = ""
    annotation_id: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class Tracer:
    """Streams :class:`Decision` records to a JSONL file and tallies a summary."""

    path: Path | None = None
    records: list[Decision] = field(default_factory=list)
    keep_in_memory: bool = True
    _handle: object = None

    def __enter__(self) -> Tracer:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("w", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            self._handle.close()  # type: ignore[attr-defined]
            self._handle = None

    def record(self, decision: Decision) -> None:
        if self._handle is not None:
            self._handle.write(decision.to_json() + "\n")  # type: ignore[attr-defined]
            # Flushed per record, which is what makes the trace a resume log rather
            # than a post-mortem. A full book is 73 minutes of LLM calls; buffered
            # writes mean a Ctrl-C or a closed lid at minute 70 loses the lot. The
            # cost is one write syscall against a ~1.7s model call.
            self._handle.flush()  # type: ignore[attr-defined]
        if self.keep_in_memory:
            self.records.append(decision)

    # ------------------------------------------------------------- reporting

    def summary(self) -> dict[str, int]:
        return dict(Counter(r.outcome for r in self.records))

    def escalation_rate(self) -> float:
        considered = [r for r in self.records if r.outcome != Outcome.TIER1_EMPTY]
        if not considered:
            return 0.0
        return sum(r.tier2_used for r in considered) / len(considered)

    def band_samples(self, limit: int = 15) -> list[Decision]:
        """Escalated segments, least confident first — where tuning pays off."""
        escalated = [r for r in self.records if r.tier2_used]
        return sorted(escalated, key=lambda r: r.tier1_confidence)[:limit]

    def llm_rejections(self, limit: int = 15) -> list[Decision]:
        return [r for r in self.records if r.tier2_is_spanish is False][:limit]

    def errors(self) -> list[Decision]:
        return [r for r in self.records if r.outcome == Outcome.TIER2_ERROR]

    def format_report(self) -> str:
        """Human-readable summary for the terminal."""
        counts = self.summary()
        total = sum(counts.values())
        lines = [
            "",
            f"  segments considered   {total:,}",
        ]
        for outcome in (
            Outcome.ANNOTATED,
            Outcome.TIER1_ENGLISH,
            Outcome.TIER2_REJECTED,
            Outcome.TIER2_VETOED,
            Outcome.EMBEDDED_LOANWORD,
            Outcome.TIER2_ERROR,
            Outcome.TIER1_EMPTY,
            Outcome.SUPPRESSED,
        ):
            if counts.get(outcome):
                lines.append(f"  {outcome:<21} {counts[outcome]:,}")

        escalated = sum(r.tier2_used for r in self.records)
        if escalated:
            latency = sum(r.tier2_latency_s for r in self.records)
            lines += [
                "",
                f"  escalated to LLM      {escalated:,}  "
                f"({100 * self.escalation_rate():.0f}% of non-empty)",
                f"  LLM time              {latency:.0f}s "
                f"({latency / escalated:.1f}s/call)",
            ]
        if errs := self.errors():
            lines.append(f"  LLM ERRORS            {len(errs)}  <- investigate")
        return "\n".join(lines)


def load_trace(path: str | Path) -> list[Decision]:
    """Read a JSONL trace back, for offline analysis."""
    decisions = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                decisions.append(Decision(**json.loads(line)))
    return decisions


# ------------------------------------------------------------------- resume

ResumeKey = tuple[str, int, int]


def reusable_answers(decisions: list[Decision]) -> dict[ResumeKey, Decision]:
    """Index a trace by segment address, keeping only the LLM answers worth reusing.

    Two exclusions carry the meaning:

    * segments the model never saw are left out — recomputing Tier 1 is instant,
      and it should pick up any gate or threshold change since the last run;
    * **recorded errors are left out**, so a resumed run retries them. A run that
      died mid-book usually died for a reason, and the failures nearest the end
      are the ones most likely to have been the symptom.

    Later records win, so a trace appended across several runs resolves to the
    most recent answer for each segment.
    """
    return {
        (d.href, d.para_index, d.segment_index): d
        for d in decisions
        if d.tier2_used and not d.tier2_error
    }
