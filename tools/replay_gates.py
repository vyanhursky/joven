"""Replay the suppression gates over a recorded trace — no LLM calls.

Every escalated segment's trace record keeps the model's verbatim answer, so the
deterministic gates that run *after* the model can be re-evaluated offline. That
turns "did my threshold change help?" from a 72-minute book run into a one-second
question, and it answers it against the real corpus rather than invented cases.

    python tools/replay_gates.py trace.jsonl

Reports every segment whose outcome the current code would change, so a gate edit
can be checked for both the false suppressions it fixes and the true ones it
breaks.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from joven.detect.triage import is_embedded_loanword
from joven.trace import Outcome
from joven.translate import is_normalization


def replay(record: dict) -> str:
    """The outcome today's gates would produce for an escalated segment.

    Mirrors the post-model half of :func:`joven.detect.pipeline.detect`. Segments the
    model never saw are returned unchanged — nothing here can affect them.
    """
    if not record.get("tier2_used"):
        return record["outcome"]

    translation = record.get("tier2_translation") or ""
    spanish = record.get("tier2_spanish_text") or ""

    # The two escalation paths disagree about an empty translation: a Tier-1
    # accept was only ever asking *how* to translate, so nothing back is an
    # error, while an adjudication may legitimately answer "not Spanish".
    tier1_accepted = record.get("tier1_verdict") == "spanish"
    if record.get("tier2_error") or (tier1_accepted and not translation):
        return Outcome.TIER2_ERROR
    if not record.get("tier2_is_spanish") or not translation:
        return Outcome.TIER2_REJECTED
    if is_normalization(spanish or record["text"], translation):
        return Outcome.TIER2_VETOED
    if is_embedded_loanword(record["text"], spanish):
        return Outcome.EMBEDDED_LOANWORD
    return Outcome.ANNOTATED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--quiet", action="store_true", help="counts only")
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.trace.read_text("utf-8").splitlines() if line.strip()]
    escalated = [r for r in rows if r.get("tier2_used")]

    flips: list[tuple[str, str, dict]] = []
    for record in escalated:
        now = replay(record)
        if now != record["outcome"]:
            flips.append((record["outcome"], now, record))

    before = Counter(r["outcome"] for r in escalated)
    after = Counter(replay(r) for r in escalated)

    print(f"{len(escalated):,} escalated segments replayed from {args.trace}\n")
    print(f"  {'outcome':<20} {'before':>8} {'after':>8}   delta")
    for key in sorted(set(before) | set(after)):
        delta = after[key] - before[key]
        print(f"  {key:<20} {before[key]:>8} {after[key]:>8}   {delta:+d}" if delta else
              f"  {key:<20} {before[key]:>8} {after[key]:>8}")

    print(f"\n  {len(flips)} segments change outcome")
    if not args.quiet:
        for was, now, record in flips:
            print(f"\n  {was} -> {now}")
            print(f"    seg {record['text'][:88]!r}")
            print(f"     es {(record.get('tier2_spanish_text') or '')[:88]!r}")
            print(f"     en {(record.get('tier2_translation') or '')[:88]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
