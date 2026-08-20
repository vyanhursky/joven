"""Tier 2 backends and the normalization veto."""

from __future__ import annotations

import pytest

from etx.translate import StubTranslator, get_translator, is_normalization


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("Yessir.", "Yes sir."),
        ("He's done ate.", "He's done eating."),
        ("She come up out of Mexico.", "She came up out of Mexico."),
        ("Where you live at?", "Where do you live?"),
        ("I dont intend to.", "I don't intend to."),
        ("Nosir.", "No sir."),
    ],
)
def test_dialect_normalization_is_vetoed(source: str, translation: str) -> None:
    """The bug a traced book run exposed: dialect English 'translated' to English."""
    assert is_normalization(source, translation), f"{source!r} -> {translation!r}"


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("Se fué.", "He is gone."),
        ("Vaya con Dios.", "Go with God."),
        ("Bastante.", "Enough."),
        ("Sí.", "Yes."),
        ("Cuántos años tienes?", "How old are you?"),
        ("Yo no sé nada.", "I know nothing."),
        ("Está bien.", "That's fine."),
        ("Ándale, joven.", "Come on, young man."),
        ("Lugares donde ha quemado el fuego.", "Places where the fire has burned."),
    ],
)
def test_real_translations_survive(source: str, translation: str) -> None:
    assert not is_normalization(source, translation), f"{source!r} -> {translation!r}"


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        # A dialogue tag survives translation verbatim, so it is shared text that
        # was never translated. Comparing it inflated the ratio and vetoed 35
        # genuinely Spanish segments on the first full-book run.
        ("Cuatro días, he said.", "Four days, he said."),
        ("Dos ordenes de las enchiladas, Billy said.", "Two orders of the enchiladas, Billy said."),
        ("Los papeles, said the man.", "The papers, said the man."),
        ("El médico, Billy said.", "The doctor, Billy said."),
        ("Más, he said.", "More, he said."),
        ("Permítame, he said.", "Let me, he said."),
        ("Vámonos, said Billy.", "Come on, said Billy."),
        ("Sí, sí, she whispered.", "Yes, yes, she whispered."),
        ("Tómelo, he called to them.", "Take it, he called to them."),
    ],
)
def test_shared_dialogue_tag_does_not_veto_a_real_translation(
    source: str, translation: str
) -> None:
    assert not is_normalization(source, translation), f"{source!r} -> {translation!r}"


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        # ...but stripping the tag must not rescue a genuine no-op. Each of these
        # is a name, a place, or English the model simply echoed back.
        ("Old Niño, he said.", "Old Niño, he said."),
        ("Porfirio, he said.", "Porfirio, he said."),
        ("He rode as far south as Cuauhtémoc.", "He rode as far south as Cuauhtémoc."),
        ("Bayo cebruno.", "Bayo cebruno."),
        ("La tablilla, cried the carretero.", "The tablilla"),
        ("Al none, he said.", "At none"),
        ("Seize them back, whispered the sepulturero.", "Take them back"),
    ],
)
def test_no_op_translations_are_still_vetoed_after_tag_stripping(
    source: str, translation: str
) -> None:
    assert is_normalization(source, translation), f"{source!r} -> {translation!r}"


def test_veto_ignores_case_and_punctuation() -> None:
    assert is_normalization("Yessir.", "yes sir")


def test_veto_handles_empty_input() -> None:
    assert not is_normalization("", "something")
    assert not is_normalization("something", "")


def test_veto_threshold_is_tunable() -> None:
    assert is_normalization("Sí.", "Yes.", threshold=0.3)
    assert not is_normalization("Sí.", "Yes.", threshold=0.9)


def test_stub_is_deterministic_and_offline() -> None:
    stub = StubTranslator()
    first = stub.adjudicate("Se fué.")
    second = stub.adjudicate("Se fué.")
    assert first == second
    assert first.is_spanish
    assert not stub.adjudicate("Plain english here.").is_spanish


def test_get_translator_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        get_translator("telepathy")


def test_get_translator_returns_stub() -> None:
    assert get_translator("stub").name == "stub"


# ---------------------------------------------------------------- context bleed


def test_context_is_delimited_and_marked_do_not_translate() -> None:
    """Regression: a context ending in Spanish got translated instead of the target.

    Observed on real output — context ``...Se fué.`` + paragraph
    ``Ay. Ándale, joven.`` produced ``He went away. Come on, young man.``, i.e. a
    translation of the context. The prompt now hard-delimits the two.
    """
    from etx.translate import OllamaTranslator

    messages = OllamaTranslator()._messages("Está bien.", "Where is the sun? Se fué.")
    user = messages[-1]["content"]
    assert "<context>" in user and "</context>" in user
    assert "<paragraph>" in user and "</paragraph>" in user
    assert "Do NOT translate" in user
    # the target must appear inside the paragraph delimiters, not the context ones
    assert user.index("Está bien.") > user.index("</context>")


def test_no_context_means_no_delimiters() -> None:
    from etx.translate import OllamaTranslator

    user = OllamaTranslator()._messages("Está bien.", "")[-1]["content"]
    assert user == "Está bien."


@pytest.mark.ollama
def test_live_model_does_not_bleed_context(request: pytest.FixtureRequest) -> None:
    """End-to-end guard, skipped unless ollama is up. Cases avoid the few-shots."""
    from etx.translate import DEFAULT_MODEL, OllamaTranslator, installed_models, ollama_available

    if not ollama_available() or DEFAULT_MODEL not in installed_models():
        pytest.skip("ollama or the default model is unavailable")

    translator = OllamaTranslator()
    try:
        verdict = translator.translate("Dieciseis.", "He turned and stood. Cuántos años tienes?")
    finally:
        translator.close()
    assert verdict.translation
    # must not echo a translation of the context question
    assert "how old" not in verdict.translation.lower()
