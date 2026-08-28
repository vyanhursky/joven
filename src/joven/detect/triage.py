"""Tier 1: cheap statistical triage with an explicit abstention band.

Measured behaviour of `lingua` on this book (DESIGN.md §2):

* Confident calls are excellent in both directions — real Spanish lands at
  0.90-1.00, real English at 0.99.
* **No true-English sentence ever scores high Spanish confidence.** Every false
  positive sits at 0.50-0.58, i.e. the detector *abstains* rather than being
  confidently wrong.

That second property is what makes this safe as a pre-filter: we accept only the
confident tail in each direction and escalate the middle. The escalation band is
large (~74% of Spanish candidates), which is the point — those are exactly the
cases a 1-3 word Spanish utterance welded to an English dialogue tag produces.

The tiers turn out to be complementary. Tier 1 nails isolated Spanish words with
distinctive orthography (``Dieciseis.`` -> 1.00) that the LLM misreads, and
rejects English-with-loanword prose (``The matríz will not help you...`` ->
ENGLISH 0.99) that the LLM sometimes flags. Tier 2 nails the mixed sentences that
Tier 1 can only shrug at.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

from ..dialogue import WORD as _WORD
from ..dialogue import strip_dialogue_tags, word_count


class Verdict(StrEnum):
    """Tier-1 outcome."""

    SPANISH = "spanish"  # confident enough to accept without asking the LLM
    ENGLISH = "english"  # confident enough to drop without asking the LLM
    UNCERTAIN = "uncertain"  # escalate to Tier 2

    @property
    def needs_llm(self) -> bool:
        return self is Verdict.UNCERTAIN


@dataclass(frozen=True, slots=True)
class TriageResult:
    text: str
    verdict: Verdict
    language: str
    confidence: float
    stripped: str | None = None  # tag-stripped text, when stripping changed the call
    reason: str = ""

    @property
    def word_count(self) -> int:
        return len(_WORD.findall(self.text))


def is_embedded_loanword(
    segment_text: str, spanish_text: str, *, max_outside_words: int = 1
) -> bool:
    """True when the Spanish is a word or two *inside* an otherwise English clause.

    Policy (DESIGN.md §1.1 pattern C, confirmed): a single Spanish word embedded in
    English prose does **not** get a footnote. McCarthy uses ``matríz``, ``copo``,
    ``candela``, ``vaquero`` as texture in English narration, and annotating every
    one produces constant noise for negligible gain.

    The obvious rule — "reject when the Spanish span is a small fraction of the
    sentence" — is wrong, because it cannot separate these two:

    ===========================  =============  ==========
    segment                      Spanish span   want
    ===========================  =============  ==========
    ``Bastante, the doctor       ``Bastante``   annotate
    said.``                      (1 of 4)
    ``You know what is           ``orgullo``    reject
    orgullo?``                    (1 of 5)
    ===========================  =============  ==========

    Same ratio, opposite answers. The actual discriminator is *what kind* of
    English surrounds the Spanish: in the first it is a **dialogue tag**, which is
    a closed set we already recognise. So strip the tags and look at what is left —
    if the remainder is essentially just the Spanish span, it is a Spanish
    utterance; if substantive English survives, the Spanish is embedded in it.
    """
    if not spanish_text.strip():
        return False

    # The policy is about a single *word* (DESIGN.md §7, embedded loanwords).
    # Anything longer is a Spanish utterance the reader needs, however much
    # English surrounds it — and the surrounding-English test alone got these
    # badly wrong on the full book:
    #
    #   "Somos dolientes en la oscuridad."  trailing a whole English sentence
    #   "La tercera historia, said the gypsy, es ésta."   split by its own tag
    #   "Me dice que él conoce todo que sabe el lobo..."  span vs OCR mismatch
    #
    # The last is the instructive one: the model silently repaired the book's
    # scanning errors ("to" -> "todo", "and" -> "y") in the span it reported, so
    # words that *are* the Spanish counted as English sitting outside it. Sizing
    # the span first makes that whole class of mismatch harmless.
    if word_count(spanish_text) > 1:
        return False

    remainder = strip_dialogue_tags(segment_text)
    span_words = {w.casefold() for w in _WORD.findall(spanish_text)}
    outside = [w for w in _WORD.findall(remainder) if w.casefold() not in span_words]
    return len(outside) > max_outside_words


# A two-language detector cannot answer "neither". Asked about Latin it must pick
# English or Spanish, and it does not pick English: *Suttree* -- a novel with no
# Spanish in it at all -- produced `Stabat Mater Dolorosa.` as SPANISH 0.94, over
# the accept threshold, which skips adjudication entirely and becomes a confident,
# wrong footnote.
#
# The obvious fix -- add Latin to the detector -- is wrong, and measurably so.
# Latin is close enough to Spanish to take probability mass from it, and the
# thresholds here are tuned against a two-language distribution:
#
#     text                        EN/ES        EN/ES/LA
#     Dieciseis.                  1.00 accept  0.54 escalate
#     Vaya con Dios.              ~1.0 accept  0.72 escalate
#     He rode back in the dark.   0.99 reject  0.80 escalate
#
# `Dieciseis.` at 1.00 is a *documented* Tier-1 strength (see the module docstring)
# and the LLM gets it wrong, so trading it away to catch Latin is a bad exchange.
#
# So the primary detector is left exactly as it was, and Latin is asked about
# separately, as a veto. The question is not "how Latin is this?" but "when Latin
# is on the ballot, does it win?" -- which real Spanish survives and Latin does not.
LATIN_LANGUAGES = (Language.ENGLISH, Language.SPANISH, Language.LATIN)


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    return (
        LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.SPANISH)
        .with_preloaded_language_models()
        .build()
    )


@lru_cache(maxsize=1)
def _latin_detector() -> LanguageDetector:
    return (
        LanguageDetectorBuilder.from_languages(*LATIN_LANGUAGES)
        .with_preloaded_language_models()
        .build()
    )


def looks_latin(text: str) -> bool:
    """True when Latin outranks both English and Spanish for this text.

    Deliberately a ranking test rather than a confidence threshold. The three
    Latin passages in *Suttree* score 0.67, 0.85 and 0.98 as Latin -- no cutoff
    separates those from noise without also catching real Spanish -- but Latin
    *wins* in all three.

    **The dialogue tag is stripped first**, which is what makes the rule safe.
    Measured over 781 reviewed spans from *The Crossing* and 313 from *All the
    Pretty Horses*, the raw ranking test wrongly vetoes ten genuine Spanish
    passages; every one of them that survives to the accept path is dialogue --
    ``Respóndele, he said.``, ``La matríz, the old man said.`` -- and the English
    attribution is doing it:

    ===============================  ============  ==========
    text                             latin / span  stripped
    ===============================  ============  ==========
    ``Respóndele, he said.``         0.64 / 0.34   0.00 / 1.00
    ``La matríz, the old man said.`` 0.44 / 0.42   0.00 / 1.00
    ===============================  ============  ==========

    That is the same tag, distorting a third measurement, for the same reason it
    distorted the other two (see :mod:`joven.dialogue`).
    """
    stripped = strip_dialogue_tags(text)
    values = _latin_detector().compute_language_confidence_values(stripped)
    return bool(values) and str(values[0].language) == "Language.LATIN"


@dataclass(frozen=True, slots=True)
class Triager:
    """Bands tuned to the measurements in DESIGN.md §2.

    ``accept_spanish`` is deliberately high: a spurious footnote on ``Go on.`` is
    far more damaging to the reading experience than a missed one, so we would
    rather escalate than guess.
    """

    accept_spanish: float = 0.90
    reject_english: float = 0.90
    # Ask whether a passage is Latin before calling it Spanish. Off only for tests
    # that want the pre-veto behaviour.
    veto_latin: bool = True
    min_words_to_accept: int = 3
    strip_tags: bool = True
    # a tag-stripped fragment must clear a higher bar, since it has less signal
    accept_spanish_stripped: float = 0.95

    def classify(self, text: str) -> TriageResult:
        cleaned = text.strip()
        if not cleaned or not _WORD.findall(cleaned):
            return TriageResult(text, Verdict.ENGLISH, "none", 1.0, reason="no letters")

        language, confidence = self._detect(cleaned)
        words = len(_WORD.findall(cleaned))

        if language == "SPANISH" and confidence >= self.accept_spanish:
            # The accept path is the dangerous one: it skips adjudication entirely,
            # so a wrong call here becomes a footnote with nothing downstream to
            # catch it. Everything the veto would wrongly reject *escalates*
            # anyway, where the model still gets a say -- so ask about Latin only
            # here, where being wrong is unrecoverable.
            if self.veto_latin and looks_latin(cleaned):
                return TriageResult(
                    text, Verdict.ENGLISH, "LATIN", confidence, reason="latin, not spanish"
                )
            if words >= self.min_words_to_accept:
                return TriageResult(
                    text, Verdict.SPANISH, language, confidence, reason="confident spanish"
                )
            # very short but very confident (e.g. "Sí.", "Dieciseis.") — still accept
            if confidence >= 0.99:
                return TriageResult(
                    text, Verdict.SPANISH, language, confidence, reason="short but certain"
                )
            return TriageResult(
                text, Verdict.UNCERTAIN, language, confidence, reason="too short to accept"
            )

        if language == "ENGLISH" and confidence >= self.reject_english:
            return TriageResult(
                text, Verdict.ENGLISH, language, confidence, reason="confident english"
            )


        # Ambiguous. Try again with the English dialogue tag removed.
        if self.strip_tags:
            stripped = strip_dialogue_tags(cleaned)
            if stripped != cleaned and _WORD.findall(stripped):
                s_language, s_confidence = self._detect(stripped)
                if s_language == "SPANISH" and s_confidence >= self.accept_spanish_stripped:
                    return TriageResult(
                        text,
                        Verdict.SPANISH,
                        s_language,
                        s_confidence,
                        stripped=stripped,
                        reason="confident spanish after tag strip",
                    )

        return TriageResult(text, Verdict.UNCERTAIN, language, confidence, reason="in band")

    def _detect(self, text: str) -> tuple[str, float]:
        values = _detector().compute_language_confidence_values(text)
        if not values:
            return "none", 0.0
        top = values[0]
        return str(top.language).replace("Language.", ""), top.value
