"""Tier-1 triage behaviour, including the adversarial cases from DESIGN.md §2.2.

The bands are a product decision, not an implementation detail: a spurious
footnote on ``Go on.`` is worse for the reading experience than a missed one, so
the accept threshold is high and the band is deliberately wide.
"""

from __future__ import annotations

import pytest

from etx.detect.triage import Triager, Verdict, strip_dialogue_tags


@pytest.fixture(scope="module")
def triager() -> Triager:
    return Triager()


# --------------------------------------------------------------- tag stripping


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tantos, said the man.", "Tantos"),
        ("Adelante, he cried.", "Adelante"),
        ("Güero, he said.", "Güero"),
        ("Bastante, the doctor said.", "Bastante"),
        ("Sí, said the Mexican.", "Sí"),
        ("Es muy amable, he said.", "Es muy amable"),
        ("A Casas Grandes, said Billy.", "A Casas Grandes"),
        ("Cuántos años tienes? the old man said.", "Cuántos años tienes?"),
    ],
)
def test_strips_trailing_dialogue_tags(text: str, expected: str) -> None:
    assert strip_dialogue_tags(text) == expected


def test_stripping_never_returns_empty() -> None:
    assert strip_dialogue_tags("he said.") != ""


def test_stripping_leaves_plain_prose_alone() -> None:
    text = "He turned the horse out along the rutted track and rode on."
    assert strip_dialogue_tags(text) == text


# ------------------------------------------------- confident accept and reject


@pytest.mark.parametrize(
    "text",
    [
        "Se fué.",
        "Y cómo se encuentra?",
        "Está bien.",
        "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
        "Lugares donde el fierro ya está en la tierra, the old man said.",
        "Dieciseis.",  # 1 word, but conf 1.00 — the LLM misses this one
    ],
)
def test_confident_spanish_is_accepted_without_the_llm(triager: Triager, text: str) -> None:
    result = triager.classify(text)
    assert result.verdict is Verdict.SPANISH, f"{text!r} -> {result}"
    assert not result.verdict.needs_llm


@pytest.mark.parametrize(
    "text",
    [
        "He turned and stood.",
        "The matríz will not help you, the old man said. He said that the boy should find "
        "that place where acts of God and those of man are of a piece.",
        "She touched her temple. He dont remember so good sometimes, she said. He is old.",
        "No one comes to see him. That's too bad, hey?",
    ],
)
def test_confident_english_is_rejected_without_the_llm(triager: Triager, text: str) -> None:
    """These are exactly the paragraphs the LLMs false-positive on."""
    result = triager.classify(text)
    assert result.verdict is Verdict.ENGLISH, f"{text!r} -> {result}"
    assert not result.verdict.needs_llm


# ------------------------------------------------------------ the safety property


@pytest.mark.parametrize("text", ["Go on.", "Yes mam.", "I dont intend to."])
def test_short_english_traps_are_never_accepted_as_spanish(triager: Triager, text: str) -> None:
    """The damaging error class. These may escalate, but must never auto-accept."""
    assert triager.classify(text).verdict is not Verdict.SPANISH


# ------------------------------------------------------------------- escalation


def test_ambiguous_fragments_escalate_or_resolve_but_never_misfire(triager: Triager) -> None:
    for text in ["Tan horrible.", "Tantos, said the man.", "En este pueblo, he said."]:
        verdict = triager.classify(text).verdict
        assert verdict in (Verdict.SPANISH, Verdict.UNCERTAIN), text


def test_real_spanish_just_below_threshold_escalates_rather_than_dropping(
    triager: Triager,
) -> None:
    """``Ay. Ándale, joven. Ándale pues.`` sits at 0.888 — just under accept.

    Escalating is the correct outcome, not a bug: it costs ~2s of local compute
    and the LLM resolves it. Lowering the threshold to swallow this case is
    tempting (no true-English sentence measured above 0.58, so there is a wide
    safe gap), but placing it needs the ~250-case golden set from M2, not this
    handful of examples.
    """
    result = triager.classify("Ay. Ándale, joven. Ándale pues.")
    assert result.verdict is Verdict.UNCERTAIN
    assert result.language == "SPANISH"
    assert 0.85 < result.confidence < 0.90


def test_tag_stripping_rescues_short_spanish(triager: Triager) -> None:
    """``Sí, said the Mexican.`` is 0.51 whole, 1.00 once the tag is gone."""
    result = triager.classify("Sí, said the Mexican.")
    assert result.verdict is Verdict.SPANISH
    assert result.stripped == "Sí"
    assert "strip" in result.reason


def test_disabling_tag_stripping_widens_the_band() -> None:
    text = "Tantos, said the man."
    assert Triager(strip_tags=True).classify(text).verdict is Verdict.SPANISH
    assert Triager(strip_tags=False).classify(text).verdict is Verdict.UNCERTAIN


def test_empty_and_punctuation_only_are_english(triager: Triager) -> None:
    for text in ["", "   ", "—", "..."]:
        assert triager.classify(text).verdict is Verdict.ENGLISH


def test_thresholds_are_configurable() -> None:
    text = "Tan horrible."
    strict = Triager(accept_spanish=0.999, accept_spanish_stripped=0.999, strip_tags=False)
    assert strict.classify(text).verdict is Verdict.UNCERTAIN


def test_result_reports_word_count(triager: Triager) -> None:
    assert triager.classify("Y cómo se encuentra?").word_count == 4


# ------------------------------------- embedded loanwords (design policy §1.1 C)


@pytest.mark.parametrize(
    ("segment", "span"),
    [
        ("You know what is orgullo?", "orgullo"),
        ("The matríz will not help you, the old man said.", "matríz"),
        ("Like you blow out the copo.", "copo"),
        ("He rode with the vaqueros all that day.", "vaqueros"),
        ("Where they burn the candela at night.", "candela"),
    ],
)
def test_lone_spanish_word_in_english_prose_is_rejected(segment: str, span: str) -> None:
    """Confirmed policy: no footnote for a single loanword inside English."""
    from etx.detect.triage import is_embedded_loanword

    assert is_embedded_loanword(segment, span), segment


@pytest.mark.parametrize(
    ("segment", "span"),
    [
        # one-word Spanish utterances + a dialogue tag — these MUST survive
        ("Bastante, the doctor said.", "Bastante"),
        ("Tantos, said the man.", "Tantos"),
        ("Güero, he said.", "Güero"),
        ("Sí, said the Mexican.", "Sí"),
        ("Adelante, he cried.", "Adelante"),
        # longer Spanish, with and without tags
        ("Cuántos años tienes? the old man said.", "Cuántos años tienes?"),
        ("Escúchame, joven, he said.", "Escúchame, joven"),
        ("Y por eso soy hereje, he said.", "Y por eso soy hereje"),
        ("Es muy amable, he said.", "Es muy amable"),
        ("Se fué.", "Se fué."),
    ],
)
def test_spanish_utterances_are_kept(segment: str, span: str) -> None:
    """The rule must not confuse a dialogue tag with surrounding English prose."""
    from etx.detect.triage import is_embedded_loanword

    assert not is_embedded_loanword(segment, span), segment


@pytest.mark.parametrize(
    ("segment", "span"),
    [
        # Real full-book false suppressions. The policy is about a single *word*,
        # so a multi-word Spanish span is never a loanword however much English
        # surrounds it.
        (
            "This man will be required to begin again whether he wishes to or no. "
            "Somos dolientes en la oscuridad.",
            "Somos dolientes en la oscuridad.",
        ),
        ("La tercera historia, said the gypsy, es ésta.", "La tercera historia"),
        ("Las alforjas, called out one of the riders.", "Las alforjas"),
        ("Quién es’, he hissed.", "Quién es’"),
        # The model silently repaired the book's OCR errors in the span it
        # reported ("to" -> "todo", "and" -> "y"), so words that *are* the Spanish
        # counted as English outside it. Sizing the span first makes that harmless.
        (
            "Me dice que él conoce to que sabe el lobo antes de que to sepa el lobo.",
            "Me dice que él conoce todo que sabe el lobo antes de que todo sepa el lobo.",
        ),
        ("Yeguas and caballos, capones and potros.", "Yeguas y caballos, capones y potros."),
    ],
)
def test_multiword_spanish_is_never_a_loanword(segment: str, span: str) -> None:
    from etx.detect.triage import is_embedded_loanword

    assert not is_embedded_loanword(segment, span), segment


def test_ratio_alone_would_get_these_wrong() -> None:
    """Documents *why* the rule strips tags instead of using a word ratio.

    Both spans are 1 word of 4-5, so any fraction-based rule gives them the same
    answer — but the correct answers are opposite.
    """
    from etx.detect.triage import is_embedded_loanword

    assert not is_embedded_loanword("Bastante, the doctor said.", "Bastante")
    assert is_embedded_loanword("You know what is orgullo?", "orgullo")


def test_empty_span_is_not_a_loanword() -> None:
    from etx.detect.triage import is_embedded_loanword

    assert not is_embedded_loanword("Some English text.", "")


def test_loanword_threshold_is_tunable() -> None:
    from etx.detect.triage import is_embedded_loanword

    seg, span = "Muy bien amigo", "amigo"
    assert is_embedded_loanword(seg, span, max_outside_words=1)
    assert not is_embedded_loanword(seg, span, max_outside_words=5)
