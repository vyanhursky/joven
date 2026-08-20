"""Flag annotations whose translation is probably wrong, to order the review pass.

The first full-book run settled what *doesn't* work: detector confidence. It
measures how hard Tier 1 found the language call, which is a property of sentence
length and dialogue tags — not of translation quality. Sampled across every band,
quality was uniformly good; ``En este pueblo, he said.`` sits at 0.50 and
translates perfectly.

What actually produces bad footnotes in this book is **damage in the scanned
source**. The OCR turned ``historia`` into ``historic``, ``sabe`` into ``Babe``,
``todo`` into ``to``, ``llegan`` into ``Megan`` — and the pipeline then faithfully
translates the corruption:

    La tercera historic          ->  "The third historic"
    Nadie Babe to que le espera  ->  "Nobody Babe to what awaits him"

These span the full confidence range (0.51-0.99), so no threshold finds them, and
they read as confident nonsense — the worst kind of error to ship to a device.

The signal used here is deliberately not a list of known OCR typos, which would
not survive a different book. It is the *consequence* they share: a word the model
could not translate survives verbatim into the English. That also catches genuine
untranslatable regionalisms (``huinche``, ``tablilla``, ``ciénega``), which is a
reasonable thing to want flagged for the same reason.
"""

from __future__ import annotations

import re

from .dialogue import SPEECH_VERBS, WORD, strip_dialogue_tags

# English that legitimately sits *inside* a Spanish span and will therefore always
# survive into the translation, meaning nothing:
#
#   the dialogue tag        "Vámonos, called the rider."  -> called, rider
#   McCarthy's code-switch  "You know what is parentesco?" -> know, what
#   loanwords English took  whiskey, salsa, mescal, rifle
#
# The speech verbs come from `dialogue.py` so the two cannot drift apart — the
# first version of this hand-listed them, missed the mid-sentence tags, and
# flagged 20% of the book.
_TAG_VOCABULARY = """
    they them their there that this then than what when where which while
    woman women girl boy man men rider riders driver doctor gypsy mozo drunk
    primadonna sepulturero carretero ganadero old young blind other another
    know knows knew said says asked look looking come came goes went
"""
_TAG_WORDS = frozenset(SPEECH_VERBS.split("|")) | frozenset(_TAG_VOCABULARY.split())

# Spanish that is also ordinary English, or a word no translation would alter.
_SHARED_VOCABULARY = """
    hotel general federal capital central animal criminal color favor
    error terror horror mayor real total normal legal local final
    whiskey salsa rifle chocolate plaza patio siesta fiesta adobe
    visible potable terrible horrible probable notable memorable
    enchiladas tortilla tequila mescal punchinello
"""
_SHARED = frozenset(_SHARED_VOCABULARY.split())

_MIN_LEN = 4

# A digit welded inside a run of letters — "É1 existe", "hab1a". Never legitimate
# in prose, so it is scan damage with no false-positive risk worth modelling.
_DIGIT_IN_WORD = re.compile(r"(?<=[^\W\d_])\d|\d(?=[^\W\d_])", re.UNICODE)


def _content_words(text: str) -> list[str]:
    """Lowercase words worth comparing, skipping short and shared vocabulary.

    Only words that are already lowercase in the source are considered, so proper
    nouns — ``Billy``, ``Niño``, ``San Diego``, ``Waterfills`` — cannot be mistaken
    for untranslated Spanish when they correctly appear in both.
    """
    return [
        word.casefold()
        for word in WORD.findall(strip_dialogue_tags(text))
        if len(word) >= _MIN_LEN
        and word[0].islower()
        and word.casefold() not in _SHARED
        and word.casefold() not in _TAG_WORDS
    ]


def carried_through(spanish: str, translation: str) -> list[str]:
    """Spanish content words that survived verbatim into the English."""
    target = {w.casefold() for w in WORD.findall(translation)}
    return sorted({w for w in _content_words(spanish) if w in target})


def garbled_tokens(text: str) -> list[str]:
    """Words containing a digit — unambiguous scan damage."""
    return sorted({w for w in re.findall(r"\S+", text) if _DIGIT_IN_WORD.search(w)})


def suspicions(spanish: str, translation: str) -> list[str]:
    """Human-readable reasons this annotation deserves a closer look.

    Empty means nothing detected — *not* that the translation is right. This
    orders a review pass; it does not replace one.
    """
    reasons: list[str] = []

    if carried := carried_through(spanish, translation):
        reasons.append("untranslated: " + ", ".join(carried))
    if garbled := garbled_tokens(spanish):
        reasons.append("garbled source: " + ", ".join(garbled))

    return reasons
