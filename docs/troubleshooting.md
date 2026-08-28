# Tracing, tuning, and troubleshooting

The second-read material: how to find out why a passage was or wasn't annotated,
what levers actually improve translation quality, the flags that exist for
debugging rather than for use, and the Kobo behaviour worth knowing before you
blame the tool.

For *why* the pipeline is shaped this way, see [DESIGN.md](../DESIGN.md).

---

## Why isn't this passage annotated?

Never guess — ask the trace. A book with a missing footnote has at least six
possible causes, each with a different fix: the segmenter never produced that
sentence, Tier 1 confidently called it English, Tier 1 vetoed it as Latin, Tier 1
abstained with no translator configured, Tier 2 ran and said "not Spanish", or it is
`rejected` in the sidecar from an earlier review.

So every segment gets a record whether or not it became a footnote:

```bash
joven detect book.epub -o annotations.json --trace trace.jsonl
joven explain trace.jsonl --find "Yo no sé nada"
joven explain trace.jsonl --outcome tier2_rejected      # everything the LLM dropped
```

Records carry the Tier-1 language and confidence, the verdict and why, and for
escalated segments the model, the latency, and the **raw LLM response** — so a bad
translation traces to what the model said, not to what we did with it.

The trace is JSONL because it stays greppable and streams without holding the book
in memory:

```bash
jq -c 'select(.outcome=="escalated") | [.tier1_confidence, .text]' trace.jsonl | sort
jq -c 'select(.tier2_used and .tier2_is_spanish == false)' trace.jsonl
```

### A passage was annotated as Spanish and isn't

The reverse question, and the trace answers it the same way. Two causes worth
knowing:

```bash
joven explain trace.jsonl --find "Stabat Mater"
```

`reason: latin, not spanish` means the Latin veto caught it — Tier 1's detector
knows only English and Spanish, so a third language is asked about separately before
a passage can be accepted outright (DESIGN.md §2.6). If it vetoed something that
really is Spanish, `Triager(veto_latin=False)` turns it off, and the case is worth
reporting: measured over 1,094 known-Spanish passages it rejected none.

A "translation" that is really the same words tidied up — `No suh.` → "No sir." —
is the similarity veto missing one. It fires at a 0.75 ratio and that pair scores
0.67. Lower `SIMILARITY_VETO` in `translate.py` and replay the gates (below) to see
what else the change would catch or break before committing to a re-run.

### Resuming an interrupted run

`detect` writes the sidecar once, at the end, but the trace is flushed a record at a
time — so an interrupted run has already recorded every model answer it received:

```bash
joven detect book.epub -o annotations.json --trace trace.jsonl --resume trace.jsonl
```

Every recorded answer is reused and only unreached segments cost anything. Tier 1
and the suppression gates still run over the whole book, so a resumed run reflects
the current code. Two things are deliberately *not* reused: recorded errors (they
are retried), and any answer whose Tier-1 verdict has since changed, because the
accept and escalation paths ask the model different questions.

**A prompt change is not visible to `--resume`.** The recorded answer is reused as
it stands, so after editing `SYSTEM_PROMPT` or the few-shot examples, re-run without
`--resume` or the change will not be measured.

### Replaying the gates without re-running the book

Because the raw response is kept, the deterministic gates that run *after* the model
can be re-evaluated offline. Tuning a threshold costs a second instead of a
73-minute book run, and it is checked against the real corpus rather than invented
cases:

```bash
python tools/replay_gates.py trace.jsonl      # before/after, and what flipped
```

Threshold changes should be justified this way before a re-run. The exception:
changes that alter Tier-1 *confidence* — the dialogue-verb set does — still need a
full re-run, because they change which segments escalate at all. Replay only sees
segments the model already saw.

---

## Improving quality

Three levers, in order of how much they are worth:

1. **A clean source EPUB.** The dominant error class is scan damage that the
   pipeline then faithfully translates (`Está fibre.` → "It's fibre." for
   `Está libre.`). A clean copy removes it outright — and fixes the body text,
   which no sidecar edit can. Check a candidate before you trust it:

   ```bash
   python tools/scan_damage.py book.epub
   ```

   It exits non-zero when it finds anything, so it works as a gate. The markers it
   looks for were all found by auditing what the translation pipeline got wrong on
   a damaged copy of *The Crossing*; a different scan will have its own
   substitutions but the same *kinds*.

2. **The review pass.** `joven review` already surfaces the suspect annotations
   first, and every decision is sticky across re-detection, so review time is never
   wasted.

   Notably the ordering is *not* by detector confidence. That measures how hard the
   language call was, not whether the translation is right, and the full-book run
   showed quality is uniformly good across every confidence band. What does predict
   a bad footnote is a word the model could not translate surviving verbatim into
   the English — see [DESIGN.md §6.4](../DESIGN.md).

3. **A larger local model.** `qwen3:8b` won on the benchmark against the other 8B
   candidates ([model-selection.md](model-selection.md)); a larger model is the
   next free lever if the hardware allows. `tools/bench_models.py` scores a new one
   against the same 27 adversarial cases.

---

## Debug flags

These exist for working on the tool, not for annotating a book:

```bash
joven detect book.epub --backend none --trace t1.jsonl   # tier 1 only: instant, free
joven detect book.epub --limit 200                       # scan a slice while iterating
joven render book.epub annotations.json --style inline   # bracketed text, no popups
```

`--backend none` answers "is this a detection problem or a translation problem?" in
seconds instead of an hour. `--style inline` renders `Vaya con Dios. [Go with God.]`
directly in the prose — useless on a device, but it makes a diff readable and it was
the renderer that proved the insertion pipeline in the first place.

---

## Kobo notes

**The filename matters.** `render` emits both a spec-clean `.epub` (the verifiable
intermediate — this is what `epubcheck` validates) and a `.kepub.epub` (what you
copy to the device). Kobo needs that double extension to recognise a sideloaded
KEPUB. Copy the KEPUB to the device root over USB, eject, and the Kobo imports it.
`joven verify` asserts the naming.

**Some notes preview, others jump.** Kobo's *Footnote preview* popup appears for
the first two note documents in the spine; the rest navigate to the note page and
offer a "Back to page N" link. This is position-dependent, not length-dependent —
a 250-character note previews while a 50-character one jumps — and it was measured
on hardware with a purpose-built calibration book
([DESIGN.md §6.6b](../DESIGN.md)). It is a worse reading experience than a preview,
not a bug, and chasing it further would mean fighting an undocumented cache
heuristic for cosmetic gain.

**If a marker does nothing when tapped,** the note target has no layout box. A
`display: none` note is unnavigable, so Kobo falls back to position zero and jumps
to the start of the book. The shipping renderer keeps notes visible in the flow, one
per file; `joven verify` checks Kobo's four published popup conditions.

**Already-converted sources.** If you hand `render` a file that has already been
through `kepubify`, it strips the injected `koboSpan` elements before annotating and
says so. You do not need to do anything, but it is worth knowing that the output is
built from the de-kepubified text.

---

## Regionalisms without a clean translation

`ejido`, `hacendado`, `compadre` — the model often returns them untranslated,
because there is no single English word. They surface in `joven review` alongside
the genuine failures, since both share the same signature: a Spanish word carried
through into the English. About 10 of 726 footnotes on the full run. A glossary
would suit them better than a footnote, which is a thing this tool does not do.
