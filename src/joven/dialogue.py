"""Dialogue-tag recognition, shared by detection and translation.

McCarthy writes unquoted dialogue with a trailing or leading attribution —
``Tantos, said the man.`` — and that English scaffolding distorts two different
measurements:

1. **Language confidence** (:mod:`joven.detect.triage`). ``Tantos, said the man.``
   scores 0.50; the bare ``Tantos`` scores 0.98. Stripping the tag concentrates
   the signal in the words that carry it.
2. **Source/translation similarity** (:func:`joven.translate.is_normalization`).
   The tag is *identical* on both sides — ``Cuatro días, he said.`` against
   ``Four days, he said.`` — so it inflates the ratio with text that was never
   translated, and a real translation gets vetoed as a no-op.

Both needed the same closed set of verbs, so it lives here rather than in either
caller. Keeping it out of :mod:`joven.detect.triage` also means the translator no
longer drags ``lingua`` into every import.
"""

from __future__ import annotations

import re

# Speech verbs McCarthy actually uses, gathered from the book rather than
# guessed. Membership only ever removes attribution, so a missing verb costs
# recall (a tag survives, confidence stays low) and never precision.
SPEECH_VERBS = (
    r"said|says|asked|replied|answered|cried|called|whispered|murmured|muttered"
    r"|wheezed|shouted|repeated|added|announced|observed|hissed|breathed"
    r"|gasped|laughed|nodded|snorted|spat"
)
_SUBJECT = r"(?:he|she|they|i|the\s+\w+(?:\s+\w+)?|[A-Z][a-z]+)"
# " to them" / " to the boy" — an addressee hanging off the verb. Without this the
# tag in ``Tómelo, he called to them.`` survives on both sides of a similarity
# comparison and vetoes a real translation.
_ADDRESSEE = rf"(?:\s+to\s+(?:him|her|them|me|us|{_SUBJECT}))?"

# ", he said." / ", said the man." / " the old man said." / ", he called to them."
_TAG_TRAILING = re.compile(
    rf"[,;]?\s*(?:{_SUBJECT}\s+(?:{SPEECH_VERBS})|(?:{SPEECH_VERBS})\s+{_SUBJECT})"
    rf"{_ADDRESSEE}\s*[.!?]?\s*$",
    re.IGNORECASE,
)
# "he said, " at the start
_TAG_LEADING = re.compile(
    rf"^\s*(?:{_SUBJECT}\s+(?:{SPEECH_VERBS})|(?:{SPEECH_VERBS})\s+{_SUBJECT})\s*[,:]\s*",
    re.IGNORECASE,
)

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def strip_dialogue_tags(text: str) -> str:
    """Remove leading/trailing English dialogue tags, repeatedly.

    Never returns empty: a segment that is *only* a tag (``he said.``) comes back
    unchanged, so callers always have something to measure.
    """
    current = text.strip()
    for _ in range(4):  # bounded; tags don't nest deeply
        before = current
        current = _TAG_TRAILING.sub("", current).strip()
        current = _TAG_LEADING.sub("", current).strip()
        current = current.strip(" ,;:")
        if current == before:
            break
    return current or text.strip()


def word_count(text: str) -> int:
    """Number of alphabetic words, ignoring digits and punctuation."""
    return len(WORD.findall(text))
