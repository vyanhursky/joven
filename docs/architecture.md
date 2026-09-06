# Architecture

How Joven is built and why each part is shaped the way it is. This is the
current design, not its history: every rule here is one the code enforces today,
with the measurement that justifies it where a number is doing the work.

---

## The shape

```
book.epub ─► extract text units ─► segment into sentences ─► TIER 1 (lingua)
                                                                │
                                   confident English ◄──────────┼──────────► confident Spanish
                                   (dropped, traced)            │            (translate only)
                                                                ▼
                                                      abstention band ─► TIER 2 (local LLM)
                                                                                │
                                                        gates: similarity veto · loanword · narrowing
                                                                                │
                                                                                ▼
                                          annotations.json ◄── review / add / reject ── the human
                                                 │
                                                 ▼
                              render: original EPUB + sidecar ─► book.annotated.epub ─► kepubify
                                                                        │
                                                                     verify (12 checks)
```

Every segment, annotated or not, writes one record to `trace.jsonl`, so any
missing footnote can be explained afterwards with `joven explain`.

## The sidecar is the source of truth

The EPUB is never edited in place. `annotations.json` holds every decision, and
rendering is a pure, idempotent function of (original EPUB + sidecar). Corrections
mean editing the sidecar and re-rendering, never re-translating; a run costs an
hour, a render costs seconds.

**Ids are content hashes, not positions.** An annotation's id is
`sha256(href + normalised paragraph text + occurrence)[:12]`. Positional indices
scramble the moment detection settings change, destroying every manual fix.
`occurrence` numbers repeated paragraphs within a document, because McCarthy
repeats short dialogue relentlessly — *The Crossing* has 245 paragraphs whose text
matches another in the same file — and without it those collapse to one id and
most footnotes silently vanish.

**Human edits are sticky.** Re-detection replaces `auto` entries only.
`approved` and `edited` are never touched, and a `rejected` id is never re-added
however confident the detector is. Manual work accumulates monotonically, so
re-running is always safe.

## Tier 1: statistical triage with an abstention band

`lingua`, restricted to English and Spanish, over each sentence. Its confident
calls are excellent in both directions and its false positives are never
confident: no true-English sentence has scored above 0.58 as Spanish across four
books. That property makes it a safe pre-filter — accept the confident tail on
each side, escalate the middle — and useless as a classifier on its own, because
about three quarters of the Spanish candidates land in the middle.

| Setting | Value | Why it sits there |
|---|---|---|
| `accept_spanish` | 0.90 | The accept path skips adjudication, so a wrong call here becomes a footnote nothing downstream can catch. At book scale genuine English sits well inside 0.70–0.90 (`Yessir.` 0.70–0.75, `Astrolabe or sextant.` 0.75–0.80), so it cannot come down. |
| `reject_english` | 0.90 | A confident-English drop never reaches the model; the margin is symmetric. |
| `min_words_to_accept` | 3 | Below three words a high score needs 0.99 (`Sí.`, `Dieciseis.`) or it escalates. |
| `accept_spanish_stripped` | 0.95 | A tag-stripped fragment has less signal; `A Casas Grandes` scores 0.9491, a bare place name that should escalate, so this floor cannot come down either. |

**The dialogue tag.** McCarthy writes unquoted speech with an attribution —
`Tantos, said the man.` — and the English tag drags a short Spanish utterance to
a coin-flip score (0.50; bare `Tantos` scores 0.98). Stripping trailing and
leading tags from a closed vocabulary of speech verbs (`dialogue.py`) rescues
those into the confident band and roughly halves the model's workload. The same
tag distorts three other measurements — the similarity veto, the loanword gate
and the Latin veto — which is why all of them strip it first from one shared
vocabulary.

**The Latin veto.** A two-language detector cannot answer "neither": liturgical
Latin scores as Spanish, sometimes above the accept threshold. Adding Latin to the
detector is the wrong fix — it dilutes the distribution the thresholds are tuned
against (`Dieciseis.` drops from 1.00 to 0.54) — so Latin is asked about
separately, as a ranking test on the tag-stripped text, only on the accept path
where being wrong is unrecoverable. Over 1,094 known-Spanish passages it wrongly
vetoed none.

## Tier 2: a local instruct model, two prompts, three gates

The task is not translation. It is three questions at once — is this Spanish,
which part of it, and what does it mean — which is why the backend is an instruct
model and not a translation engine; an NMT engine would cheerfully "translate"
`Yes mam.` The model gets up to 400 characters of preceding prose, because
`Bastante.` means nothing alone, fenced and marked do-not-translate because it will
otherwise answer about the context.

**Two prompts.** The abstention band gets *adjudicate*: is this Spanish, and if so
which part? A Tier-1 accept gets *translate only*: this is Spanish, do not
second-guess it. A single combined prompt let the model veto Tier 1's correct
calls (it labels `Dieciseis.` English); skipping the model for accepts shipped
annotations with no translation at all — blank popups on the device.

**Three deterministic gates after the model**, each traced as its own outcome:

1. **Similarity veto.** If the "translation" is the source tidied up
   (`Yessir.` → `Yes sir.`, ratio 0.92), the footnote teaches nothing. Ratio ≥ 0.75
   after stripping dialogue tags from both sides. Genuine translations sit at
   0.60–0.70 with the tag stripped, so the threshold has little room either way.
2. **Embedded loanword.** A lone Spanish word inside English narration
   (`You know what is orgullo?`) gets no footnote; McCarthy uses them as texture.
   The check that matters runs first: a span of two or more words is never a
   loanword, however much English surrounds it — which also makes the model's
   habit of silently repairing OCR errors in the span it reports harmless.
3. **Span narrowing.** The annotation shrinks to the `spanish_text` the model
   reported, so the marker lands after the Spanish and before the English tag.
   On any mismatch it falls back to the whole segment: a valid marker beats a
   well-placed one.

Replies are constrained to a JSON schema, `temperature` is 0, and thinking is
switched off, so a re-run is reproducible and a trace can be treated as evidence.
The Ollama and OpenAI-compatible backends share the prompt, the few-shot examples
and the context fence through one builder, so they cannot drift.

**One paragraph is the unit of work.** The pipeline scans a paragraph — both
tiers, every gate — and returns its decisions; the caller finishes paragraphs
strictly in book order whether they were scanned one at a time or on a pool of
workers. The trace, the sidecar and `--resume` are therefore identical at any
worker count.

## Rendering: change only what must change

**Byte-level zip surgery**, not an EPUB library. Every archive entry is copied
verbatim and only the XHTML documents that gained annotations are re-serialised,
with the original XML declaration style preserved. `ebooklib` normalises and
reflows markup it was not asked to touch, which would violate the one promise the
tool makes.

**The text-preservation invariant.** Strip every inserted node from the output
and the text of every document must be byte-identical to the original. Insertion
only ever *splits* a text node — `"abc"` becomes `"a" + marker + "bc"` — so the
property holds by construction, and `joven verify` proves it on every render.
`joven strip` is the same operation in reverse: rejoin the split text nodes,
drop the note documents, cut the appended CSS.

**One footnote per paragraph.** Detection is per sentence, where the confidence
signal lives, but contiguous Spanish sentences in a paragraph merge into one
annotation with one marker, so a three-sentence utterance does not carry three
asterisks.

**EPUB 2 → 3.** Popup footnotes need `epub:type="noteref"` and `"footnote"`,
so an EPUB 2 package is upgraded in an isolated transform: version, a generated
nav document from the NCX, `dcterms:modified`, the `epub` namespace on touched
documents. The NCX stays, because Kobo reads it. The book's identity is the
`dc:identifier` named by `@unique-identifier`, never the first one listed; the
wrong choice writes a mismatched `dtb:uid` into a correct NCX.

**What Kobo actually does with footnotes**, measured on hardware (firmware
4.45.23697) across ten markup recipes, because almost every inference from the
spec was wrong:

| Finding | Consequence in the renderer |
|---|---|
| A `display:none` note has no layout box; tapping its marker jumps to the start of the book | Notes stay visible in the flow, styled small |
| A note adjacent to its paragraph renders inline, no popup | Notes are never placed next to the paragraph |
| The preview does not stop at the target element; siblings after it are shown too | **One XHTML file per note**, in the manifest and the spine with `linear="no"` |
| Only the first two note documents in the spine preview; the rest navigate to the note and offer a back link | Accepted: a jump is a worse experience than a preview, not a bug |
| A source already through `kepubify` has every sentence in a `koboSpan`; a marker inside one loses the sentence's text on the device | Kobo markup is stripped first and re-applied by `kepubify` at the end |

Apple Books renders the same markup as popups throughout. The KEPUB is what goes
on the device; the spec-clean EPUB 3 is what `epubcheck` validates, and the
reader-facing checks run again on the KEPUB because `kepubify` rewrites the XHTML.

## Review: suspect first, in book order after that

Detector confidence does not predict translation errors — it measures how hard
the language call was, which is a property of utterance length and dialogue
tags, and a three-word line welded to `, he said.` scores 0.50 and translates
perfectly. What does predict a bad footnote is a word the model could not
translate surviving verbatim into the English, which is what scan damage
produces (`Está fibre.` → "It's fibre.") and what genuine regionalisms produce
(`ejido`, `hacendado`). `suspicion.py` flags exactly that, plus digits welded
into words, and the review page sorts those first with the reason badged;
everything else follows in narrative order, where a translation that is fine
alone but wrong for the scene is easiest to notice.

Every decision writes straight to the sidecar the moment it is made.

## What is deliberately not done

- **No hosted-model backend.** An OpenAI-compatible *protocol* is supported so
  that llama.cpp, LM Studio and vLLM work, but the design is offline end to end:
  no API key, no per-book cost, and the book never leaves the machine.
- **No lowering of the accept threshold** to save model calls. The wide band is
  what keeps `Yessir.` from becoming a footnote.
- **No third language in the detector.** See the Latin veto.
- **No chasing of the two-note preview limit.** It is an undocumented cache
  heuristic, and the fallback navigates correctly.
- **No Homebrew formula.** `lingua` ships no source distribution;
  see [releasing.md](releasing.md).
