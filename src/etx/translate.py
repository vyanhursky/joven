"""Tier 2: LLM adjudication and translation.

Tier 2 answers three questions at once, which is why it is an instruct LLM and not
a translation engine: *is this actually Spanish*, *which part of it is Spanish*,
and *what does it mean*. A pure NMT backend can only answer the third.

The default is a local model via Ollama — free, offline, no API key, and the only
backend actually in use.

:class:`ClaudeTranslator` is complete and tested but **not used**: a Claude Pro
subscription does not include API access (the API bills separately, as prepaid
credits), and no spend is planned for this project. It is kept because the seam is
free to maintain — ``anthropic`` is an optional dependency imported lazily, so
nothing in the default path touches it — and because the caching analysis behind it
(:mod:`etx.pricing`) is the reusable part. Its tests run against a fake client, so
it cannot silently rot.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

import httpx

from . import pricing as _pricing
from .dialogue import strip_dialogue_tags


class SpendCapExceeded(RuntimeError):
    """Raised when a metered backend reaches its configured budget.

    Deliberately fatal rather than a silent degrade: a run that quietly finishes
    half the book would be indistinguishable from a complete one in the sidecar.
    """

    def __init__(self, spent: float, cap: float, calls: int) -> None:
        super().__init__(
            f"spend cap reached: ${spent:.2f} of ${cap:.2f} after {calls:,} calls. "
            f"Raise --max-cost to continue, or re-run with --backend ollama."
        )
        self.spent = spent
        self.cap = cap
        self.calls = calls

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


def claude_available() -> tuple[bool, str]:
    """Whether a Claude call could authenticate, and why not if it couldn't.

    Worth a pre-flight check rather than discovering it per call: an unauthenticated
    run does not crash — every call is recorded as a traced ``tier2_error`` and the
    book finishes with an empty sidecar an hour later. Cheap to rule out up front.
    """
    try:
        import anthropic
    except ModuleNotFoundError:
        return False, "the anthropic SDK is not installed — pip install -e '.[claude]'"
    # Constructing the client resolves nothing — credentials are only exercised on
    # a request. `count_tokens` is the cheapest one that authenticates, and it does
    # not bill as usage.
    try:
        anthropic.Anthropic(timeout=15.0).messages.count_tokens(
            model=_pricing.DEFAULT_MODEL,
            messages=[{"role": "user", "content": "probe"}],
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


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
    if backend == "claude":
        # `model` defaults to the Ollama model name, so fall back to the paid
        # default rather than passing "qwen3:8b" to the Claude API.
        chosen = model if model in _pricing.MODELS else _pricing.DEFAULT_MODEL
        return ClaudeTranslator(model=chosen)
    raise ValueError(f"unknown backend {backend!r} — choose from: ollama, claude, stub")


# --------------------------------------------------------------------- Claude

# Extra few-shots for the paid backend, every one drawn from a real failure in
# the traced full-book run rather than invented.
#
# They serve two purposes at once, which is the whole reason the paid path has
# its own set. They teach the four error classes the local model got wrong; and
# they push the cached prefix past the model's minimum cacheable length, so the
# *larger* prompt is also the *cheaper* one (see :mod:`etx.pricing`). Kept off
# the Ollama path because an 8B model degrades with a prompt this long.
CLAUDE_EXTRA_FEWSHOT: list[tuple[str, dict]] = [
    # Cognates. A translation that looks like the source still tells the reader
    # something; the local model's near-identical output got vetoed instead.
    (
        "Cuatro días, he said.",
        {
            "is_spanish": True,
            "spanish_text": "Cuatro días",
            "translation": "Four days",
        },
    ),
    # Regionalisms with no clean English equivalent — translate them anyway,
    # because leaving the word untranslated is exactly what the reader can't use.
    (
        "Es ejido, said the girl.",
        {
            "is_spanish": True,
            "spanish_text": "Es ejido",
            "translation": "It's communal farmland",
        },
    ),
    (
        "Y este hacendado, said the rider, él vive en la colonia Morales?",
        {
            "is_spanish": True,
            "spanish_text": "Y este hacendado, él vive en la colonia Morales?",
            "translation": "And this landowner, does he live in the Morales colony?",
        },
    ),
    # Scan damage. The book is an OCR of a print edition: read through the
    # corruption rather than translating it literally.
    (
        "Debe guitar el bowl, said the girl.",
        {
            "is_spanish": True,
            "spanish_text": "Debe quitar el bozal",
            "translation": "He must take the muzzle off",
        },
    ),
    (
        "Y la tercera historic?",
        {
            "is_spanish": True,
            "spanish_text": "Y la tercera historia?",
            "translation": "And the third story?",
        },
    ),
    # A lone Spanish word inside English narration: report the span, and let the
    # caller's policy decide whether it earns a footnote.
    (
        "In the morning they were sitting naked in the dark water of the ciénega.",
        {"is_spanish": True, "spanish_text": "ciénega", "translation": "marsh"},
    ),
    # Context bleed. The single worst failure of the local run: a translation of
    # the *surrounding* paragraph instead of the one asked about.
    (
        "Ay. Ándale, joven. Ándale pues.",
        {
            "is_spanish": True,
            "spanish_text": "Ay. Ándale, joven. Ándale pues.",
            "translation": "Alas. Come on, young man. Go on then.",
        },
    ),
    # An idiom the local model rendered literally as "Pass it on" / "Give it to
    # him". It is an invitation — the sense is "come in" / "go ahead".
    (
        "Bueno, the man said. Desmonte. Pásale.",
        {
            "is_spanish": True,
            "spanish_text": "Bueno, Desmonte. Pásale.",
            "translation": "Good. Get down. Come on in.",
        },
    ),
    # A dialogue tag wrapping the whole utterance: exclude the tag from the span.
    (
        "Las alforjas, called out one of the riders.",
        {
            "is_spanish": True,
            "spanish_text": "Las alforjas",
            "translation": "The saddlebags",
        },
    ),
    # A complete Spanish sentence trailing an English one — still a footnote,
    # however much English precedes it.
    (
        "This man will be required to begin again whether he wishes to or no. "
        "Somos dolientes en la oscuridad.",
        {
            "is_spanish": True,
            "spanish_text": "Somos dolientes en la oscuridad.",
            "translation": "We are mourners in the darkness.",
        },
    ),
    # Scan damage that produces a real English word, which is the dangerous kind:
    # "fibre" is not a typo the model can see, only infer. (Está libre.)
    (
        "Está fibre. Tome.",
        {
            "is_spanish": True,
            "spanish_text": "Está libre.",
            "translation": "It's free.",
        },
    ),
    # A long multi-sentence passage. These are the footnotes that matter most —
    # the reader is otherwise stranded for a whole paragraph.
    (
        "No puedo recordar el mundo de luz, he said. Hace muchos años. Ese mundo "
        "es un mundo frágil. Ultimamente lo que vine a ver era más durable. Más "
        "verdadero.",
        {
            "is_spanish": True,
            "spanish_text": (
                "No puedo recordar el mundo de luz, he said. Hace muchos años. Ese "
                "mundo es un mundo frágil. Ultimamente lo que vine a ver era más "
                "durable. Más verdadero."
            ),
            "translation": (
                "I cannot remember the world of light, he said. Many years ago. That "
                "world is a fragile world. Lately what I came to see was more "
                "enduring. More true."
            ),
        },
    ),
    (
        "La tercera historia, said the gypsy, es ésta. Él existe en la historia de "
        "las historias. Es que ultimadamente la verdad no puede quedar en ningún "
        "otro lugar sino en el habla.",
        {
            "is_spanish": True,
            "spanish_text": (
                "La tercera historia, es ésta. Él existe en la historia de las "
                "historias. Es que ultimadamente la verdad no puede quedar en "
                "ningún otro lugar sino en el habla."
            ),
            "translation": (
                "The third history is this. It exists in the history of histories. "
                "Ultimately the truth cannot reside anywhere but in speech."
            ),
        },
    ),
]

# Structured outputs require every object to forbid unknown keys.
STRICT_SCHEMA = {**SCHEMA, "additionalProperties": False}


@dataclass(slots=True)
class ClaudeTranslator:
    """Claude via the official SDK. Opt-in, metered, and capped.

    Three things differ from :class:`OllamaTranslator` beyond the transport.

    **The prefix is cached.** System prompt and few-shots are identical on every
    call and make up ~75% of all input on a book run. A ``cache_control``
    breakpoint on the last few-shot turn bills them at a tenth of the base rate
    after the first call. Caching is requested only when the prefix actually
    clears the model's minimum — below it the API silently ignores the marker, so
    :attr:`cache_active` reports what really happened rather than what we asked
    for.

    **Thinking is off.** Sentence translation needs no deliberation, and thinking
    tokens bill as output. The documented risk of disabling it — reasoning
    leaking into the visible response — cannot reach us here: the reply is
    constrained to a JSON schema, so there is nowhere for stray prose to go.

    **Spend is capped.** Every response reports its own token usage, so the true
    cost accumulates in :attr:`spent` and the run aborts at
    :attr:`max_cost_usd` rather than discovering the overrun on a bill.
    """

    model: str = _pricing.DEFAULT_MODEL
    max_cost_usd: float = 2.00
    timeout: float = 120.0
    name: str = "claude"
    spent: float = 0.0
    calls: int = 0
    cache_reads: int = 0
    _client: object | None = None

    def __post_init__(self) -> None:
        self.name = f"claude:{self.model}"
        if self.model not in _pricing.MODELS:
            known = ", ".join(_pricing.MODELS)
            raise ValueError(f"unknown model {self.model!r} — choose from: {known}")

    # -- prompt assembly ---------------------------------------------------

    def _fewshot(self, base: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        return [*base, *CLAUDE_EXTRA_FEWSHOT]

    def _messages(self, text: str, context: str, fewshot: list[tuple[str, dict]]) -> list[dict]:
        """Few-shot turns first, then the paragraph under test.

        The cache breakpoint goes on the **last few-shot turn**, not the final
        user message: everything before it is byte-identical across calls, and
        the paragraph after it changes every time. Marking the last message
        instead would write a new cache entry per call and read none.
        """
        messages: list[dict] = []
        shots = self._fewshot(fewshot)
        for index, (shot_text, shot_answer) in enumerate(shots):
            messages.append({"role": "user", "content": shot_text})
            content: dict = {
                "type": "text",
                "text": json.dumps(shot_answer, ensure_ascii=False),
            }
            if index == len(shots) - 1:
                content["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            messages.append({"role": "assistant", "content": [content]})
        messages.append({"role": "user", "content": _user_turn(text, context)})
        return messages

    # -- transport ---------------------------------------------------------

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ModuleNotFoundError as exc:  # pragma: no cover - install-time path
                raise RuntimeError(
                    "the claude backend needs the anthropic SDK: "
                    "pip install -e '.[claude]'"
                ) from exc
            self._client = anthropic.Anthropic(timeout=self.timeout)
        return self._client

    def close(self) -> None:
        self._client = None

    def adjudicate(self, text: str, context: str = "") -> Verdict:
        return self._call(text, context, SYSTEM_PROMPT, FEWSHOT)

    def translate(self, text: str, context: str = "") -> Verdict:
        return self._call(text, context, TRANSLATE_ONLY_PROMPT, TRANSLATE_FEWSHOT)

    def _call(
        self, text: str, context: str, system: str, fewshot: list[tuple[str, dict]]
    ) -> Verdict:
        if self.spent >= self.max_cost_usd:
            raise SpendCapExceeded(self.spent, self.max_cost_usd, self.calls)

        started = time.perf_counter()
        try:
            response = self.client().messages.create(
                model=self.model,
                max_tokens=600,
                system=[{"type": "text", "text": system}],
                messages=self._messages(text, context, fewshot),
                thinking={"type": "disabled"},
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": STRICT_SCHEMA},
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface, never crash the run
            return Verdict(
                is_spanish=False,
                model=self.model,
                latency_s=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )

        latency = time.perf_counter() - started
        usage = response.usage
        cost = _pricing.call_cost(
            self.model,
            input_tokens=usage.input_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            output_tokens=usage.output_tokens,
        )
        self.spent += cost
        self.calls += 1
        if getattr(usage, "cache_read_input_tokens", 0):
            self.cache_reads += 1

        # A refusal is a successful HTTP response with empty content — reading
        # content[0] first would crash on it.
        if response.stop_reason == "refusal":
            return Verdict(
                is_spanish=False,
                model=self.model,
                latency_s=latency,
                cost_usd=cost,
                error="refusal: declined by safety classifiers",
            )

        raw = next((b.text for b in response.content if b.type == "text"), "")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return Verdict(
                is_spanish=False,
                model=self.model,
                latency_s=latency,
                raw=raw,
                cost_usd=cost,
                error=f"unparseable JSON: {exc}",
            )

        return Verdict(
            is_spanish=bool(parsed.get("is_spanish")),
            spanish_text=(parsed.get("spanish_text") or "").strip(),
            translation=(parsed.get("translation") or "").strip(),
            model=self.model,
            latency_s=latency,
            raw=raw,
            cost_usd=cost,
        )

    @property
    def cache_active(self) -> bool:
        """Whether the cache is actually being read, not merely requested."""
        return self.cache_reads > 0
