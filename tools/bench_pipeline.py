#!/usr/bin/env python3
"""Measure the real two-tier pipeline: lingua triage -> LLM only for the band.

`bench_models.py` scores the LLM in isolation. This scores the system that
actually ships, which is the number that matters — Tier 1 both catches cases the
LLM misses and shields it from cases it gets wrong.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx  # noqa: E402
from bench_models import CASES, ask  # noqa: E402

from joven.detect.triage import Triager, Verdict  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default="qwen3:8b")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    triager = Triager()
    tiers = {Verdict.SPANISH: 0, Verdict.ENGLISH: 0, Verdict.UNCERTAIN: 0}
    correct = 0
    fp: list[str] = []
    fn: list[str] = []
    escalated = 0
    span_ok = span_total = 0

    with httpx.Client() as client:
        for case in CASES:
            t = triager.classify(case.text)
            tiers[t.verdict] += 1

            if t.verdict is Verdict.SPANISH:
                predicted, source, span = True, f"T1 {t.confidence:.2f}", None
            elif t.verdict is Verdict.ENGLISH:
                predicted, source, span = False, f"T1 {t.confidence:.2f}", None
            else:
                escalated += 1
                answer, _, err = ask(client, args.model, case.text)
                if answer is None:
                    print(f"    LLM ERROR {case.text[:40]!r}: {err}")
                    continue
                predicted = bool(answer.get("is_spanish"))
                span = (answer.get("spanish_text") or "").strip()
                source = "T2 llm"

            if predicted == case.is_spanish:
                correct += 1
            elif predicted:
                fp.append(f"{case.text[:56]}  [{source}]")
            else:
                fn.append(f"{case.text[:56]}  [{source}]")

            if case.is_spanish and predicted and span and (case.must_include or case.must_exclude):
                span_total += 1
                ok = True
                if case.must_include and case.must_include.lower() not in span.lower():
                    ok = False
                if case.must_exclude and case.must_exclude.lower() in span.lower():
                    ok = False
                span_ok += ok

            if args.verbose:
                mark = "ok  " if predicted == case.is_spanish else "MISS"
                print(f"  [{mark}] {source:<10} es={predicted!s:<5} {case.text[:60]}")

    n = len(CASES)
    print(f"\n=== two-tier pipeline ({args.model}) ===")
    print(f"  cases                {n}")
    print(f"  tier 1 accept (ES)   {tiers[Verdict.SPANISH]}")
    print(f"  tier 1 reject (EN)   {tiers[Verdict.ENGLISH]}")
    print(f"  escalated to LLM     {escalated}  ({100*escalated/n:.0f}%)")
    print(f"  classification       {correct}/{n}  ({100*correct/n:.0f}%)")
    if span_total:
        print(f"  span precision       {span_ok}/{span_total}  ({100*span_ok/span_total:.0f}%)")
    if fp:
        print("  FALSE POSITIVES (would annotate English):")
        for x in fp:
            print(f"    · {x}")
    if fn:
        print("  false negatives (missed Spanish):")
        for x in fn:
            print(f"    · {x}")
    if not fp and not fn:
        print("  no errors")


if __name__ == "__main__":
    main()
