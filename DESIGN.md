# Joven — Design & Implementation Plan

Insert clickable translation footnotes for foreign-language passages in an EPUB.
v1 scope: **EPUB input, Spanish→English, Cormac McCarthy's *The Crossing*.**

## Status

| Milestone | State |
|---|---|
| M0 — Skeleton, CLI, `joven inspect` | ✅ done |
| M1 — Lossless round-trip + `epubcheck` + text invariant | ✅ done, green on the real book |
| M2b — Local model selection | ✅ done → [`docs/model-selection.md`](docs/model-selection.md) |
| M2 — Detection & triage (Tier 1) | ✅ segmentation + triage over the full book |
| M3 — Rendering + EPUB 2→3 + KEPUB | ✅ **done and device-verified** — see §6.6b |
| M4 — Local LLM adjudication at book scale | ✅ **full book run** — 12,302 segments, 2,556 LLM calls, 726 footnotes, 73 min, $0 |
| **Decision trace** (`--trace` / `joven explain`) | ✅ added — see §6.7 |
| M5 — Review & corrections | ✅ `joven review` (local UI) + `joven add`; round-trip verified |
| M6 — Read the book | ✅ annotated KEPUB on the device, 11 of 11 integrity checks green |

The first full-book run paid for itself by exposing two false-suppression bugs
that no synthetic case had caught — both traced to the same root cause, and both
now fixed and pinned by tests drawn from the real corpus (§8).

**364 tests passing, ruff clean.** Verified on the real book: lossless
round-trip, text-preservation invariant, `epubcheck` clean **as EPUB 3**,
noteref→footnote integrity, and KEPUB conversion preserving all 623,144
characters of prose with all `epub:type` markup intact.

Verified on the physical Kobo (firmware `4.45.23697`): footnote rendering across
ten markup recipes (§6.6b) and the `KoboReader.sqlite` schema (§6.6).

---

## 1. What the actual book looks like

Before designing anything I unpacked the target file and measured it. This drove
every decision below.

```
[Border Trilogy 02 1] McCarthy, Cormac - The Crossing ... libgen.li.epub   324 KB
├── mimetype                     application/epub+zip
├── META-INF/container.xml
├── META-INF/calibre_bookmarks.txt      ← Calibre artifact, not spec
├── content.opf                  EPUB **2.0**, dc:language = "en"
├── toc.ncx
├── stylesheet1.css
├── cover1.jpeg
├── titlepage.xhtml
└── OEBPS/  title, part1, part2_split_000, part2_split_001, part3, part4, part5
```

**The markup is unusually clean, which is very good news:**

| Property | Value |
|---|---|
| Non-empty `<p>` elements | 4,465 (100% of content — zero `<i>`, `<em>`, `<span>`, `<a>`) |
| Existing `lang` attributes | **0** — Spanish is not marked up in any way |
| Existing footnotes/notes | none |
| Total words | ~151,000 |
| EPUB version | 2.0 (Calibre 2.22 output) |
| DRM | none |

Every paragraph is a flat `<p class="calibre4">…text…</p>`. No nested inline
elements means text-node surgery is straightforward and low-risk.

### 1.1 The four mixing patterns

This is the crux of the whole project. Spanish appears in **four** distinct forms:

```xhtml
<!-- A. Whole-paragraph Spanish (easy) -->
<p class="calibre4">Vaya con Dios.</p>
<p class="calibre4">Se fué.</p>

<!-- B. Spanish sentence + English dialogue tag (the common case) -->
<p class="calibre4">Cuántos años tienes? the old man said.</p>
<p class="calibre4">Lugares donde el fierro ya está en la tierra, the old man said.
   Lugares donde ha quemado el fuego.</p>

<!-- C. English prose containing a Spanish loanword (must NOT translate) -->
<p class="calibre4">The matríz will not help you, the old man said. He said that
   the boy should find that place where acts of God and those of man are of a piece.</p>

<!-- D. Spanish opener, then English narration (must translate only the opener) -->
<p class="calibre4">Escúchame, joven, the old man wheezed. If you could breathe a
   breath so strong you could blow out the wolf. Like you blow out the copo.</p>
```

Cases **C** and **D** are why naive per-paragraph language detection fails in
*both* directions: C is a false positive waiting to happen, D is a guaranteed
miss.

---

## 2. Measured findings — the detection problem is the hard part

I ran `lingua` (a strong statistical language-ID library) over the whole book,
segmented into sentences, restricted to EN/ES only.

### 2.1 Confident detections are excellent

| Text | Verdict | Confidence |
|---|---|---|
| `Y cómo se encuentra?` | SPANISH | 1.00 |
| `Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.` | SPANISH | 0.99 |
| `Lugares donde el fierro ya está en la tierra, the old man said.` | SPANISH | 1.00 |
| `Ay. Ándale, joven. Ándale pues.` | SPANISH | 0.89 |
| `He turned and stood.` | ENGLISH | 0.99 |

### 2.2 …but confident detections are the minority

```
confident Spanish sentences (≥0.90 conf, ≥3 words):  313   (1,657 words)
ambiguous / Spanish-leaning (needs adjudication):     887
```

**~74% of Spanish candidates land in a band where statistical language ID is a
coin flip.** And that band contains *both* classes, interleaved:

| Sentence | Conf | Truth |
|---|---|---|
| `Tantos, said the man.` | 0.50 | **Spanish** |
| `I dont intend to.` | 0.50 | English |
| `Tan horrible.` | 0.50 | **Spanish** |
| `Go on.` | 0.51 | English |
| `Adelante, he cried.` | 0.51 | **Spanish** |
| `Güero, he said.` | 0.51 | **Spanish** |
| `Yes mam.` | 0.58 | English |
| `Bastante, the doctor said.` | 0.51 | **Spanish** |
| `Sí, said the Mexican.` | 0.51 | **Spanish** |

**Root cause:** McCarthy's Spanish utterances are frequently 1–3 content words
(`Sí`, `Tantos`, `Adelante`, `Bastante`, `Güero`) attached to an English dialogue
tag (`he said`, `said the Mexican`). The English tag dilutes the signal to
roughly 50/50.

### 2.3 The one genuinely good property

I checked whether any *true English* sentence scores high Spanish confidence.
**None did.** Every false positive sits at 0.50–0.58 — the detector is
*abstaining*, not confidently wrong.

> **This is the key architectural finding.** The cheap detector is a safe,
> high-recall pre-filter. Everything in the abstention band gets escalated. It is
> not a classifier on its own, and shipping it as one would produce a mangled book.

### 2.4 Attempted rescue: strip the dialogue tag first

Stripping trailing `, he said.` / `said the man.` before classifying helps
dramatically **when the strip succeeds**:

```
Tantos, said the man.         → "Tantos"          SPANISH 0.98   (was 0.50)
Sí, said the Mexican.         → "Sí"              SPANISH 1.00   (was 0.51)
A Casas Grandes, said Billy.  → "A Casas Grandes" SPANISH 0.95   (was 0.50)
```

But it does not rescue short English (`Go on` → 0.51, `Yes mam` → 0.58 still
Spanish-leaning), and tag-stripping is itself a regex arms race against
McCarthy's prose. **Verdict: use it as a confidence booster to shrink the
escalation band, never as the decision mechanism.**

### 2.5 What about `detect_multiple_languages_of`?

lingua's built-in mixed-text span API is too jittery to build on:

```
"Escúchame, joven, the old man wheezed. If you could…"
  → [SPANISH] 'Escúchame, joven, '  [ENGLISH] 'the old man wheezed. If you…'   ✓ correct

"Y por eso soy hereje, he said. Por eso y nada más."
  → [SPANISH] 'Y por eso soy '  [ENGLISH] 'hereje, he said. '  [SPANISH] 'Por eso…'  ✗ splits mid-clause,
                                                                    labels a Spanish word English

"Cuántos años tienes? the old man said."
  → [SPANISH] (entire string)   ← no split at all
```

Do our own deterministic sentence segmentation instead.

---

## 3. Recommended architecture

```
                    ┌──────────────────────────────────────────┐
  book.epub ───────▶│ 1. UNPACK   zipfile → in-memory tree     │
                    │    integrity + DRM + version checks      │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │ 2. EXTRACT  lxml → addressable text units │
                    │    (spine file, element path, char span)  │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │ 3. SEGMENT  paragraph → sentences/clauses │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │ 4. TRIAGE   lingua + heuristics          │
                    │    ┌─ ES ≥0.90 ─────▶ candidate          │
                    │    ├─ band 0.35–0.90 ▶ ESCALATE          │
                    │    └─ EN ≥0.90 ──────▶ drop              │
                    └────────────────┬─────────────────────────┘
                                     ▼
                    ┌──────────────────────────────────────────┐
                    │ 5. ADJUDICATE + TRANSLATE  (LLM, batched) │
                    │    with surrounding English context       │
                    │    → is_spanish? + span + translation     │
                    └────────────────┬─────────────────────────┘
                                     ▼
              ╔══════════════════════════════════════════════════╗
              ║  annotations.json   ← THE DURABLE ARTIFACT       ║
              ║  stable content-hash IDs, status, human edits    ║
              ╚════════════════┬═══════════════════╤═════════════╝
                               ▼                   ▼
              ┌────────────────────────┐  ┌──────────────────────┐
              │ 6. RENDER (idempotent) │  │  REVIEW UI / report  │
              │    → annotated .epub   │  │  (localhost or HTML) │
              └────────────────────────┘  └──────────────────────┘
```

### 3.1 The single most important design decision

**The EPUB is never edited in place. `annotations.json` is the source of truth,
and rendering is a pure, idempotent function of (original epub + sidecar).**

Consequences — all of them good:
- Corrections = edit the sidecar, re-render. Never re-translate.
- Re-running detection with better settings merges into existing human edits
  instead of clobbering them.
- The expensive step (LLM) is cached and separable from the cheap step (render).
- You can produce multiple output variants (popup footnotes / inline brackets /
  no annotations) from one sidecar.
- The original file is never at risk.

---

## 4. Decisions & recommendations

### 4.1 Language & libraries → **Python 3.13**

Already on the machine (3.13.0, arm64, 16 GB). Wins on the two things that matter:
best language-ID ecosystem and best LLM/translation clients.

| Need | Choice | Why |
|---|---|---|
| EPUB read/write | **`zipfile` + `lxml` directly** | See warning below |
| Language ID | **`lingua-language-detector`** | Validated above; gives calibrated confidence values, which the whole triage design depends on. `langdetect` does not. |
| Sentence segmentation | **`pysbd`** (or hand-rolled) | Must not split on `Sr.`, `Sra.`, ellipses |
| LLM (default) | **Ollama** via HTTP (`httpx`) | Local, free, offline. No SDK needed — the REST API is two endpoints. |
| LLM (opt-in, future) | **`anthropic`** SDK | Wired + contract-tested, not called by default |
| KEPUB conversion | **`kepubify`** (`brew`) | Standalone binary; no Calibre dependency |
| CLI | **`typer`** | Subcommands, good `--help` |
| Validation | **`epubcheck`** (`brew`) | External gate; Java present at `/usr/bin/java` |
| Tests | **`pytest`** | |

**Installed and verified on this machine:** `kepubify` 4.0.4, `ollama` 0.32.9,
`epubcheck` 5.3.0 (all via Homebrew), Python 3.13.0.

> ⚠️ **Do not use `ebooklib`.** It normalizes and re-serializes XHTML, which
> silently reflows markup you did not intend to touch. For a tool whose core
> promise is "change only what must change", do byte-level zip surgery: copy every
> archive entry verbatim, re-serialize *only* the XHTML documents that actually
> gained annotations.

**EPUB repacking gotchas to encode as tests from day one:**
1. `mimetype` must be the **first** entry and **STORED** (uncompressed), no extra field.
2. Preserve entry order and per-entry compression for everything else.
3. Don't reformat/prettify untouched XHTML.
4. Preserve the XML declaration style (`<?xml version='1.0' encoding='utf-8'?>` — note single quotes here).

### 4.2 Form factor → **CLI first, with a local review UI**

Recommend a Unix/macOS CLI. This is a batch file transformation you run once per
book; a web app adds upload, storage, and hosting for zero benefit, and shipping
a copyrighted book to a server is exactly what you don't want.

**But** the correction workflow genuinely wants a UI. So:

```bash
joven inspect  book.epub                    # report: structure, version, DRM, stats
joven detect   book.epub -o annotations.json    # triage + LLM, writes sidecar
joven review   annotations.json                 # localhost review UI (or --html report)
joven render   book.epub annotations.json -o book.annotated.epub
joven verify   book.annotated.epub              # epubcheck + text-preservation proof
```

`detect` and `render` are separate commands on purpose — that separation *is* the
correction workflow.

### 4.3 Translation backend → **local LLM via Ollama** ✅ decided (offline-first)

The task is **not** pure translation. It is simultaneously:
1. **detection** — is this fragment Spanish at all? (the 887-sentence problem)
2. **span identification** — which part of the sentence is Spanish? (case B/D)
3. **translation** — of short, context-dependent, 1930s Mexican-border dialect

That combination is why an *instruct LLM* is the right shape of tool, and why a
pure NMT engine is not — see the comparison table below. The model gets
**surrounding English context**, which is decisive for fragments like `Tantos.` or
`Bastante.` that are meaningless in isolation, and it copes with McCarthy's
nonstandard orthography (`Se fué`, `Dieciseis`, `matríz`).

**Decision: run this locally via Ollama. Zero marginal cost, no network, no API
key.** Model selection was settled empirically — see
[`docs/model-selection.md`](docs/model-selection.md).

**Chosen model: `qwen3:8b`** (5.2 GB) — 27/27 on the two-tier benchmark, 100% span
precision, zero false positives, ~1.9 s/case. `gemma3:12b` ties on accuracy but is
3× slower; `aya-expanse:8b` is one case behind. Full-book pass ≈ 14 minutes, $0.

Hardware budget: 16 GB unified memory, arm64. Leave ~5 GB for the OS, so the
practical ceiling is a **~8–10 GB model** — i.e. 7–14B at 4-bit quantization.

**Measured: the two tiers have complementary failure modes.** Standalone, the 8B
models score 89%; inside the pipeline they score 96–100% with *zero* false
positives, because every LLM error in the benchmark is caught by Tier 1 —
`Dieciseis.` (LLM misses, lingua 1.00) and the loanword paragraphs (LLM
false-positives, lingua ENGLISH 0.99). That result is the empirical justification
for this architecture, not just a nice-to-have.

Tag-stripping also cut the escalation rate from the predicted 74% to a **measured
37%**, roughly halving LLM work.

**Backend comparison:**

| Option | Detection | Span | Context | Cost | Verdict |
|---|---|---|---|---|---|
| **Local LLM (Ollama, 7–14B Q4)** | ✅ | ✅ | ✅ | **$0** | ✅ **Default.** Weaker than frontier models on literary idiom, but the pipeline's escalation design means we only ask it about pre-filtered candidates, and the review step (§6.4) catches its misses. |
| Argos Translate / OPUS-MT | ❌ | ❌ | ❌ | $0 | Pure NMT — translates whatever you hand it. No detection, no span selection, no context window. Would cheerfully "translate" `Yes mam.` Useful only as a *translation-only* backend behind a separate detector. Keep as a comparison point. |
| Claude API | ✅ | ✅ | ✅ | ~$0.32–$1.61/book | **Deferred, kept as a protocol implementation.** Best accuracy, but not worth spending on at this stage. Wire the interface, don't call it. |
| Google Translate API | ❌ | ❌ | ❌ | $ + GCP setup | Rejected. Same NMT limitations as Argos, plus billing setup and shipping the book text to Google. |

**Design for this** — a `Translator` protocol so the backend is a one-flag swap:

```python
class Translator(Protocol):
    def adjudicate(self, batch: list[Candidate]) -> list[Verdict]: ...

# implementations
StubTranslator     # deterministic, offline, used by the entire test suite
OllamaTranslator   # DEFAULT — local, free
ClaudeTranslator   # implemented + contract-tested, opt-in via --backend claude
ArgosTranslator    # optional, translation-only comparison baseline
```

```bash
joven detect book.epub                      # local model, free
joven detect book.epub --backend claude     # future, opt-in only
```

Because the sidecar is the durable artifact (§3.1), you can start with the local
model, read the book, and later re-run only the low-confidence subset through a
better backend without redoing anything — `rejected`/`edited` stay sticky.

### 4.4 Annotation mechanism → **EPUB 3 popup footnotes, targeting Kobo** ✅ decided

Target: **Kobo, popup footnotes, one footnote per paragraph.**

The book is EPUB 2.0, so the renderer must upgrade the package to EPUB 3 to get
popup semantics:

```xhtml
<!-- Marker: anchored at the END of the Spanish span, before the English tag -->
<p class="calibre4">Cuántos años tienes?<a epub:type="noteref"
   href="#joven-a3f9c21b" id="joven-ref-a3f9c21b" class="joven-note">*</a> the old man said.</p>

<!-- Note body: aside is hidden from the reading flow by conforming readers -->
<aside epub:type="footnote" id="joven-a3f9c21b" class="joven-footnote">
  <p>How old are you?</p>
</aside>
```

Package changes required (isolated, tested transform):

| File | Change |
|---|---|
| `content.opf` | `version="2.0"` → `"3.0"`; add `<meta property="dcterms:modified">`; add the nav item |
| new `nav.xhtml` | EPUB 3 nav document (`epub:type="toc"`), generated from the existing `toc.ncx` |
| `toc.ncx` | **Keep it.** EPUB 3 permits it for backward compatibility, and Kobo reads it. |
| each annotated XHTML | add `xmlns:epub="http://www.idpf.org/2007/ops"` to `<html>` |
| `stylesheet1.css` | append `.joven-note` / `.joven-footnote` rules (superscript marker; `aside` display rules) |

**Output format → KEPUB is the default** ✅ decided

KEPUB is Kobo's own flavour (what the Kobo store ships) and renders better on
device. Pipeline:

```
book.epub ──▶ [annotate] ──▶ book.annotated.epub ──▶ [kepubify] ──▶ book.kepub.epub
                                     │                                    │
                              epubcheck gate                     what goes on the Kobo
```

- **Tool: `kepubify`** (`brew install kepubify`, by pgaskin) — a standalone binary,
  no Calibre or plugin dependency. Vastly simpler than the Calibre KePub Output
  plugin route.
- **Conversion is the last step, after validation.** `epubcheck` validates EPUB, not
  KEPUB, so we must validate the intermediate EPUB 3 *before* kepubifying.
  `joven verify` therefore runs on the `.epub`; the `.kepub.epub` gets structural
  checks only.
- **Both artifacts are kept.** The EPUB 3 is the verifiable, spec-clean thing; the
  KEPUB is the device deliverable.
- **Filename matters:** Kobo needs the `.kepub.epub` double extension to recognise
  a sideloaded KEPUB. `kepubify` handles this; assert it in `verify`.

```bash
joven render book.epub annotations.json -o out/          # emits both .epub and .kepub.epub
joven render book.epub annotations.json --no-kepub       # EPUB 3 only (debugging)
```

**Other Kobo notes:**

- **Sideload over USB** — mount the device and copy the `.kepub.epub` in.
- **Verify popup behavior on your actual model/firmware in M3, as KEPUB.** Kobo
  supports EPUB 3 `noteref`/`footnote` popups, but rendering has historically
  varied by firmware, and kepubify rewrites the XHTML (it wraps text in
  `<span class="koboSpan">` elements for its own pagination/highlighting). That
  rewrite is exactly the sort of thing that could disturb our markers, so the
  device test must run on the **KEPUB**, not the EPUB.
- **kepubify's span injection is also why the Kobo highlight round-trip (§6.5)
  needs verification** — KEPUB `ContentID`s and container paths differ from plain
  EPUB.
- **Fallback if popups disappoint:** in-page endnote links per chapter. Same
  sidecar, different renderer — no rework.

`aside` must be styled so non-conforming readers don't dump the note inline:

```css
aside.joven-footnote { display: none; }        /* conforming readers override for popup */
a.joven-note { vertical-align: super; font-size: 0.7em; text-decoration: none; }
```

**Build order within M3:** ship the trivial inline-bracket renderer *first*
(`Vaya con Dios. [Go with God.]`) purely to prove the insertion pipeline and the
text-preservation invariant end-to-end. Then build the real popup renderer. The
inline renderer stays permanently useful as a debug/diff view.

### 4.5 Annotation granularity → **one footnote per paragraph** ✅ decided

Detect per *sentence* (that's where the confidence signal lives), then **group
contiguous Spanish sentences within a single paragraph into one annotation**.

```
<p>Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.</p>
   └─ 3 sentences detected, 1 footnote, marker after "verdad."
```

Sidecar implication: an annotation carries a **list** of spans plus one merged
translation, not a single span:

```jsonc
{
  "id": "a3f9c21b7e04",
  "spans": [[0, 17], [27, 42], [43, 60]],   // Spanish runs; gap = the English "he said."
  "marker_offset": 60,                      // where the noteref goes
  "translation": "Listen to me, young man, he said. I know nothing. That is the truth."
}
```

Note the English dialogue tag inside the run: reproducing it in the translation
(rather than surgically excising it) reads better and is what a human translator
would do.

---

## 5. Integrity, correctness, and the invariant that matters

### 5.1 The text-preservation invariant

This is the highest-value test in the project. State it precisely:

> Strip all markup from the annotated EPUB and delete every inserted annotation
> node. The resulting text must be **byte-identical** to the same extraction from
> the original EPUB.

That single property proves the tool never mangled, dropped, duplicated, or
reordered McCarthy's prose. Assert it on the real book in CI (locally), not just
on fixtures.

### 5.2 Full integrity gate (`joven verify`)

| Check | Detects |
|---|---|
| Round-trip identity (0 annotations → structurally identical output) | Repacking bugs |
| Text-preservation invariant (above) | Insertion bugs |
| `epubcheck` clean | Spec violations, broken IDs, bad manifest |
| Every `noteref` resolves to exactly one `footnote` id | Dangling links |
| All ids unique across the spine | ID collisions |
| `mimetype` first + STORED | The classic epub repacking bug |
| Annotation count == sidecar `approved` count | Renderer drift |
| Rendered output byte-stable across two runs | Non-determinism |

### 5.3 Detection quality — a golden set, not vibes

Precision/recall on detection is the actual product risk, and it needs a
labelled fixture:

- Hand-label ~250 sentences drawn from this book, deliberately oversampling the
  0.35–0.90 confidence band where the real difficulty lives.
- Include the known adversarial cases: `Go on.` / `Yes mam.` / `I dont intend to.`
  (must be English) and `Tantos, said the man.` / `Güero, he said.` / `Sí, said
  the Mexican.` / `Tan horrible.` (must be Spanish), plus the loanword paragraphs
  (`matríz`, `copo`, `candela`) which must be **left alone**.
- Track **precision and recall separately** and fail the build on regression.
  Optimize for precision on the "leave English alone" side — a spurious footnote
  on `Go on.` is far more damaging to the reading experience than a missed one.

> ⚖️ **Copyright note for fixtures:** do not commit the EPUB. Commit only short
> labelled sentence fragments (fair-use scale) plus a small synthetic EPUB built
> in code. Gate the real-book test behind an env var pointing at your local copy.

### 5.4 Test suite (runs on build)

```
tests/
├── unit/
│   ├── test_segmentation.py     abbreviations, ellipses, McCarthy's no-quote dialogue
│   ├── test_triage.py           threshold behavior, band boundaries, tag-stripping
│   ├── test_insertion.py        text-node splitting, offsets, entity handling
│   └── test_ids.py              content-hash stability under re-detection
├── golden/
│   ├── test_detection_quality.py   labelled set → precision/recall thresholds
│   └── test_render_snapshot.py     synthetic epub → exact expected XHTML
├── integration/
│   ├── test_roundtrip.py        zero-annotation identity
│   ├── test_text_preserved.py   THE invariant
│   └── test_epubcheck.py        subprocess epubcheck, skip if absent
└── smoke/
    └── test_real_book.py        opt-in via JOVEN_TEST_EPUB env var
```

Hard rule: **no network in tests.** `StubTranslator` everywhere; the Claude path
gets contract tests against recorded fixtures.

---

## 6. The corrections workflow (your 80/100 problem)

Two failure classes need different answers.

### 6.1 Stable IDs — the prerequisite

Annotation IDs must be **content-derived, not positional**:

```
id = sha256(spine_href + "\x00" + normalized_source_text + "\x00" + occurrence_index)[:12]
```

Positional indices scramble the moment detection settings change, which would
destroy every manual fix. Content hashes survive re-runs.

### 6.2 Sidecar schema

```jsonc
{
  "source": { "sha256": "...", "title": "McCarthy, Cormac - The Crossing" },
  "annotations": [
    {
      "id": "a3f9c21b7e04",
      "file": "OEBPS/part2_split_000.xhtml",
      "para_index": 595,
      "source_text": "Escúchame, joven, he said. Yo no sé nada. Esto es la verdad.",
      "spanish_span": [0, 17],            // char offsets — excludes "he said."
      "translation": "Listen to me, young man, he said. I know nothing. That is the truth.",
      "detector_confidence": 0.99,
      "model": "claude-opus-5",
      "status": "auto"                    // auto | approved | edited | rejected
    }
  ]
}
```

### 6.3 Merge semantics (this is what makes corrections durable)

Re-running `joven detect` on an existing sidecar:

| Existing status | Behavior on re-detect |
|---|---|
| `auto` | **Overwrite** freely |
| `edited` | **Never touch** the translation; may update metadata |
| `approved` | **Never touch** |
| `rejected` | **Never re-add**, even if the detector now flags it confidently |

So your manual work monotonically accumulates and re-runs are always safe.

### 6.4 Fixing *mistranslations* → proactive review, not reactive

The insight: **don't wait to find errors while reading.** Triage the annotations
*before* loading the book on the device:

```bash
joven review annotations.json --epub book.epub
```

> **Built differently than planned.** This was specified as a static
> `--html coverage.html` report. It shipped as a small localhost server instead,
> because a static file cannot write decisions back — you would have been
> hand-editing JSON afterwards. The server writes each approve/edit/reject straight
> to the sidecar, so quitting mid-review loses nothing.

One card per annotation: preceding English context · Spanish source with the
detected spans highlighted · proposed translation · Approve / Edit / Reject, with
`a`/`r`/`e` keyboard shortcuts. Every decision writes straight to the sidecar, so
closing the tab loses nothing. 725 candidates, most of them one short line.

#### Ordering: confidence turned out to be the wrong signal ⚠️ revised

The original plan (and the first implementation) sorted by **ascending detector
confidence**, on the reasoning that the detector's own uncertainty is the best
available predictor of error. The full-book run disproved it. Sampling every
confidence band showed uniformly good translations:

| Confidence | Example | Translation |
|---|---|---|
| 0.50 | `En este pueblo, he said.` | "In this town, he said." ✅ |
| 0.51 | `Es para el camino, she said.` | "It's for the journey." ✅ |
| 0.98 | `A pesar de to que piensa la gente?` | "Despite all that people think?" ✅ |
| 1.00 | `Dieciseis.` | "Sixteen." ✅ |

Of course it doesn't predict quality — confidence measures how hard the *language
call* was, and that is driven by utterance length and dialogue tags. A three-word
Spanish line welded to `, he said.` scores 0.50 and translates perfectly.

What actually produces bad footnotes in this book is **damage in the scanned
source**, which the pipeline then faithfully translates:

| Source (as scanned) | Should be | Footnote produced |
|---|---|---|
| `La tercera historic` | `historia` | "The third **historic**" |
| `Nadie Babe to que le espera` | `sabe todo` | "Nobody **Babe to** what awaits him" |
| `Debe guitar el bowl` | `quitar` | "He must **guitar the bowl**" |
| `Está fibre.` | `libre` | "It's **fibre**." |
| `Que undo, she said.` | `Qué mundo` | "What **undo**" |

These sit anywhere from 0.51 to 0.99, so no threshold finds them — and they read as
confident nonsense, the worst kind of error to put on a device.

[`suspicion.py`](src/joven/suspicion.py) detects them without a list of known typos,
which would not survive a different book. It uses the *consequence* they share: **a
word the model could not translate survives verbatim into the English.** Two
signals, both cheap:

1. `carried_through()` — a Spanish content word reappears in the translation
2. `garbled_tokens()` — a digit welded inside a word (`É1`, `Conoc16`, `cuest16n`)

The first attempt flagged **20% of the book**, because the Spanish span *includes
the dialogue tag* and so `said`/`called`/`hissed` carry through trivially. That is
the same root cause as the two gate bugs in §8 — the third time the dialogue tag
corrupted a measurement — which is why the tag vocabulary is now derived from
`dialogue.SPEECH_VERBS` rather than hand-listed. After that: **19 of 725 (3%)**,
essentially all genuine, the rest being untranslatable regionalisms (`ejido`,
`hacendado`, `compadre`) that are worth a look for the same reason.

So the order is **suspect first, then book order**, and each flagged card shows the
reason (`untranslated: historic`) so a false positive costs one glance instead of
re-reading the paragraph. Book order for the remainder is not just a fallback:
reading in narrative sequence is how you catch a translation that is fine in
isolation but wrong for the scene.

Then `joven render` and you're done.

### 6.5 Fixing *missed* passages → three escape hatches

1. **Widen the escalation band and re-review.** The thresholds live in
   [`detect/triage.py`](src/joven/detect/triage.py) (`accept_spanish`,
   `reject_english`); lowering `reject_english` sends more of the middle to the
   LLM. Because `rejected` is sticky, re-reviewing is cheap — a re-run only asks
   you about annotations you have not already judged.

   > There is no `--escalate-below` flag. An earlier draft of this section
   > described one; the thresholds turned out to be a thing you tune once against
   > `docs/model-selection.md` and then leave alone, so exposing them per-run would
   > have invited exactly the miscalibration §2.3 warns about.
2. **Manual add by search.**
   ```bash
   joven add annotations.json --find "Bueno pues" --translation "Well then"
   ```
   Locates the text, mints the stable ID, sets `status: edited`.
3. **Kobo highlight round-trip** — highlighting *is* the bug report. See §6.6.

### 6.6 The Kobo highlight round-trip, explained — ⚠️ NOT IMPLEMENTED

> **Status: designed and schema-verified, no code written.** The database layout
> below was confirmed on the physical device, so the groundwork is real — but
> `joven add --from-kobo` **does not exist**. The `joven add --find` path (§6.5) covers
> the same need with a phrase you type yourself. Kept here because the schema
> findings are the expensive part and would otherwise have to be rediscovered.

**Problem it solves.** You're 200 pages in and hit a Spanish line the tool missed.
Without this, your options are to write it on paper or remember it, then later hunt
through 4,465 paragraphs to find it. Both are annoying enough that in practice you
won't, and misses never get fixed.

**What Kobo gives us for free.** Every highlight you make is stored *on the device*
in a SQLite database at `.kobo/KoboReader.sqlite`. Plug the Kobo in over USB and it
mounts as a normal volume — that database is just a readable file. Highlights live
in a `Bookmark` table, and one of its columns holds **the highlighted text itself**,
alongside identifiers for which book and which chapter file it came from.

So no OCR, no screenshots, no page numbers — the literal text is sitting in a
queryable table.

**The workflow:**

```
  read on Kobo
      │
      ├── hit a missed Spanish passage
      ├── highlight it (normal gesture, ~2 seconds)
      └── add a short note like "x"        ← marks it as a translation request
      │
  ▼ plug in USB
# NOT IMPLEMENTED — the design sketch for §6.6, shown for reference only
joven add annotations.json --from-kobo /Volumes/KOBOeReader/.kobo/KoboReader.sqlite
      │
      ├── read Bookmark table, filter to this book
      ├── keep only highlights carrying the marker note
      ├── fuzzy-match highlighted text back to our extracted paragraphs
      ├── translate just those
      └── append to sidecar with status: edited
      │
  ▼
joven render book.epub annotations.json -o out/     →  copy .kepub.epub back to device
```

The marker note matters: without it, `joven add` would hoover up your ordinary
reading highlights as translation requests. Filtering on the note keeps the two
uses of highlighting separate.

**Why fuzzy matching.** Kobo highlight boundaries snap to its own internal text
spans, so the stored text may start or end mid-word or differ in whitespace. Match
on normalized text with a similarity threshold, and fail loudly with the candidates
listed rather than guessing when it's ambiguous.

**✅ Schema verified against the physical device** (firmware `4.45.23697`):

```sql
CREATE TABLE Bookmark (
  BookmarkID TEXT NOT NULL,  VolumeID TEXT NOT NULL,  ContentID TEXT NOT NULL,
  StartContainerPath TEXT NOT NULL,  StartOffset INTEGER NOT NULL,
  EndContainerPath TEXT NOT NULL,    EndOffset INTEGER NOT NULL,
  Text TEXT,          -- the highlighted text  <-- what we match on
  Annotation TEXT,    -- the user's typed note <-- our marker
  Type TEXT,          -- 'highlight' | 'dogear'
  Hidden BOOL NOT NULL DEFAULT 0,  DateCreated TEXT,  Color INTEGER DEFAULT 0,
  ...
  PRIMARY KEY (BookmarkID)
);
```

Confirmed on device:

* **Filter `Type='highlight'`** — `dogear` rows are plain bookmarks with no text
  (observed: 8 dogears, 1 highlight).
* **`Annotation` is unused** (NULL on every existing row), so using a typed note
  as the "translate this" marker doesn't collide with anything.
* **`StartContainerPath` is a kepubify artifact**, e.g. `span#kobo\.81\.1` — it
  addresses an injected `koboSpan`, *not* the original XHTML structure.
  `ContentID` likewise uses the KEPUB form
  `<uuid>!OEBPS!xhtml/chapter51.xhtml`. **This confirms path-based matching is
  unusable and text matching is mandatory.**
* Highlight text can be as short as **one character** (observed `text_len=1`),
  so fuzzy matching with a similarity threshold is required, not optional.

> **Status: still deferred past v1** — it's a convenience on top of
> `joven add --find` — but it is now a known quantity rather than a guess.

---

## 6.6b Kobo footnote popups — what actually works

Hard-won on hardware (firmware `4.45.23697`). Recording it because almost every
inference we made from the spec alone turned out wrong, and the device is the only
authority.

**Kobo does have a popup**, branded *"Footnote preview"*, with a *See more* button
that navigates to the note. Per
[Kobo's published spec](https://github.com/kobolabs/epub-spec) it fires for a link
to a node when the target text is ≥9 and ≤5000 characters and **"the location
being linked to comes after the location being linked from"**.

### Measured results, 12 notes on one page

| Recipe | `epub:type` | note placement | hide | Result |
|---|---|---|---|---|
| Z (control) | yes | adjacent | `display:none` | tap → **jumps to start of book** |
| B | yes | adjacent | off-screen | tap → **link dead** |
| A | yes | adjacent | visible | note renders **inline**, no popup |
| C | yes (`<div>`) | adjacent | visible | same as A |
| D | **no** | end of chapter | visible | **popup**, but concatenates *every following note* |
| F | yes | end of chapter | visible | popup, same concatenation |
| G | yes (`<span>`) | end of chapter | visible | popup, same concatenation |
| **H** | yes | **one file per note** | visible | **popup showing a single note** ✅ |
| **I** | no | **one file per note** | visible | single-note popup / sometimes direct jump |
| E | — | inline brackets | — | works everywhere, no interaction |

### What that overturned

* **`display: none` is fatal.** No layout box ⇒ nothing to navigate to ⇒ Kobo
  falls back to position zero. This was the original bug.
* **Adjacency prevents the popup.** A note immediately after its paragraph simply
  renders inline. The M3b "notes must be adjacent" change was a wrong inference
  from a single confounded test, and it was *encoded as a verification check* that
  then defended the mistake. Both are gone; the check now tests Kobo's four
  published conditions instead.
* **`epub:type` does not decide popup vs. jump.** D (without it) and F (with it)
  both popped up. Useful for other readers, not the lever here.
* **Kobo's preview does not stop at the target element.** With notes as siblings
  it renders the tapped note *and everything after it*. Only **end of file** is a
  boundary it respects — hence one XHTML file per note, registered in the manifest
  and in the spine with `linear="no"` (epubcheck `RSC-011` requires spine
  membership; `linear="no"` keeps hundreds of stubs out of the reading order).

### Resolved: popup vs. jump is **position**, not length

Some markers preview and others jump straight to the note page. The obvious
suspect was note length; a calibration book (`tools/make_calibration.py`, notes of
10→250 characters each labelling its own length) ruled that out flatly — **250
characters previewed while 50 jumped.**

Mapping device page numbers back to spine order shows the real pattern:

| Note document | Spine slot | Device page | Length | Result |
|---|---|---|---|---|
| para 594 | 8 (1st note) | 883 | 250 ch | **preview** |
| para 592 | 9 (2nd note) | 884 | 180 ch, and 25 ch in an earlier round | **preview** |
| para 591 | 10 (3rd note) | 885 | 130 ch, and 99 ch earlier | jump |
| para 584 | 15 (8th note) | 890 | 50 ch | jump |

**Only the first two note documents in the spine preview**, consistently across two
rounds with different note contents. Likely mechanism: those two sit immediately
after the book's last real chapter, and Kobo can only preview a target it already
has loaded — but that is four data points and an inference, so hold it loosely.

**Accepted as-is.** A jump navigates to the correct note and offers "Back to page
N"; it is a worse reading experience than a preview, not a bug. Chasing it further
would mean fighting an undocumented cache heuristic for cosmetic gain.

Untried, if it ever matters: generating note documents in forward paragraph order
(they are currently written in reverse, a side effect of inserting markers
back-to-front so earlier insertions don't shift later offsets) would move *which*
two notes preview. That distinguishes position from paragraph identity in one pass.

### Shipping recipe

`FootnoteRenderer` defaults are now the surviving recipe: `placement="file"`,
`hide="visible"`, `element="aside"`, `epub:type` present, backlink present. Apple
Books renders these as proper popups throughout; Kobo previews some and navigates
correctly for the rest. The other placements and hide modes are kept solely for
diagnosis.

---

## 6.7 The decision trace — detection is never a black box

Added after the first device test, where the honest answer to "why isn't this
passage annotated?" was *we have no idea*. A book with missing footnotes has at
least five possible causes, each with a different fix:

1. the segmenter never produced that sentence
2. Tier 1 confidently called it English
3. Tier 1 abstained and no translator was configured
4. Tier 2 ran and said "not Spanish"
5. it's `rejected` in the sidecar from an earlier review

Guessing between those is hopeless, so **every segment writes a record** —
annotated or not — to a JSONL trace:

```bash
joven detect book.epub --trace trace.jsonl              # full two-tier run
joven detect book.epub --backend none --trace t1.jsonl  # tier 1 only: instant, free
joven explain trace.jsonl --find "Yo no sé nada"        # why was this skipped?
joven explain trace.jsonl --outcome tier2_rejected      # everything the LLM dropped
```

Each record carries the Tier-1 language, confidence, verdict and reason; the
tag-stripped form when stripping changed the call; and for escalated segments the
model, latency, parsed verdict, **and the raw LLM response verbatim** — so a bad
translation can be traced to what the model actually said rather than to what we
did with it.

JSONL because it stays greppable and streams without holding the book in memory:

```bash
jq -c 'select(.tier2_used and .tier2_is_spanish == false)' trace.jsonl
jq -c 'select(.outcome=="escalated") | [.tier1_confidence, .text]' trace.jsonl | sort
```

### What the first full trace immediately taught us

Tier 1 over all **12,120 segments** of the book — figures here and in §8 come from
a *damaged scan* of *The Crossing*, the copy the project was developed against.
The final run used a clean Knopf edition (12,302 segments); the damaged-copy numbers
are kept because the bugs they exposed are the interesting part.

| Outcome | Segments |
|---|---|
| Tier 1 accepted as Spanish | 623 |
| Tier 1 rejected as English | 9,573 |
| abstention band (→ LLM) | 1,921 |
| empty / punctuation only | 3 |

And it **overturned a threshold decision made on 27 cases.** The benchmark showed
no true-English sentence above 0.58 Spanish confidence, which suggested the 0.90
accept threshold could safely drop to ~0.75 and save a third of the LLM calls. At
book scale that is plainly wrong:

| Segment | Tier-1 Spanish confidence | Truth |
|---|---|---|
| `Yessir.` | 0.70–0.75 | **English** |
| `Astrolabe or sextant.` | 0.75–0.80 | **English** |
| `La Charca.` | 0.85–0.90 | place name |

Lowering the threshold would auto-accept `Yessir.` as Spanish at Tier 1, where no
LLM can veto it. **Threshold stays at 0.90**; the wide band is earning its keep.
A small hand-built benchmark could not have shown this.

---

## 7. Roadmap

Each milestone ends with something runnable and tested.

### M0 — Skeleton (½ day)
`uv`/venv, `pyproject.toml`, `typer` CLI, pytest, ruff. `joven inspect` prints the
structure table from §1. **Exit:** `joven inspect` works on the real book.

### M1 — Lossless round-trip (1 day) ← *do not skip or reorder*
Unpack → parse → re-serialize → repack, with **zero** changes. Get `mimetype`
STORED-first, entry order, and XHTML byte-stability right. Wire up `epubcheck`.
**Exit:** `joven render` with an empty sidecar produces a file that passes
`epubcheck` and the text-preservation invariant. *This milestone is the
foundation of all trust in the tool — the translation is worthless if the
repacking corrupts the book.*

### M2 — Detection + triage, no LLM (1–2 days)
Segmentation, lingua triage, tag-strip booster, sidecar writing. Build the
labelled golden set here. **Exit:** precision/recall reported on the golden set;
`detect --no-llm` emits a sidecar of confident candidates + an escalation queue.

### M2b — Local model selection (½ day, parallelizable with M2)
Benchmark Ollama candidates (7–14B Q4, within the ~8–10 GB budget) on the §2.2
adversarial set: the must-be-English traps (`Go on.`, `Yes mam.`,
`I dont intend to.`), the must-be-Spanish fragments (`Tantos, said the man.`,
`Güero, he said.`, `Sí, said the Mexican.`, `Tan horrible.`), the loanword
paragraphs that must be left alone (`matríz`, `copo`, `candela`), and the
span-splitting cases (B and D from §1.1). Score classification accuracy, span
correctness, translation quality, and tokens/sec.
**Exit:** a chosen model + recorded numbers in `docs/model-selection.md`.

### M3 — Rendering + **early Kobo device test** (2 days)
Text-node insertion with multi-span offset correctness. Order matters:

1. Inline-bracket renderer (proves insertion + preserves the invariant)
2. EPUB 2→3 package upgrade (`content.opf`, `nav.xhtml`, `epub:` namespace, CSS)
3. Popup footnote renderer (`noteref` + `aside`)
4. `epubcheck` gate on the EPUB 3, **then** `kepubify` → `.kepub.epub`
5. **Sideload a hand-made 3-annotation KEPUB to the Kobo and confirm the popup
   actually works** — before M4, before the full-book run

Step 5 is the real gate, and it must be tested **as KEPUB** — kepubify rewrites the
XHTML and injects `koboSpan` elements, which is exactly the kind of transform that
could disturb our markers. If popups misbehave on your firmware you find out here
for the cost of a USB copy, and pivot to endnote links with zero rework (same
sidecar, different renderer). Discovering it in M6 would be painful.

**Exit:** a 3-annotation sample renders with working popups on the physical Kobo as
a `.kepub.epub`, and `joven verify` is green on the intermediate EPUB 3.

### M4 — Local LLM adjudication + translation (1–2 days)
Batched prompts with surrounding context, JSON-schema-constrained output for
`{is_spanish, spans, translation}`, on-disk response cache keyed by content hash,
concurrency tuned to the machine. **Exit:** full-book pass at $0; the 887 ambiguous
sentences correctly triaged; cache makes re-runs instant.

### M5 — Review & corrections (1–2 days)
HTML coverage report, then the localhost review UI. `joven add`. Merge semantics
with sticky `edited`/`approved`/`rejected`. **Exit:** you can fix a bad
translation and re-render without re-running detection.

### M6 — Read the book. 🎉
Then harden based on what actually annoyed you.

**Explicitly out of scope for v1:** other input formats (MOBI/AZW3/PDF), other
language pairs, a GUI, DRM handling, distribution.

---

## 8. Decisions

### Resolved

| Decision | Choice |
|---|---|
| Target device | **Kobo** (sideload over USB) |
| Output format | **KEPUB** (`.kepub.epub`) via `kepubify`; EPUB 3 kept as the verifiable intermediate |
| Annotation style | **EPUB 3 popup footnotes** (`noteref` / `aside`), with EPUB 2→3 package upgrade |
| Granularity | Detect per sentence; **one footnote per paragraph** (contiguous runs merged) |
| Language | Python 3.13 |
| EPUB I/O | `zipfile` + `lxml` — **not** `ebooklib` |
| Form factor | CLI, with a local HTML/localhost review step |
| Translation backend | **Local LLM via Ollama** (offline, $0) — the only path in use. `--backend claude` is built and tested but **unused**: a Claude Pro subscription does not include API access, and no spend is planned. |
| State model | `annotations.json` sidecar as source of truth; epub never edited in place |
| Embedded loanwords | **No footnote** for a lone Spanish word inside an English clause — see below |

### Embedded loanwords → **not annotated** ✅ decided

`matríz`, `copo`, `candela`, `vaquero`, `orgullo` inside English narration get no
footnote. McCarthy uses them as texture; annotating each one is constant noise for
negligible gain. Enforced by `is_embedded_loanword()` in
[`detect/triage.py`](src/joven/detect/triage.py), applied on **both** pipeline paths
and traced as its own outcome (`embedded_loanword`) so suppressions stay visible.

The obvious implementation is wrong. "Reject when the Spanish span is a small
fraction of the sentence" cannot separate these:

| Segment | Spanish span | Want |
|---|---|---|
| `Bastante, the doctor said.` | `Bastante` — 1 of 4 words | **annotate** |
| `You know what is orgullo?` | `orgullo` — 1 of 5 words | **reject** |

Identical ratios, opposite answers. The real discriminator is *what kind* of
English surrounds the Spanish: in the first it is a **dialogue tag**, which is a
closed set the triager already recognises. So the rule strips dialogue tags and
inspects the remainder — if what's left is essentially just the Spanish span it is
an utterance; if substantive English survives, the Spanish is embedded in it.
14/14 on the adversarial set, with the ratio trap pinned by its own test.

> ⚠️ Before this existed, the case was being rejected **by accident**: the
> similarity veto (§ below) happened to fire because the model returned the whole
> sentence as the span. Had it returned just `orgullo`, the note would have shipped.
> Policies that hold only when a model guesses a particular way are not policies.

#### Corrected after the first full-book run — size the span first

The tag-stripping rule above is right but was incomplete: it measured the English
*around* the span without ever measuring the span itself, so it suppressed real
Spanish. 7 of 11 suppressions on the first full run were wrong:

| Segment | Span the model reported | Why it was suppressed |
|---|---|---|
| `…whether he wishes to or no. Somos dolientes en la oscuridad.` | `Somos dolientes en la oscuridad.` | a whole English sentence precedes it |
| `La tercera historia, said the gypsy, es ésta.` | `La tercera historia` | its own tag splits it, so `es ésta` counts as "outside" |
| `Me dice que él conoce to que sabe el lobo…` | `…conoce todo que sabe…` | **span/OCR mismatch** |
| `Yeguas and caballos, capones and potros.` | `Yeguas y caballos, capones y potros.` | **span/OCR mismatch** |

The last two are the instructive ones. The model silently *repaired the book's
scan errors* in the span it reported — `to` → `todo`, `and` → `y` — so words that
**are** the Spanish were counted as English sitting outside the span. Two phantom
words is all it takes to cross `max_outside_words`.

The fix is one check, placed first: **the policy is about a single Spanish _word_,
so a span of two or more words is never a loanword**, however much English
surrounds it. That is what was actually decided, it makes the whole class of
span/OCR mismatch harmless, and it is strictly simpler than tightening the
mismatch handling. The 4 remaining suppressions are each exactly one Spanish word
in English prose (`Cuidado`, `ciénega`, `sefior`, `Vámonos`) — correct.

### The similarity veto → **suppress no-op translations** ✅ decided

A footnote whose "translation" repeats the source teaches the reader nothing, and
the models produce them in two distinct ways:

1. **Dialect English mistaken for Spanish.** `Yessir.` → `Yes sir.`,
   `He's done ate.` → `He's done eating.` McCarthy's idiom is not a foreign
   language, but it isn't standard English either, so a model asked "is this
   Spanish?" sometimes says yes and hands back a tidied-up copy.
2. **Untranslated words echoed back.** `Bayo cebruno.` → `Bayo cebruno.`,
   `Porfirio, he said.` → `Porfirio, he said.` — names, places, and horse colours
   the model cannot translate and so simply returns.

`is_normalization()` in [`translate.py`](src/joven/translate.py) catches both
deterministically: flatten case and punctuation, then veto when
`SequenceMatcher` ratio ≥ **0.75**. Prompt guidance alone was not enough to rely
on — this holds whether or not the model complies.

#### Corrected after the first full-book run — strip tags before comparing

The veto fired on **43 segments, 35 of them genuinely Spanish**. The cause was
McCarthy's dialogue attribution: it survives translation *verbatim*, so it is
shared text on both sides that was never translated, and it dominates the ratio
on exactly the short utterances this book is full of.

| Compared | Ratio | Verdict |
|---|---|---|
| `Cuatro días, he said.` vs `Four days, he said.` | 0.78 | ❌ vetoed |
| `Cuatro días` vs `Four days` | 0.60 | ✅ kept |
| `Dos ordenes de las enchiladas, Billy said.` vs `Two orders…, Billy said.` | 0.78 | ❌ vetoed |
| `Dos ordenes de las enchiladas` vs `Two orders of the enchiladas` | 0.70 | ✅ kept |

Note how narrow the margin is: both stripped ratios (0.60, 0.70) sit *below* 0.75
but not far below, which is why the threshold itself was left alone. Lowering it
would re-veto these; raising it would start letting `Yessir.` (0.92, unaffected by
stripping) through.

So the comparison now runs on `strip_dialogue_tags()` output for both sides. The
cases the veto exists for are untouched: `Yessir.` has no tag to strip, and
`Old Niño, he said.` is identical with or without one. 14 of 43 vetoes were
released; the 29 that remain are no-ops, place names, or OCR garbage
(`Al none` → `At none`).

This is the same root cause as the loanword bug above, seen from the other side:
**both gates were measuring text that included the dialogue tag.** The tag
machinery therefore moved out of `detect/triage.py` into its own
[`dialogue.py`](src/joven/dialogue.py), shared by both — which also stops the
translator from importing `lingua`.

#### Replaying gates without re-running the book

Every escalated segment's trace record keeps the model's verbatim answer, so the
deterministic gates that run *after* the model can be re-evaluated offline:

```bash
python tools/replay_gates.py trace.jsonl
```

This turned "did the fix help, and what did it break?" from a 72-minute run into a
one-second question answered against the real corpus rather than invented cases —
and it is how the +21/-21 above was confirmed to contain **zero** new
suppressions. Threshold changes should be justified this way before a re-run.

> Gate changes that alter Tier-1 *confidence* (the dialogue-verb set does) still
> need a full re-run, because they change which segments escalate at all. Replay
> only sees segments the model already saw.

### Still open (defer — decide with evidence, not up front)

1. **Which local model.** Benchmarked in M2b against the §2.2 adversarial cases →
   [`docs/model-selection.md`](docs/model-selection.md).
2. ~~**Marker glyph.**~~ ✅ **Decided: `*`** (`MARKER_GLYPH` in
   [`render/annotate.py`](src/joven/render/annotate.py)). Chosen against the real
   Kobo page in M3 and device-verified across all ten markup recipes (§6.6b).
3. **Kobo highlight round-trip** (§6.6). Deferred past v1 — needs the device in
   hand to confirm the schema.

---

## 9. Serious challenges — ranked by risk

| # | Challenge | Severity | Mitigation |
|---|---|---|---|
| 1 | **74% of candidates are in the language-ID coin-flip band** (§2.2) | 🔴 High | LLM adjudication with context; abstention-band escalation; labelled golden set with precision/recall gates |
| 2 | **Mixed-language sentences** — the unit of translation isn't a clean language span (cases B/D) | 🔴 High | Char-offset spans in the sidecar; LLM returns the span; renderer anchors the marker at the span end. Keeping the English tag inside the translated source and reproducing it is acceptable and often reads better. |
| 3 | **Spanish loanwords inside English narration** (`matríz`, `copo`, `candela`, `güero`, `vaquero`) | 🟠 Med-High | Minimum-Spanish-content rule + loanword allowlist + LLM confirmation. Never annotate a sentence whose Spanish span is a single non-dialogue word. |
| 4 | **McCarthy's nonstandard orthography** (`Se fué`, `Dieciseis`, `matríz`) and total absence of quotation marks | 🟠 Med | LLM handles it; dictionary/rule-based approaches do not. No-quotes means dialogue-tag detection is heuristic — hence the LLM. |
| 5 | **Kobo popup-footnote fidelity varies by firmware** on a sideloaded, upgraded-from-EPUB-2 file | 🟠 Med | Physical device test in M3 step 4 with a 3-annotation sample, before any LLM spend. Fallbacks (KEPUB, endnote links) reuse the same sidecar — zero rework. |
| 6 | **Marker placement aesthetics** — where does a superscript go in unpunctuated dialogue without wrecking the rhythm? | 🟡 Low-Med | Anchor at end of the Spanish span, before the English tag. Configurable glyph. Eyeball it on the real Kobo page in M3. |
| 7 | **Artistic intent.** McCarthy *chose* not to translate — the opacity is deliberate. | 🟡 Low-Med (design, not technical) | Popup footnotes are the right call here: the page reads unchanged until you deliberately tap. Also render a clean unannotated copy from the same sidecar so you can switch if the markers start to grate. |
| 8 | **EPUB 2→3 package upgrade** touches `content.opf`, adds namespaces, may need a nav document | 🟡 Low-Med | Guard with `epubcheck` in `--version 3.0` mode; keep the upgrade an isolated, tested transform |
| 9 | **DRM'd inputs** (not this file, but the next one) | 🟢 Low | Detect `META-INF/encryption.xml` in `inspect` and fail with a clear message rather than producing garbage |
| 10 | **LLM nondeterminism** across re-runs | 🟢 Low | Content-hash response cache; the sidecar is committed, so translations are frozen once reviewed. Pin `temperature=0` and the model tag. |
| 11 | **Local 7–14B model is weaker than a frontier model** on literary Spanish idiom and on span selection | 🟠 Med | Accepted tradeoff for $0. Mitigated by: only asking it about pre-filtered candidates; the confidence-sorted review pass (§6.4); and the sidecar design, which lets a better backend later re-run *only* the low-confidence subset without discarding reviewed work. Quantified in M2b rather than assumed. |
| 12 | **kepubify's `koboSpan` injection could disturb footnote markers or `aside` handling** | 🟠 Med | KEPUB is what gets device-tested in M3 step 5; `verify` asserts noteref→footnote integrity on the EPUB 3, and a structural diff catches kepubify surprises |

---

## 10. Immediate next step

M1. Get lossless round-trip + `epubcheck` + the text-preservation invariant green
before writing a single line of translation code. Everything downstream is
worthless if the repacking corrupts the book, and it's much cheaper to establish
that guarantee now than to debug a subtly-broken 324 KB zip later.

```bash
brew install epubcheck
```
