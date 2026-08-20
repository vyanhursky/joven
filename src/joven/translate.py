"""Tier 2: LLM adjudication and translation.

Tier 2 answers three questions at once, which is why it is an instruct LLM and not
a translation engine: *is this actually Spanish*, *which part of it is Spanish*,
and *what does it mean*. A pure NMT backend can only answer the third.

The backend is a local model via Ollama — free, offline, and no API key. The
:class:`Translator` protocol is the seam: anything answering :meth:`adjudicate`
and :meth:`translate` can stand in, which is what :class:`StubTranslator` does
for the tests.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

import httpx

from .dialogue import strip_dialogue_tags

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:8b"

# JSON schema handed to the model so the response shape is guaranteed.
SCHEMA = {
    "type": "object",
    "properties": {
        "is_spanish": {"type": "boolean"},
        "spanish_text": {"type": "string"},
        "translation": {"type": "string"},
    },
    "required": ["is_spanish", "spanish_text", "translation"],
}

SYSTEM_PROMPT = """\
You analyse single paragraphs from an English-language novel set on the \
US-Mexico border in the 1930s. Some paragraphs contain untranslated Spanish \
dialogue that an English-only reader cannot understand.

The novel's English is deliberately dialectal and non-standard: dropped
apostrophes ("dont", "aint", "wasnt"), regional grammar ("He's done ate",
"She come up out of Mexico", "Where you live at?"), and run-together speech
("Yessir", "Yesmam", "Nosir"). **That is English.** Rewriting it into standard
English is NOT translation, and those paragraphs must be reported as English.

For the paragraph you are given, return JSON with three fields:

is_spanish
  true only if the paragraph contains actual Spanish that an English-only
  reader could not understand.
  false for English prose - including
    - dialect or non-standard English, however unusual it looks
    - English prose containing isolated Spanish loanwords or proper nouns
      (matriz, copo, candela, vaquero, guero, Casas Grandes)
    - short plain English lines
  If your translation would just be a tidied-up version of the same words,
  the answer is false.

spanish_text
  The Spanish substring, copied verbatim from the paragraph. Exclude English
  narration and English dialogue tags such as "he said", "she said",
  "said the man", "the old man said", "he cried". Empty string when
  is_spanish is false.

translation
  Natural English translation of spanish_text, preserving any English dialogue
  tag that sits inside it. Empty string when is_spanish is false.

Answer with JSON only."""

# Few-shot examples covering the four mixing patterns from DESIGN.md §1.1.
# These carry most of the span-precision performance — see docs/model-selection.md.
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
    ("Go on.", {"is_spanish": False, "spanish_text": "", "translation": ""}),
    # Dialect English. Added after a traced full-book run showed the model
    # "translating" these into standard English and labelling them Spanish.
    ("Yessir.", {"is_spanish": False, "spanish_text": "", "translation": ""}),
    (
        "He's done ate. She come up out of Mexico.",
        {"is_spanish": False, "spanish_text": "", "translation": ""},
    ),
]

# --------------------------------------------------------------- output guard

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
SIMILARITY_VETO = 0.75


def _flatten(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub("", text.lower())).strip()


def is_normalization(
    spanish_text: str, translation: str, *, threshold: float = SIMILARITY_VETO
) -> bool:
    """True when the "translation" is really the same words tidied up.

    A deterministic backstop for the failure mode a traced full-book run exposed:
    the model rewriting McCarthy's dialect English into standard English and
    labelling it Spanish —

    ==============================  ===========================  =====
    source                          "translation"                ratio
    ==============================  ===========================  =====
    ``Yessir.``                     ``Yes sir.``                 0.92
    ``He's done ate.``              ``He's done eating.``        0.81
    ``She come up out of Mexico.``  ``She came up out of ...``   0.96
    ``Se fué.``                     ``He is gone.``              0.25
    ``Bastante.``                   ``Enough.``                  0.14
    ==============================  ===========================  =====

    This is a *semantic* rule rather than a heuristic patch: if the translation
    barely differs from the source, the footnote conveys nothing to the reader —
    so vetoing is the right call even for genuine Spanish cognates.

    Prompt guidance alone was not enough to rely on; this does not depend on the
    model complying.

    **Dialogue tags are removed from both sides first.** McCarthy's attributions
    survive translation verbatim, so they are shared text that was never
    translated, and on a full-book run they dragged real translations over the
    threshold — the veto fired on 35 genuinely Spanish segments:

    ====================================  =============================  =====
    compared                              ratio                          verdict
    ====================================  =============================  =====
    ``Cuatro días, he said.``             vs ``Four days, he said.``      vetoed
    ``Cuatro días``                       vs ``Four days``               kept
    ``Dos ordenes de las enchiladas, B``  vs ``Two orders of the ench``  vetoed
    ``Dos ordenes de las enchiladas``     vs ``Two orders of the ench``  kept
    ====================================  =============================  =====

    The cases the veto exists for are unaffected: ``Yessir.`` carries no tag to
    strip, and ``Old Niño, he said.`` is identical on both sides with or without
    it.
    """
    source = _flatten(strip_dialogue_tags(spanish_text))
    target = _flatten(strip_dialogue_tags(translation))
    if not source or not target:
        return False
    return SequenceMatcher(None, source, target).ratio() >= threshold


def _user_turn(text: str, context: str) -> str:
    """The paragraph under test, fenced off from its surrounding context.

    Surrounding context is what makes bare fragments like ``Bastante.`` or
    ``Tantos.`` adjudicable at all — but it bleeds. Observed on the local model:
    with a context ending ``...Se fué.``, the paragraph ``Ay. Ándale, joven.``
    came back as "He went away. Come on, young man." — a translation of the
    *context*. Hence hard delimiters and an explicit do-not-translate line.

    Shared by both backends so the fix cannot drift between them.
    """
    if not context:
        return text
    return (
        "<context>\n" + context + "\n</context>\n"
        "The context above is background only. Do NOT translate it and do "
        "NOT include any of it in your answer.\n\n"
        "<paragraph>\n" + text + "\n</paragraph>\n"
        "Answer about the paragraph above, and nothing else."
    )


@dataclass(frozen=True, slots=True)
class Verdict:
    """What Tier 2 concluded about one segment."""

    is_spanish: bool
    spanish_text: str = ""
    translation: str = ""
    model: str = ""
    latency_s: float = 0.0
    raw: str = ""
    error: str = ""
    cost_usd: float = 0.0
    """What this call actually cost, from the API's own usage figures. 0.0 offline."""

    @property
    def ok(self) -> bool:
        return not self.error


TRANSLATE_ONLY_PROMPT = """\
You translate Spanish passages from an English-language novel set on the \
US-Mexico border in the 1930s into natural English.

The passage has already been confirmed as Spanish — do not second-guess that.
Your only job is to translate it.

Return JSON with three fields:

is_spanish
  Always true.

spanish_text
  The Spanish portion, copied verbatim. Exclude English dialogue tags such as
  "he said", "she said", "said the man", "the old man said" - leave those out of
  the span but keep them in the translation where they occur mid-quote.

translation
  Natural English translation. Never return the Spanish unchanged, and never
  return an empty string.

Answer with JSON only."""

TRANSLATE_FEWSHOT: list[tuple[str, dict]] = [
    (
        "Dieciseis.",
        {"is_spanish": True, "spanish_text": "Dieciseis.", "translation": "Sixteen."},
    ),
    (
        "Adiós compadrito, they said.",
        {
            "is_spanish": True,
            "spanish_text": "Adiós compadrito,",
            "translation": "Goodbye little friend, they said.",
        },
    ),
    (
        "Ay. Ándale, joven. Ándale pues.",
        {
            "is_spanish": True,
            "spanish_text": "Ay. Ándale, joven. Ándale pues.",
            "translation": "Oh. Come on, young man. Go on then.",
        },
    ),
]


class Translator(Protocol):
    """Tier 2 backend.

    Two entry points, because the two jobs have different failure modes:

    :meth:`adjudicate`
        For the abstention band. Decides *whether* it is Spanish, and translates.

    :meth:`translate`
        For segments Tier 1 already accepted with high confidence. Translation
        only — the model is told not to second-guess the classification.

    The split exists because a combined prompt let the LLM veto Tier 1's correct
    calls (it labels ``Dieciseis.`` English), while skipping the LLM entirely for
    Tier-1 accepts left those annotations with **no translation at all** — blank
    popups on the device.
    """

    name: str

    def adjudicate(self, text: str, context: str = "") -> Verdict: ...

    def translate(self, text: str, context: str = "") -> Verdict: ...


@dataclass(slots=True)
class StubTranslator:
    """Deterministic, offline, no network. Used by the whole test suite.

    Calls anything containing a non-ASCII letter Spanish, which is crude but
    stable — the point is to exercise the pipeline, not to be right.
    """

    name: str = "stub"

    def adjudicate(self, text: str, context: str = "") -> Verdict:
        looks_spanish = any(ord(ch) > 127 for ch in text)
        return Verdict(
            is_spanish=looks_spanish,
            spanish_text=text if looks_spanish else "",
            translation=f"[stub translation of {text[:40]}]" if looks_spanish else "",
            model="stub",
        )

    def translate(self, text: str, context: str = "") -> Verdict:
        return Verdict(
            is_spanish=True,
            spanish_text=text,
            translation=f"[stub translation of {text[:40]}]",
            model="stub",
        )


@dataclass(slots=True)
class OllamaTranslator:
    """Local model via Ollama. The default backend — free and offline."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    timeout: float = 240.0
    name: str = "ollama"
    _client: httpx.Client | None = None

    def __post_init__(self) -> None:
        self.name = f"ollama:{self.model}"

    def _messages(
        self,
        text: str,
        context: str,
        system: str = SYSTEM_PROMPT,
        fewshot: list[tuple[str, dict]] | None = None,
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": system}]
        for shot_text, shot_answer in (fewshot if fewshot is not None else FEWSHOT):
            messages.append({"role": "user", "content": shot_text})
            messages.append(
                {"role": "assistant", "content": json.dumps(shot_answer, ensure_ascii=False)}
            )
        messages.append({"role": "user", "content": _user_turn(text, context)})
        return messages

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def adjudicate(self, text: str, context: str = "") -> Verdict:
        """Band case: decide whether it is Spanish, and translate it."""
        return self._call(text, context, SYSTEM_PROMPT, FEWSHOT)

    def translate(self, text: str, context: str = "") -> Verdict:
        """Tier-1 accept: translate only, without re-litigating the language."""
        return self._call(text, context, TRANSLATE_ONLY_PROMPT, TRANSLATE_FEWSHOT)

    def _call(
        self, text: str, context: str, system: str, fewshot: list[tuple[str, dict]]
    ) -> Verdict:
        started = time.perf_counter()
        try:
            response = self.client().post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": self._messages(text, context, system, fewshot),
                    "format": SCHEMA,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "num_predict": 400},
                },
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - surface, never crash the run
            return Verdict(
                is_spanish=False,
                model=self.model,
                latency_s=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )

        latency = time.perf_counter() - started
        raw = response.json().get("message", {}).get("content", "")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return Verdict(
                is_spanish=False,
                model=self.model,
                latency_s=latency,
                raw=raw,
                error=f"unparseable JSON: {exc}",
            )

        return Verdict(
            is_spanish=bool(parsed.get("is_spanish")),
            spanish_text=(parsed.get("spanish_text") or "").strip(),
            translation=(parsed.get("translation") or "").strip(),
            model=self.model,
            latency_s=latency,
            raw=raw,
        )



def ollama_available(base_url: str = DEFAULT_OLLAMA_URL) -> bool:
    try:
        return httpx.get(f"{base_url}/api/version", timeout=3.0).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def installed_models(base_url: str = DEFAULT_OLLAMA_URL) -> list[str]:
    try:
        response = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        response.raise_for_status()
    except Exception:  # noqa: BLE001
        return []
    return [m["name"] for m in response.json().get("models", [])]


def get_translator(backend: str, model: str = DEFAULT_MODEL) -> Translator:
    if backend == "stub":
        return StubTranslator()
    if backend == "ollama":
        return OllamaTranslator(model=model)
    raise ValueError(f"unknown backend {backend!r} — choose from: ollama, stub")
