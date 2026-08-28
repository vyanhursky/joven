# Changelog

## v1.0.0b2 — 2026-08-28

### Fixed

- **Books using HTML named entities no longer fail to parse.** XML defines five
  entity names; XHTML in the wild uses the full HTML set, so any book containing
  `&nbsp;`, `&mdash;` or `&rsquo;` was refused with `Entity 'nbsp' not defined`
  and a traceback. The Knopf edition of *The Crossing* happens to use none, which
  is why one book was enough to hide this. Named entities are now resolved before
  parsing, on both sides of the text-preservation comparison so the invariant is
  unaffected.
- **Font obfuscation is no longer misreported as DRM.** `META-INF/encryption.xml`
  is how the IDPF and Adobe font-obfuscation schemes declare themselves as well as
  how real DRM does, and unencumbered trade EPUBs carry it routinely. Refusing on
  the file's presence rejected those books with advice to strip DRM that was never
  applied. The check now reads the encryption algorithm and refuses only genuine
  encryption, naming the resources it cannot read.
- An unparseable document reports an error and exits, instead of raising a
  traceback out of `inspect`, `detect` or `add`.

### Added

- **Releases publish to PyPI from a tag**, via Trusted Publishing — no API token
  in repository secrets. The install path becomes `uv tool install
  joven-ebook-annotator` instead of a venv and an editable checkout. A guard fails
  the build when a tag disagrees with the version in `pyproject.toml`, because
  PyPI will not let a version number be reused. See
  [docs/releasing.md](docs/releasing.md).
- **`joven detect --resume TRACE`.** A full run is 73 minutes and the sidecar was
  written only at the end, so an interruption at minute 70 lost all of it. The
  trace is now flushed per record and can be replayed: every model answer it holds
  is reused and only unreached segments cost anything. Tier 1 and the suppression
  gates still run over the whole book, so a resumed run reflects the current code;
  recorded errors are retried rather than inherited.

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
| integrity checks | 12 of 12 |

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

### Known limitations

- One book, one language pair, one reader. Tuned for McCarthy's border Spanish.
- Scan damage in the source is translated faithfully: `Está fibre.` → "It's
  fibre." for `Está libre.` Run `tools/scan_damage.py` on a candidate first.
- Only the first two note documents in the spine render a Kobo popup preview; the
  rest navigate to the note with a back-link. Accepted, not solved.
- Regionalisms with no clean English equivalent (`ejido`, `hacendado`) are often
  left untranslated — 10 of 726 footnotes. A glossary would suit them better than
  a footnote.
- Local models only. There is no hosted-API backend, by design: the pipeline is
  offline end to end, and a run costs nothing.
