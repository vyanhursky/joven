"""Suspicion signals used to order the review pass.

Cases are taken from the first full-book run, so a regression here is a regression
against real output rather than against an invented example.
"""

from __future__ import annotations

import pytest

from joven.suspicion import carried_through, garbled_tokens, suspicions


@pytest.mark.parametrize(
    ("spanish", "translation", "expected"),
    [
        # scan damage the model then translated faithfully
        ("Y la tercera historic?", "And the third historic?", "historic"),
        ("Que undo, she said.", "What undo", "undo"),
        ("Es feroz la perm, no?", "It's fierce the perm, isn't it?", "perm"),
        ("Está fibre.", "It's fibre.", "fibre"),
        # a regionalism the model could not render
        ("Adónde se fué su compadre?", "Where did his compadre go?", "compadre"),
        ("Del ejido?", "From the ejido?", "ejido"),
    ],
)
def test_untranslated_words_are_flagged(spanish: str, translation: str, expected: str) -> None:
    assert expected in carried_through(spanish, translation)


@pytest.mark.parametrize(
    ("spanish", "translation"),
    [
        # A dialogue tag is English on *both* sides by construction. Counting it
        # flagged 20% of the book on the first attempt.
        ("Ándale, he said.", "Come on, he said."),
        ("Vámonos, called the rider.", "Come on, called the rider."),
        ("Sí, sí, she whispered.", "Yes, yes, she whispered."),
        ("Pobrecito, the girl said.", "Poor thing, said the girl."),
        ("Y este hacendado, said the rider, él vive allá?", "And this man, said the rider, ...?"),
        ("Herida tan grave, they said.", "Wounded so badly, they said."),
        # words that are simply the same in both languages
        ("Y traiga un vaso de aqua potable.", "And bring a glass of potable water."),
        ("El mundo visible es nada.", "The visible world is nothing."),
        ("No le gusta el whiskey?", "Does he not like whiskey?"),
        # a proper noun correctly appearing in both
        ("Estoy regresándole a mi país, Billy said.", "I'm returning to my country, Billy said."),
        ("Vámonos, said Billy.", "Come on, said Billy."),
        # ordinary good translations
        ("Cuántos años tienes?", "How old are you?"),
        ("Lugares donde ha quemado el fuego.", "Places where the fire has burned."),
    ],
)
def test_clean_translations_are_not_flagged(spanish: str, translation: str) -> None:
    assert suspicions(spanish, translation) == [], f"{spanish!r} -> {translation!r}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("É1 existe en la historia", "É1"),
        ("E1 cuero?", "E1"),
        ("Conoc16 a su sobrino?", "Conoc16"),
        ("No es cuest16n de ningún sello", "cuest16n"),
    ],
)
def test_digit_welded_into_a_word_is_scan_damage(text: str, expected: str) -> None:
    assert expected in garbled_tokens(text)


@pytest.mark.parametrize(
    "text",
    ["Cuatro días, he said.", "Tengo 16 años.", "Dieciseis.", "Nueve días. Nueve noches."],
)
def test_ordinary_text_is_not_garbled(text: str) -> None:
    assert garbled_tokens(text) == []


def test_known_limitation_decade_forms_would_flag() -> None:
    """``1930s`` is a digit welded to a letter and would be reported.

    Documented rather than special-cased: it never fired on the real book (5
    garbled tokens across 725 annotations, all genuine), the signal only ever sees
    Spanish dialogue spans, and a wrong flag costs one glance at a badge that names
    the word. Worth revisiting if a book with dated prose comes along.
    """
    assert garbled_tokens("the 1930s") == ["1930s"]


def test_reasons_name_the_offending_word() -> None:
    """A false positive must be dismissable without re-reading the paragraph."""
    reasons = suspicions("La tercera historic, said the gypsy. É1 existe.", "The third historic")
    assert any("historic" in r for r in reasons)
    assert any("É1" in r for r in reasons)


def test_empty_input_is_not_suspicious() -> None:
    assert suspicions("", "") == []
    assert suspicions("Se fué.", "") == []
