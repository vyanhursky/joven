# Joven — a Cormac McCarthy ebook Spanish annotator

[![CI](https://github.com/vyanhursky/joven/actions/workflows/ci.yml/badge.svg)](https://github.com/vyanhursky/joven/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

```
   #                                                       #
   ##                                                     ##
    #*                                                   *#
    *##                                                 ##*
     *###                                             ###*
       ####*                                       *####
         ######*+                             +*######
           *#########****+==:::::==+++****#########*
              **#######=:--.......--::=+#######**
                  ***=:-...........----=++***
                    =:%@@%%........-%%@@%++
                   :-@@@@@@@.......@@@@@@@=+
                  -..@@@@@@@.......@@@@@@@-:+
                 :...%@@@@@%.......%@@@@@%--:+
                 :.................---------:+
                 =........-.........:.------=+
                  :-.....-..........-:-----=+
                    =:::=-..........-:+==++
                        =-...........:+
                        =-...........:+
                        =-...........:+
                        =-...........:+
                         -..........-=
                         :....%@%...-=
                         =-..%@@@%.-:+
                          :-.@@@@@-:=
                           :-%@@@%:=
                            =:%@%:=
                              ===
```

**Finds the untranslated Spanish in an English novel and inserts tappable
translation footnotes**, so you can read McCarthy's border trilogy on a Kobo
without reaching for a phone every other page.

*The Crossing* is mostly English with long untranslated stretches of
Mexican-border Spanish — hundreds of lines of dialogue the book never translates
for you. This annotates them in place, leaving the prose byte-for-byte unchanged.

> ### `Escúchame, joven`
>
> — *Listen to me, young man.* It appears nineteen times in the book, and eighteen
> of this run's footnotes land on a passage containing it. Hence the name.

Everything runs on your machine against a local model: no API key, no per-book
cost, nothing uploaded.

**Design, measurements, and roadmap: [DESIGN.md](DESIGN.md).**
**Local model benchmark: [docs/model-selection.md](docs/model-selection.md).**

## Status

**Working end to end on a real book:** lossless round-trip, two-tier detection with
a full decision trace, EPUB 2→3 upgrade, footnote rendering device-verified on a
Kobo, KEPUB output, a review pass, and a finished book on the device.

Last full run — Knopf's 1994 edition of *The Crossing*, 151,865 words:

| | |
|---|---|
| segments considered | 12,302 |
| escalated to the LLM | 2,556 (21%) |
| footnotes produced | 726 |
| wall clock / cost | 73 minutes / $0 |
| integrity checks | 11 of 11 passing |

Two earlier runs against a damaged scan of the same novel paid for themselves by
exposing bugs no synthetic fixture had caught — see [DESIGN.md](DESIGN.md) §7.

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
joven inspect book.epub                                    # structure, DRM, word counts
joven detect  book.epub -o annotations.json --trace trace.jsonl
joven review  annotations.json --epub book.epub            # triage, suspect passages first
joven render  book.epub annotations.json -o out/           # EPUB 3 + KEPUB for the Kobo
joven verify  out/book.annotated.epub --original book.epub
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
joven add annotations.json --epub book.epub \
  --find "Bueno pues" --translation "Well then"
```

Variations worth knowing:

```bash
joven detect book.epub --backend none --trace t1.jsonl   # tier 1 only: instant, free
joven detect book.epub --limit 200                       # scan a slice while iterating
joven render book.epub annotations.json --style inline    # bracketed text (debug view)
```

### Improving quality

The levers that are available:

1. **A clean source EPUB.** The dominant error class is scan damage the pipeline
   then faithfully translates (`Está fibre.` → "It's fibre."). A clean copy removes
   it outright — and fixes the body text, which no sidecar edit can.
2. **The review pass.** `joven review` already surfaces the suspect annotations, and
   every decision is sticky across re-detection.
3. **A larger local model.** `qwen3:8b` won on the benchmark against the other 8B
   candidates ([docs/model-selection.md](docs/model-selection.md)); a larger model
   is the next free lever if the hardware allows. `tools/bench_models.py` scores a
   new one against the same 27 adversarial cases.

### Debugging: why isn't this passage annotated?

Never guess — ask the trace. Every segment gets a record whether or not it became
a footnote:

```bash
joven explain trace.jsonl --find "Yo no sé nada"
joven explain trace.jsonl --outcome tier2_rejected      # everything the LLM dropped
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
JOVEN_TEST_EPUB=/path/to/book.epub ./.venv/bin/pytest
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
     → TIER 2  local LLM adjudicates the abstention band (~21%)
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

## Licence

The **code** is MIT — see [LICENSE](LICENSE). Use it, fork it, ship it.

The **books are not.** That is a separate question, and the distinction matters:

- Bring your own EPUB, legally obtained. This tool reads a file you already have;
  it does not fetch, share, or unlock anything, and it refuses DRM-protected files
  outright rather than working around them.
- The annotated output is a derivative of a copyrighted work. Read it, don't
  distribute it.
- **The repository deliberately tracks no book content.** The hazard is not the
  EPUB but what the pipeline derives from it: a full decision trace holds every
  segment's source text verbatim — for a 150,000-word novel that is the entire
  book. `.gitignore` excludes those by pattern, and
  [`tools/check_no_book_content.py`](tools/check_no_book_content.py) verifies it by
  *content*, diffing every tracked file against the book itself:

  ```bash
  python tools/check_no_book_content.py path/to/book.epub
  ```

  Docs and tests quote short passages to illustrate the detection problem — about
  1,500 characters in total, longest single quote 78 characters. The checker fails
  the build if any tracked file exceeds a threshold.
