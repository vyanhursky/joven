# Changelog

## v1.0.0b3 — 2026-08-28

Everything here was found by running the tool against two books it had never
seen — *All the Pretty Horses* and *Suttree* — which is the first time it has been
asked to work on anything but the novel it was written for.

### Fixed

- **The EPUB 2→3 upgrade no longer breaks a valid NCX.** `read_package` took the
  *first* `dc:identifier` and ignored `package/@unique-identifier`, the attribute
  whose only job is to say which identifier is the book's identity.
  `sync_ncx_identifier` then wrote that wrong value into the legacy NCX — so *All
  the Pretty Horses*, whose OPF lists an ISBN first and its real identity second,
  came out with an epubcheck `NCX-001` error it did not have going in. Books with a
  single `dc:identifier` (*The Crossing*, *Suttree*) cannot show the difference,
  which is why one book was not enough to catch it.

- **Latin is no longer annotated as Spanish.** A two-language detector cannot answer
  "neither": asked about Latin it must choose, and it does not choose English.
  *Suttree* — a novel with no Spanish in it — produced `Stabat Mater Dolorosa.` at
  SPANISH 0.94, above the accept threshold, which skips adjudication entirely and
  becomes a confident, wrong footnote.

  The obvious fix is wrong. Adding Latin to the detector dilutes the distribution the
  thresholds are tuned against: `Dieciseis.` drops from 1.00 to 0.54 and stops being
  accepted, and it is a *documented* Tier-1 strength that the LLM gets wrong. So the
  primary detector is untouched, and Latin is asked about separately as a veto on the
  accept path — the one place where being wrong is unrecoverable. **The dialogue tag
  is stripped before the test**, which is what makes it safe: `Respóndele, he said.`
  ranks Latin 0.64 / Spanish 0.34 with the attribution attached and 0.00 / 1.00
  without it. That is the same tag distorting a third measurement, for the same
  reason it distorted the other two.

  Measured over 1,094 known-Spanish passages from two books: **zero** wrongly vetoed.

- **The model is told that Latin is not Spanish.** The veto guards only the accept
  path. Two of *Suttree*'s three Latin passages escalated instead, and the model,
  asked directly, agreed they were Spanish and translated them. The prompt and its
  few-shot examples now cover this.

- **`--resume` checks the Tier-1 verdict before reusing an answer.** Tier 1's two
  outcomes call different prompts — an accept gets `translate` ("this is Spanish,
  render it"), the band gets `adjudicate` ("is this Spanish at all?") — and a
  translate-only answer always says yes. Resuming across a Tier-1 change would
  therefore hand a "yes" to a segment the new code wants adjudicated, quietly
  reinstating the footnote the change existed to prevent. The Latin veto is exactly
  such a change.

### Documentation

- The install guide covers `uv` itself and the `~/.local/bin` PATH step. Following
  the previous instructions on a clean machine got you `joven: command not found`.
- Results now cover three books, including the control run and the precision figure
  it produced.

### Measured

Re-running the *Suttree* control after these fixes: **six false positives down to
two**, in 177,257 words. One Latin passage was stopped by the Tier-1 veto, two by
the prompt, and `Vag.` went with them. Escalation rate and wall clock are unchanged
(2,548 calls, 63 minutes), so nothing was traded for it.

### Still open

- `No suh.` → "No sir." — dialect English that the similarity veto misses at a 0.67
  ratio, just under the 0.75 threshold.
- `Ay.` — genuinely ambiguous out of context; English here, Spanish elsewhere.

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
