"""Cost a book run before paying for it.

The expensive question — *how many segments will actually reach the LLM?* — is
answerable for free. Tier 1 is offline statistical triage, so running detection
with no translator attached produces the exact escalation count and the exact
text of every call that would have been made. Multiply that by measured token
counts and the estimate is arithmetic rather than guesswork.

Token counts come from the API's own ``count_tokens`` endpoint where credentials
exist, calibrated on a sample rather than a full sweep: the prefix is counted
exactly (it is identical on every call), and a sample of assembled user turns
fixes the characters-per-token ratio for the variable part. Without credentials
it falls back to a ratio measured on this book, and says so — an estimate whose
provenance is invisible is worse than one that admits it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from . import pricing
from .detect.pipeline import detect
from .trace import Tracer
from .translate import (
    CLAUDE_EXTRA_FEWSHOT,
    FEWSHOT,
    SYSTEM_PROMPT,
    TRANSLATE_FEWSHOT,
    TRANSLATE_ONLY_PROMPT,
    _user_turn,
)

FALLBACK_CHARS_PER_TOKEN = 3.6
"""Measured on *The Crossing* — mixed English/Spanish with accents.

Used only when no credentials are available to calibrate against. Accented
Spanish tokenizes slightly worse than plain English, so this is deliberately on
the pessimistic side of 4.0.
"""

SAMPLE_SIZE = 25
"""User turns to count exactly when calibrating. Enough to fix the ratio."""

REPLY_CHARS = 73
"""Mean JSON reply length, measured over 2,544 real replies."""


@dataclass(slots=True)
class Measurement:
    """What a run would send, measured rather than assumed."""

    calls: int
    adjudicate_calls: int
    translate_calls: int
    prefix_tokens: int
    variable_input_tokens: int
    output_tokens: int
    chars_per_token: float
    calibrated: bool
    segments_scanned: int
    paragraphs_scanned: int


def _prefix_text(system: str, fewshot: list[tuple[str, dict]]) -> str:
    """Everything sent identically on every call, as one string to be counted."""
    import json

    parts = [system]
    for shot_text, shot_answer in [*fewshot, *CLAUDE_EXTRA_FEWSHOT]:
        parts.append(shot_text)
        parts.append(json.dumps(shot_answer, ensure_ascii=False))
    return "\n".join(parts)


def _count_tokens(client, model: str, text: str) -> int:
    return client.messages.count_tokens(
        model=model, messages=[{"role": "user", "content": text}]
    ).input_tokens


def measure(
    source: str | Path,
    *,
    model: str = pricing.DEFAULT_MODEL,
    limit: int | None = None,
    client=None,
) -> Measurement:
    """Measure a prospective run without making a single translation call.

    Passing ``translator=None`` to :func:`~etx.detect.pipeline.detect` runs Tier 1
    only — free and offline — and records every segment that *would* have
    escalated as ``tier2_rejected`` with a "no translator" reason. Those records
    are the call list, and their text is the variable half of each prompt.
    """
    tracer = Tracer(keep_in_memory=True)
    _, result = detect(source, translator=None, tracer=tracer, limit=limit)

    would_escalate = [
        d
        for d in tracer.records
        if d.outcome == "tier2_rejected" and "no translator" in (d.tier1_reason or "")
    ]
    accepted = [d for d in tracer.records if d.outcome == "annotated"]

    # Tier-1 accepts take the translate-only prompt; the band takes adjudication.
    adjudicate_calls = len(would_escalate)
    translate_calls = len(accepted)
    calls = adjudicate_calls + translate_calls

    adj_prefix = _prefix_text(SYSTEM_PROMPT, FEWSHOT)
    tr_prefix = _prefix_text(TRANSLATE_ONLY_PROMPT, TRANSLATE_FEWSHOT)

    # The variable half: the fenced paragraph plus its context wrapper. Context
    # is capped per paragraph, so reconstruct a representative turn per call.
    turns = [_user_turn(d.text, "x" * 400) for d in (*would_escalate, *accepted)]
    variable_chars = sum(len(t) for t in turns)

    calibrated = False
    chars_per_token = FALLBACK_CHARS_PER_TOKEN
    if client is not None and turns:
        sample = random.Random(0).sample(turns, min(SAMPLE_SIZE, len(turns)))
        sample_tokens = sum(_count_tokens(client, model, t) for t in sample)
        sample_chars = sum(len(t) for t in sample)
        if sample_tokens:
            chars_per_token = sample_chars / sample_tokens
            calibrated = True
        adj_tokens = _count_tokens(client, model, adj_prefix)
        tr_tokens = _count_tokens(client, model, tr_prefix)
    else:
        adj_tokens = round(len(adj_prefix) / chars_per_token)
        tr_tokens = round(len(tr_prefix) / chars_per_token)

    # Report the prefix the majority of calls will carry, and bill both properly.
    prefix_tokens = adj_tokens if adjudicate_calls >= translate_calls else tr_tokens
    prefix_total = adjudicate_calls * adj_tokens + translate_calls * tr_tokens

    return Measurement(
        calls=calls,
        adjudicate_calls=adjudicate_calls,
        translate_calls=translate_calls,
        prefix_tokens=prefix_tokens,
        # Fold the *other* prompt's prefix cost into the variable bucket so the
        # total stays exact even though only one prefix is shown per line.
        variable_input_tokens=round(variable_chars / chars_per_token)
        + prefix_total
        - calls * prefix_tokens,
        # 73 chars per JSON reply, averaged over 2,544 real replies in trace.jsonl.
        # Most replies are rejections — three short fields — which is why it is
        # this low. An earlier draft used 290 and overstated output cost 4x.
        output_tokens=round(calls * REPLY_CHARS / chars_per_token),
        chars_per_token=chars_per_token,
        calibrated=calibrated,
        segments_scanned=result.segments_scanned,
        paragraphs_scanned=result.paragraphs_scanned,
    )


def report(m: Measurement, *, model: str = pricing.DEFAULT_MODEL) -> str:
    """A pre-flight estimate for a run, batched and not."""
    spec = pricing.MODELS[model]
    direct = pricing.estimate(
        model=model,
        calls=m.calls,
        prefix_tokens=m.prefix_tokens,
        variable_input_tokens=m.variable_input_tokens,
        output_tokens=m.output_tokens,
    )
    batched = pricing.estimate(
        model=model,
        calls=m.calls,
        prefix_tokens=m.prefix_tokens,
        variable_input_tokens=m.variable_input_tokens,
        output_tokens=m.output_tokens,
        batched=True,
    )
    lines = [
        f"scanned              {m.paragraphs_scanned:,} paragraphs, "
        f"{m.segments_scanned:,} segments  (Tier 1 only — free)",
        f"would escalate       {m.calls:,} calls "
        f"({m.adjudicate_calls:,} adjudicate + {m.translate_calls:,} translate)",
        "",
        direct.format(),
        "",
        f"with --batch         ${batched.total:.2f}  (-50%, completes within the hour)",
    ]
    if not direct.cached:
        lines += [
            "",
            f"NOTE  the {m.prefix_tokens:,}-token prefix is below {model}'s "
            f"{spec.cache_minimum}-token cache minimum,",
            "      so caching is silently inactive and every call pays full price.",
        ]
    if not m.calibrated:
        lines += [
            "",
            f"NOTE  token counts estimated at {m.chars_per_token:.1f} chars/token "
            "(no API credentials to",
            "      calibrate against). Treat as +/-20%; set ANTHROPIC_API_KEY for exact counts.",
        ]
    return "\n".join(lines)
