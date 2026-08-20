# epub-translation-helper (`etx`)

Insert clickable translation footnotes for untranslated foreign-language passages
in an EPUB, so you can read them on a Kobo without reaching for a phone.

Built for Cormac McCarthy's *The Crossing*, which is mostly English with long
untranslated stretches of Mexican-border Spanish.

**Design, measurements, and roadmap: [DESIGN.md](DESIGN.md).**
**Local model benchmark: [docs/model-selection.md](docs/model-selection.md).**

## Status

M1–M5 complete: lossless round-trip, two-tier detection with a full decision
trace, EPUB 2→3 upgrade, footnote rendering device-verified on a Kobo, KEPUB
output, and a review pass for triaging translations.

The full book has been run end to end — 12,120 segments, 2,544 local-LLM calls,
72 minutes, $0 — and the trace from it caught two false-suppression bugs that
synthetic tests had missed. Next: human review of the detected passages.

## Install

```bash
brew install epubcheck kepubify ollama
python3.13 -m venv .venv
./.venv/bin/pip install -e '.[dev]'

ollama serve &            # once
ollama pull qwen3:8b
```

## Use

```bash
etx inspect book.epub                                    # structure, DRM, word counts
etx detect  book.epub -o annotations.json --trace trace.jsonl
etx review  annotations.json --epub book.epub            # triage, worst-confidence first
etx render  book.epub annotations.json -o out/           # EPUB 3 + KEPUB for the Kobo
etx verify  out/book.annotated.epub --original book.epub
```

`review` opens a local page listing every annotation with the surrounding prose and
the Spanish highlighted. Approve, edit, or reject (`a`/`r`/`e`); each decision
writes straight to the sidecar and **survives re-detection**, so review time is
never wasted.

Suspect annotations sort first, each badged with the reason — `untranslated:
historic`, `garbled source: Conoc16`. These catch the dominant error class, which is
damage in the scanned source that the pipeline then faithfully translates
(`Está fibre.` → "It's fibre." for `Está libre.`). Everything else follows in book
order. Notably *not* sorted by detector confidence: that measures how hard the
language call was, not whether the translation is right, and the full-book run
showed quality is uniformly good across every band.

Missed a passage while reading?

```bash
etx add annotations.json --epub book.epub \
  --find "Bueno pues" --translation "Well then"
```

Variations worth knowing:

```bash
etx detect book.epub --backend none --trace t1.jsonl   # tier 1 only: instant, free
etx detect book.epub --limit 200                       # scan a slice while iterating
etx render book.epub annotations.json --style inline    # bracketed text (debug view)
```

### The paid backend — built, not used

> **Status: implemented and tested, but not in use.** A **Claude Pro subscription
> does not include API access** — the API is billed separately, as prepaid credits
> on the Developer Platform. Spending money on this hobby project isn't wanted, so
> **the local model is the only path in use.** The code below stays because the
> analysis behind it is worth keeping and the seam costs nothing to maintain:
> `anthropic` is an optional dependency, imported lazily, and nothing in the
> default flow touches it. A test suite covers it with a fake client, so it cannot
> silently rot.

`--backend claude` swaps in the Claude API, which would fix the error classes the
8B model got wrong — dialect-English false positives, context bleed, untranslated
regionalisms. Costing a run first is free, because Tier 1 is offline and the
escalation count is what drives the bill:

```bash
etx estimate book.epub --model claude-sonnet-5     # ~2s, makes no paid calls
etx detect book.epub --backend claude --max-cost 2.00
```

Measured on this book: **2,544 calls → $1.26 (Sonnet 5, batched)**, and the run
aborts rather than overshooting `--max-cost`.

Two findings from that work are worth keeping regardless of whether it's ever run:

**75% of the input is the same prompt re-sent 2,544 times.** The Spanish being
translated is the minority of the bill. Any prompt-driven pipeline has this shape,
and it's invisible until you measure it.

**Prompt caching has a minimum prefix length, and it is not monotonic across
models** — 512 tokens on Opus 5, 1024 on Sonnet 5, 4096 on Haiku 4.5. Below it the
API silently does not cache: no error, just full price every call. That is why
**Haiku would cost more than Sonnet here despite being a third of the per-token
price**, and why the fix is a *bigger* prompt rather than a smaller one.

### Improving quality without spending anything

The levers that are actually available:

1. **A clean source EPUB.** The dominant error class is scan damage the pipeline
   then faithfully translates (`Está fibre.` → "It's fibre."). A clean copy removes
   it outright — and fixes the body text, which no sidecar edit can.
2. **The review pass.** `etx review` already surfaces the suspect annotations, and
   every decision is sticky across re-detection.
3. **A larger local model.** `qwen3:8b` won on the benchmark against the other 8B
   candidates ([docs/model-selection.md](docs/model-selection.md)); a larger model
   is the next free lever if the hardware allows. `tools/bench_models.py` scores a
   new one against the same 27 adversarial cases.

### Debugging: why isn't this passage annotated?

Never guess — ask the trace. Every segment gets a record whether or not it became
a footnote:

```bash
etx explain trace.jsonl --find "Yo no sé nada"
etx explain trace.jsonl --outcome tier2_rejected      # everything the LLM dropped
jq -c 'select(.outcome=="escalated") | [.tier1_confidence, .text]' trace.jsonl | sort
```

Records carry the Tier-1 language and confidence, the verdict and why, and for
escalated segments the model, latency, and the **raw LLM response** — so a bad
translation traces to what the model said, not to what we did with it.

Because the raw response is kept, the deterministic gates that run *after* the
model can be re-evaluated offline — tuning a threshold costs a second instead of a
72-minute book run, and it is checked against the real corpus:

```bash
python tools/replay_gates.py trace.jsonl      # before/after, and what flipped
```

`render` emits both a spec-clean `.epub` (the verifiable intermediate — this is
what `epubcheck` validates) and a `.kepub.epub` (what you copy to the Kobo; the
double extension matters). Copy the KEPUB to the device root over USB, eject, and
the Kobo imports it.

## Test

```bash
./.venv/bin/pytest                       # synthetic fixtures only
./.venv/bin/ruff check src tests tools

# opt in to the real-book tests (the book is never committed)
ETX_TEST_EPUB=/path/to/book.epub ./.venv/bin/pytest
```

Tests never hit the network. Model benchmarks are separate tools, not tests:

```bash
python tools/bench_models.py             # LLM in isolation
python tools/bench_pipeline.py           # the two-tier system
```

## How it works

```
epub → extract text units → segment into sentences
     → TIER 1  lingua triage: confident Spanish / confident English / abstain
     → TIER 2  local LLM adjudicates the abstention band (~37%)
     → annotations.json          ← source of truth, human-editable
     → render (idempotent)  →  .epub  →  kepubify  →  .kepub.epub
```

The EPUB is never edited in place. `annotations.json` is the durable artifact, so
corrections mean editing the sidecar and re-rendering — never re-translating. Human
edits are sticky across re-detection.

Two properties are load-bearing and enforced by tests:

1. **Text preservation.** Strip the inserted annotation nodes from the output and
   the remaining prose is byte-identical to the original.
2. **Precision over recall.** A spurious footnote on `Go on.` is worse than a
   missed one, so ambiguous cases escalate rather than guess.

## Legal

For personal use on a book you own. Don't distribute modified copies.
