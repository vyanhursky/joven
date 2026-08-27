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

> The boy sat down to read the book. He read of horses and of the country to the
> south and of men who spoke in a tongue he did not have.
> *Escúchame, joven,* said the old man, and the boy did not know what it was he
> was meant to hear. So he set down the book and took up the phone and fed the
> words into Google Translate one at a time like a man counting stones across a
> river, and when he looked up again the twilight had gone out of the valley and
> the page had gone cold and the old man was still standing there in the dark
> holding his counsel like a man holding a lamp for nobody.
>
> So the boy wrote a python service. It went through the book and marked out every
> passage that was not English and put the question to a small model that ran on
> his own machine and asked nothing of anyone and told no one what it had seen.
> Where the Spanish had been it set a single asterisk and no more than that, and
> under the asterisk it laid the English down like a coin under a tongue, and the
> boy could take it or leave it as he pleased. He read the book through and he
> never once opened Google Translate. The old man went on speaking in his own
> language as he had always done and as he would go on doing, and the boy
> understood him, and the sun went down bloodred over the mesa and he read on by
> the pale light of the device until the battery gave out.

**Joven finds the untranslated Spanish in an English novel and inserts tappable
translation footnotes.** It was created specifically to improve the readability of
Cormac McCarthy's *The Crossing*, and will work well for other McCarthy westerns.

Everything runs on your machine against a local model: no API key, no per-book
cost, nothing uploaded.

**Design, measurements, and roadmap: [DESIGN.md](DESIGN.md).**
**Local model benchmark: [docs/model-selection.md](docs/model-selection.md).**

## Why this service needed

Using McCarthy's *The Crossing* as an example - it is an English novel with a great deal of Spanish in it, and the
book never translates or sufficiently contextualizes it. Spanish phrases and sentences are mixed into English paragraphs, and reaching for a dictionary/translator interrupts the flow of reading. 

**E-reader Dictionary and Translate struggle with McCarthy bilingualism.** McCarthy uses no quotation
marks, so speech and narration run together in one stream, and the Spanish arrives
in four distinct shapes:

```text
A  Vaya con Dios.                          a whole paragraph, no English at all
B  Cuántos años tienes? the old man said.  Spanish speech, English dialogue tag
C  The matríz will not help you, he said.  English prose, Spanish loanword
D  Escúchame, joven, the old man wheezed.  Spanish opener, then English narration
```

In *The Crossing*, there are a total of **726** Spanish sentences or phrases. As a non-spanish reader, your choices are to either stop reading and translate the phrase into a phone, breaking the trance the prose spent forty pages building, or skim past it and accept a hole in the page.


## Service-modified Epub

This service provides a modified copy of your EPUB in which every Spanish passage carries a small `*`. Tap
it and the translation appears in the reader's own footnote popup; ignore it and
the page reads exactly as the author set it down.

- **Unintrusive by construction.** One asterisk. No inline brackets, no interlinear
  clutter, no colour. The footnote is opt-in, the way McCarthy's silence is opt-in.
- **The prose is untouched.** Strip the inserted nodes from the output and what
  remains is *byte-identical* to the original — enforced by a test, verified on the
  real book.
- **It stays on your machine.** A local `qwen3:8b` does the translating — 73
  minutes for the whole novel, $0, and the book never leaves the laptop.
- **Precision over recall.** A spurious footnote on `Go on.` is worse than a missed
  one, so ambiguous cases escalate to the model rather than guess.

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
| integrity checks | 12 of 12 passing |

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

The two promises above are load-bearing, so both are enforced by tests rather than
by care: text preservation is a byte-comparison run over the whole book on every
render, and the precision bias is pinned by a corpus of adversarial cases drawn
from the real text (`tools/bench_pipeline.py`).

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

  Docs and tests quote short passages to illustrate the detection problem —
  1,116 characters in total across 8 files, longest single quote 78 characters.
  The checker fails the build if any tracked file exceeds a threshold.
