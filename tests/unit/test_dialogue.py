"""Dialogue-tag stripping — shared by triage confidence and the similarity veto."""

from __future__ import annotations

import pytest

from joven.dialogue import strip_dialogue_tags, word_count


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tantos, said the man.", "Tantos"),
        ("Bastante, the doctor said.", "Bastante"),
        ("Sí, he said.", "Sí"),
        ("he said, Sí", "Sí"),
        # verbs added after the first full-book run
        ("Quién es, he hissed.", "Quién es"),
        ("Vámonos, Billy shouted.", "Vámonos"),
        # an addressee hanging off the verb
        ("Tómelo, he called to them.", "Tómelo"),
        ("Vámonos, called the rider.", "Vámonos"),
        ("Está bien, he said to her.", "Está bien"),
    ],
)
def test_tags_are_stripped(text: str, expected: str) -> None:
    assert strip_dialogue_tags(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Yo no sé nada.",
        "Escúchame, joven.",
        "In the morning they were sitting naked in the dark water.",
        # "to" here is not an addressee — it must not be eaten
        "He said to hell with it.",
    ],
)
def test_prose_is_left_alone(text: str) -> None:
    assert strip_dialogue_tags(text) == text


def test_a_bare_tag_is_never_stripped_to_nothing() -> None:
    """Callers always need something to measure."""
    assert strip_dialogue_tags("he said.") != ""


def test_word_count_ignores_digits_and_punctuation() -> None:
    assert word_count("Somos dolientes en la oscuridad.") == 5
    assert word_count("Cuidado") == 1
    assert word_count("Quién es’") == 2
    assert word_count("  ") == 0
