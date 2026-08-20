#!/usr/bin/env python3
"""Benchmark local Ollama models on the adversarial cases from DESIGN.md §2.2.

This decides which local model we ship. The cases are the ones that actually
break things: short Spanish fragments welded to English dialogue tags, short
English lines that statistical language ID mislabels as Spanish, and English
prose carrying Spanish loanwords that must be left alone.

Usage:
    python tools/bench_models.py                       # every installed model
    python tools/bench_models.py qwen3:8b gemma3:12b   # specific models
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

import httpx

OLLAMA = "http://localhost:11434"

SCHEMA = {
    "type": "object",
    "properties": {
        "is_spanish": {"type": "boolean"},
        "spanish_text": {"type": "string"},
        "translation": {"type": "string"},
    },
    "required": ["is_spanish", "spanish_text", "translation"],
}

SYSTEM = """\
You analyse single paragraphs from an English-language novel set on the \
US-Mexico border in the 1930s. Some paragraphs contain untranslated Spanish \
dialogue that an English-only reader cannot understand.

For the paragraph you are given, return JSON with three fields:

is_spanish
  true only if the paragraph contains Spanish that needs translating.
  false for English prose - INCLUDING English prose that contains isolated
  Spanish loanwords or proper nouns (matriz, copo, candela, vaquero, guero,
  Casas Grandes). A single Spanish noun inside an English sentence is NOT
  Spanish dialogue.
  false for short plain English lines.

spanish_text
  The Spanish substring, copied verbatim from the paragraph. Exclude English
  narration and English dialogue tags such as "he said", "she said",
  "said the man", "the old man said", "he cried". Empty string when
  is_spanish is false.

translation
  Natural English translation of spanish_text, preserving any English dialogue
  tag that sits inside it. Empty string when is_spanish is false.

Answer with JSON only."""

FEWSHOT: list[tuple[str, dict]] = [
    (
        "Vaya con Dios.",
        {"is_spanish": True, "spanish_text": "Vaya con Dios.", "translation": "Go with God."},
    ),
    (
        "Cuántos años tienes? the old man said.",
        {
            "is_spanish": True,
            "spanish_text": "Cuántos años tienes?",
            "translation": "How old are you?",
        },
    ),
    (
        "The matríz will not help you, the old man said. He said that the boy should find "
        "that place where acts of God and those of man are of a piece.",
        {"is_spanish": False, "spanish_text": "", "translation": ""},
    ),
    (
        "Go on.",
        {"is_spanish": False, "spanish_text": "", "translation": ""},
    ),
]


@dataclass(frozen=True)
class Case:
    text: str
    is_spanish: bool
    # substring that must appear in spanish_text (span precision probe)
    must_include: str | None = None
    # substring that must NOT appear in spanish_text (English tag leakage probe)
    must_exclude: str | None = None
    note: str = ""


CASES: list[Case] = [
    # --- whole-paragraph Spanish (pattern A) -------------------------------
    Case("Se fué.", True),
    Case("Ay. Ándale, joven. Ándale pues.", True),
    Case("Dieciseis.", True),
    Case("Y cómo se encuentra?", True),
    Case("Está bien.", True),
    Case("Tan horrible.", True, note="2 words, conf 0.50"),
    # --- Spanish + English dialogue tag (pattern B) ------------------------
    Case(
        "Cuántos años tienes? the old man said.",
        True,
        must_include="Cuántos años tienes",
        must_exclude="the old man said",
    ),
    Case("Tantos, said the man.", True, must_include="Tantos", must_exclude="said the man"),
    Case("Adelante, he cried.", True, must_include="Adelante", must_exclude="he cried"),
    Case("Güero, he said.", True, must_include="Güero", must_exclude="he said"),
    Case(
        "Bastante, the doctor said.",
        True,
        must_include="Bastante",
        must_exclude="the doctor said",
    ),
    Case("Sí, said the Mexican.", True, must_include="Sí", must_exclude="said the Mexican"),
    Case("Es muy amable, he said.", True, must_include="Es muy amable", must_exclude="he said"),
    Case("En este pueblo, he said.", True, must_include="En este pueblo", must_exclude="he said"),
    Case(
        "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
        True,
        must_include="Yo no sé nada",
    ),
    Case(
        "Lugares donde el fierro ya está en la tierra, the old man said. "
        "Lugares donde ha quemado el fuego.",
        True,
        must_include="Lugares donde el fierro",
    ),
    Case("Y por eso soy hereje, he said. Por eso y nada más.", True, must_include="Por eso y nada"),
    # --- Spanish opener, English narration (pattern D) ---------------------
    Case(
        "Escúchame, joven, the old man wheezed. If you could breathe a breath so strong "
        "you could blow out the wolf. Like you blow out the copo.",
        True,
        must_include="Escúchame, joven",
        must_exclude="breathe a breath",
        note="only the opener is Spanish",
    ),
    # --- English that must be left alone (pattern C + short-line traps) ----
    Case(
        "The matríz will not help you, the old man said. He said that the boy should find "
        "that place where acts of God and those of man are of a piece.",
        False,
        note="loanword matríz",
    ),
    Case(
        "He said that it was at such places that God sits and conspires in the destruction "
        "of that which he has been at such pains to create.",
        False,
    ),
    Case("He turned and stood.", False),
    Case("Yes mam.", False, note="lingua says SPANISH 0.58"),
    Case("Go on.", False, note="lingua says SPANISH 0.51"),
    Case("I dont intend to.", False, note="lingua says SPANISH 0.50"),
    Case("No one comes to see him. That's too bad, hey?", False),
    Case(
        "She touched her temple. He dont remember so good sometimes, she said. He is old.",
        False,
    ),
    # Bare place name behind a Spanish preposition. Decided: NOT worth a footnote
    # ("A Casas Grandes" = "To Casas Grandes" — the reader loses nothing). This is
    # a labelling decision, deliberately NOT special-cased in code: it escalates
    # and the model decides. See docs/model-selection.md.
    Case("A Casas Grandes, said Billy.", False, note="place name — no footnote wanted"),
]


def build_messages(text: str) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM}]
    for shot_text, shot_answer in FEWSHOT:
        payload = json.dumps(shot_answer, ensure_ascii=False)
        messages.append({"role": "user", "content": shot_text})
        messages.append({"role": "assistant", "content": payload})
    messages.append({"role": "user", "content": text})
    return messages


def ask(client: httpx.Client, model: str, text: str) -> tuple[dict | None, float, str]:
    started = time.perf_counter()
    try:
        response = client.post(
            f"{OLLAMA}/api/chat",
            json={
                "model": model,
                "messages": build_messages(text),
                "format": SCHEMA,
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_predict": 300},
            },
            timeout=240.0,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return None, time.perf_counter() - started, f"{type(exc).__name__}: {exc}"

    elapsed = time.perf_counter() - started
    content = response.json().get("message", {}).get("content", "")
    try:
        return json.loads(content), elapsed, ""
    except json.JSONDecodeError:
        return None, elapsed, f"unparseable: {content[:120]!r}"


@dataclass
class Score:
    classify_ok: int = 0
    classify_total: int = 0
    span_ok: int = 0
    span_total: int = 0
    false_positives: list[str] = None  # type: ignore[assignment]
    false_negatives: list[str] = None  # type: ignore[assignment]
    span_failures: list[str] = None  # type: ignore[assignment]
    errors: int = 0
    seconds: float = 0.0

    def __post_init__(self) -> None:
        self.false_positives = self.false_positives or []
        self.false_negatives = self.false_negatives or []
        self.span_failures = self.span_failures or []


def evaluate(model: str, cases: list[Case], *, verbose: bool) -> Score:
    score = Score()
    with httpx.Client() as client:
        for case in cases:
            answer, elapsed, error = ask(client, model, case.text)
            score.seconds += elapsed
            if answer is None:
                score.errors += 1
                print(f"    ERROR  {case.text[:50]!r}  {error}")
                continue

            got = bool(answer.get("is_spanish"))
            span = (answer.get("spanish_text") or "").strip()
            translation = (answer.get("translation") or "").strip()

            score.classify_total += 1
            if got == case.is_spanish:
                score.classify_ok += 1
            elif got:
                score.false_positives.append(case.text[:60])
            else:
                score.false_negatives.append(case.text[:60])

            if case.is_spanish and got and (case.must_include or case.must_exclude):
                score.span_total += 1
                ok = True
                if case.must_include and case.must_include.lower() not in span.lower():
                    ok = False
                if case.must_exclude and case.must_exclude.lower() in span.lower():
                    ok = False
                if ok:
                    score.span_ok += 1
                else:
                    score.span_failures.append(f"{case.text[:44]!r} -> span={span[:44]!r}")

            if verbose:
                mark = "ok " if got == case.is_spanish else "MISS"
                print(f"    [{mark}] {case.text[:52]:<54} es={got!s:<5} {translation[:44]}")
    return score


def report(model: str, score: Score) -> None:
    c_pct = 100 * score.classify_ok / score.classify_total if score.classify_total else 0
    s_pct = 100 * score.span_ok / score.span_total if score.span_total else 0
    per_case = score.seconds / max(1, score.classify_total + score.errors)

    print(f"\n  {model}")
    print(f"    classification  {score.classify_ok}/{score.classify_total}  ({c_pct:.0f}%)")
    print(f"    span precision  {score.span_ok}/{score.span_total}  ({s_pct:.0f}%)")
    print(f"    errors          {score.errors}")
    print(f"    latency         {per_case:.1f}s/case  ({score.seconds:.0f}s total)")
    if score.false_positives:
        print("    FALSE POSITIVES (would annotate English) — the damaging kind:")
        for item in score.false_positives:
            print(f"      · {item}")
    if score.false_negatives:
        print("    false negatives (missed Spanish):")
        for item in score.false_negatives:
            print(f"      · {item}")
    if score.span_failures:
        print("    span problems:")
        for item in score.span_failures:
            print(f"      · {item}")


def installed_models() -> list[str]:
    try:
        response = httpx.get(f"{OLLAMA}/api/tags", timeout=10.0)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"cannot reach ollama at {OLLAMA}: {exc}\nStart it with: ollama serve")
    return [m["name"] for m in response.json().get("models", [])]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="*", help="model tags (default: all installed)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every case")
    args = parser.parse_args()

    models = args.models or installed_models()
    if not models:
        sys.exit("no models installed — try: ollama pull qwen3:8b")

    print(f"{len(CASES)} cases · {sum(c.is_spanish for c in CASES)} Spanish, "
          f"{sum(not c.is_spanish for c in CASES)} English")

    results: list[tuple[str, Score]] = []
    for model in models:
        print(f"\n=== {model} ===")
        score = evaluate(model, CASES, verbose=args.verbose)
        results.append((model, score))
        report(model, score)

    print("\n\n=== summary (ranked by classification, then span) ===")
    print(f"{'model':<22} {'classify':>10} {'span':>8} {'FP':>4} {'FN':>4} {'s/case':>8}")
    for model, score in sorted(
        results,
        key=lambda r: (r[1].classify_ok / max(1, r[1].classify_total),
                       r[1].span_ok / max(1, r[1].span_total)),
        reverse=True,
    ):
        c = 100 * score.classify_ok / max(1, score.classify_total)
        s = 100 * score.span_ok / max(1, score.span_total)
        per = score.seconds / max(1, score.classify_total + score.errors)
        print(
            f"{model:<22} {c:>9.0f}% {s:>7.0f}% {len(score.false_positives):>4} "
            f"{len(score.false_negatives):>4} {per:>7.1f}s"
        )


if __name__ == "__main__":
    main()
