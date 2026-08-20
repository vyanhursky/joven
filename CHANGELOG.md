# Changelog

## v1.0.0b1 — 2026-08-19

First public release. Beta because it has been proven on exactly one book by
exactly one person: everything below is measured on Cormac McCarthy's *The
Crossing*, and the prompts, dialogue-tag vocabulary and Spanish honorifics are
tuned to McCarthy's border Spanish. Whether it generalises is untested.

### What it does

Finds untranslated Spanish in an English-language EPUB and inserts tappable
translation footnotes, leaving the prose byte-for-byte unchanged. Runs entirely
offline against a local model — no API key, no per-book cost.

Verified on the Knopf 1994 edition, 151,865 words:

| | |
|---|---|
| segments considered | 12,302 |
| escalated to the LLM | 2,556 (21%) |
| footnotes produced | 726 |
| wall clock / cost | 73 minutes / $0 |
| integrity checks | 11 of 11 |

### Findings worth reading the source for

**Detector confidence does not predict translation errors.** The review pass was
built to sort worst-confidence-first. Sampling every confidence band showed
quality was uniformly good — confidence measures how hard the *language call* was,
not whether the translation is right. What does predict errors is a word the model
could not translate surviving verbatim into the English, so the review now sorts
by that instead (`suspicion.py`).

**A dialogue tag corrupted three separate measurements.** McCarthy's `, he said.`
survives translation unchanged, so it is shared text on both sides. It inflated a
similarity veto (suppressing 35 genuinely Spanish segments), distorted a
loanword gate, and flagged 20% of the book as suspicious. All three now strip
tags first, from one shared vocabulary in `dialogue.py`.

**The model silently repairs the source's OCR errors.** Asked for the Spanish it
translated, it returns corrected text — `to` → `todo`, `and` → `y`. Words that
*are* the Spanish then counted as English outside the span, suppressing real
footnotes. Sizing the span first makes the whole class harmless.

**Kobo footnote popups have undocumented constraints.** A `display:none` note
target has no position to navigate to; an adjacent note prevents the popup
entirely; one note per file is required for a single-note preview. Ten markup
recipes were tested on hardware to find this (DESIGN.md §6.6b).

**Prompt-cache minimums are not monotonic across models** — 512 tokens on Opus 5,
1024 on Sonnet 5, 4096 on Haiku 4.5 — and below them the API silently does not
cache. That makes Haiku *more* expensive than Sonnet for this workload despite a
third the per-token price, and makes a *larger* prompt the cheaper one.

### Known limitations

- One book, one language pair, one reader. Tuned for McCarthy's border Spanish.
- Scan damage in the source is translated faithfully: `Está fibre.` → "It's
  fibre." for `Está libre.` Run `tools/scan_damage.py` on a candidate first.
- Only the first two note documents in the spine render a Kobo popup preview; the
  rest navigate to the note with a back-link. Accepted, not solved.
- Regionalisms with no clean English equivalent (`ejido`, `hacendado`) are often
  left untranslated — 10 of 726 footnotes. A glossary would suit them better than
  a footnote.
- `ClaudeTranslator` is implemented and tested but unused: a Claude Pro
  subscription does not include API access.
