# Changelog

## Unreleased

The run is now something you can watch, speed up, point at whichever local model
server you already have, and configure once instead of on every command line —
plus the sidecar commands the CLI was missing. Before that, the small gaps found by
reading the code cold after the Windows port, none of them visible from a green
suite.

### Added

- **A progress bar.** `detect` showed nothing for the length of a run — 12 minutes
  on a GPU, 73 on a laptop. It now reports paragraphs done, escalations, footnotes
  so far, the last model latency and the time left, on stderr, and gets out of the
  way when the run ends or when stderr is not a terminal. `--quiet` turns it off.
- **`detect --workers N`.** Several paragraphs in flight at once. The pipeline is
  restructured so one paragraph is the unit of work and paragraphs are *finished*
  strictly in book order however the model schedules them, so `trace.jsonl`,
  `--resume` and the sidecar are identical at any worker count — pinned by a test
  that runs the same book on one worker and four with a deliberately jittery
  backend. Ollama needs `OLLAMA_NUM_PARALLEL` to match or the workers queue.
- **`detect --backend openai`**, for any server speaking the OpenAI chat protocol:
  llama.cpp's server, LM Studio, vLLM, a router in front of them. Same prompt, same
  few-shot examples, same JSON contract; only the wire format differs, and the run
  stays offline. Two request features vary between servers — a strict
  `json_schema` response format and the `enable_thinking` switch for Qwen3-style
  models — and the backend learns which yours accepts from its first 400 rather
  than assuming, then strips any `<think>` block that arrives anyway.
- **Configuration.** `joven.toml` in the working directory, a user-level
  `config.toml`, and `JOVEN_*` variables, with one precedence order: flag, then
  environment, then project file, then user file, then default. `joven config`
  prints every value with its source. An unknown key or a wrong type is an error,
  because a silently ignored `worker = 4` is a run four times slower than intended
  with nothing to say why. `JOVEN_EPUBCHECK_JAR` is unchanged; it is now simply the
  environment spelling of the `epubcheck_jar` setting.
- **`joven reject`, `joven reset`, `joven status`, `joven diff`.** The review
  page's decisions from the shell, where a sidecar stands, and what a
  re-detection changed, by annotation id.
- **`detect --href` and `--range START:STOP`**, alongside `--limit`, for iterating
  against a real model on one chapter.
- **`joven detect --ollama-url`**, also read from `JOVEN_OLLAMA_URL`. The server
  address was a constant, so an Ollama on another host or port was unreachable.
- **`j` / `k`** move between review cards, and **Ctrl-Enter** saves an edit from
  inside the text box.

### Measured

A Vintage edition of *The Crossing* (160,262 words, 5,637 paragraphs, 13,878
segments) on the RTX 4070 Ti SUPER with `qwen3:8b`, Ollama at its defaults:

| | one worker | `--workers 4` |
|---|---|---|
| escalated to the LLM | 3,046 (22%) | 3,046 |
| footnotes | 735 | 734 |
| trace order | — | identical, segment for segment |
| model time per call | 0.3 s | 0.8 s |
| wall clock | 15.2 min | 13.4 min |

The trace is the same shape at both settings, and the footnotes differ by one
(four of 13,878 outcomes moved, which is Ollama's batching, not the pipeline).
The speed-up is small **because this Ollama was not told to run requests in
parallel**: the per-call latency nearly tripled, which is four requests waiting in
one queue. `OLLAMA_NUM_PARALLEL=4` in Ollama's environment is what the flag needs,
and the docs now say so; the ordering guarantee is what this run proves.

The same 200-paragraph slice through `--backend openai` against a llama.cpp router
serving a 27B Qwen3 model: 118 calls, zero errors, valid JSON on every reply, and
the same Spanish/not-Spanish verdict as `qwen3:8b` on 112 of them. The six that
differ are the hard cases both models are entitled to disagree on — `Vaquero.`,
`Nuevo Mexico.`, `Momentito, she said.` — at 5.5 s a call for the larger model.

### Fixed

- **The review page keeps its place.** Every decision re-rendered the list, which
  dropped keyboard focus, so the next `a` or `r` went to whichever card was first
  on the page — with *hide reviewed* off, the card you had just decided. Focus now
  follows the annotation, not the DOM node, and a decision moves to the next
  unreviewed card.
- **`render` and `review` say when a sidecar belongs to a different file.** Every
  `detect` recorded the source's SHA-256 and nothing ever read it back, so a
  sidecar from another edition surfaced only as a `source text drifted` error deep
  inside rendering. The plain fact is now stated first, as a warning: the same
  edition re-saved by Calibre hashes differently and renders fine, and the
  per-paragraph check remains the hard gate.

### Infrastructure

- **mypy runs in CI**, with `lxml-stubs`. Its first pass raised 21 findings, every
  one an Optional from lxml that the code assumed non-None — `getparent()` on
  what could be the root, `find()` results used without a check. None was a live
  bug; each was the shape of one that ruff cannot see.
- **A GitHub release is created from every tag**, with the wheel and sdist
  attached and the CHANGELOG section for that version as its notes. A tag whose
  version has no CHANGELOG section fails before the PyPI upload, not after, since
  the version number is spent either way (`tools/changelog_section.py`).

## v1.0.0b4 — 2026-09-02

**Windows is a supported platform.** Everything here was found by running the tool
on Windows 11 for the first time — the suite, the guard scripts, and a whole novel
through `detect`, `render` and `verify`. The README had said "Windows is
untested", and untested turned out to mean four bugs, one of them silent.

### Fixed

- **External tools are run by resolved path, not by bare name.** `shutil.which`
  honours `PATHEXT`, so on Windows it resolves `epubcheck.CMD` — but
  `CreateProcess` can only start a `.exe`, so `subprocess.run(["epubcheck", …])`
  raised `FileNotFoundError` for a tool that was installed and on `PATH`.
  `epubcheck_available()` returned True and the run then died, which is the worst
  shape a dependency check can take. It affects every `.cmd`/`.bat` launcher,
  which is how epubcheck and most JVM tools arrive on Windows. Three integration
  tests failed on it.

- **epubcheck is found without a launcher at all.** The official distribution is a
  zip holding `epubcheck.jar` and nothing else — no `.bat`, no `.exe` — so on
  Windows there was never anything for `PATH` to find. This was the silent one:
  epubcheck was reported missing, and `verify` reported it `SKIPPED` and *passed*,
  so the single external gate on the output stopped running while the run still
  looked clean. `JOVEN_EPUBCHECK_JAR` now points at the jar and it runs under
  `java -jar`.

- **The review server answers a refused POST instead of resetting the
  connection.** `do_POST` sent 404 and 415 without reading the body it was
  refusing. Unread bytes left in the socket make Windows reset rather than close,
  so the client got `WinError 10053` where the status code should have been — the
  refusal arriving as a dropped connection rather than as a 415. Whether the reset
  beat the response out of the buffer was a race, so this also flaked rather than
  failing honestly. The content-type refusal that stops a page in another tab
  writing to the sidecar is unchanged; it just says so now.

- **Output is UTF-8 even when redirected.** A redirected stream on Windows falls
  back to the locale encoding, cp1252 on a stock install, so `matríz` written to a
  file came back as `matr?z`. Worse, `joven add` echoes the passage it matched, and
  `typer.echo` given a character outside cp1252 raises `UnicodeEncodeError` rather
  than degrading — after the sidecar has already been written. Accented Spanish is
  safe, since cp1252 covers it, and *The Crossing* contains no character outside
  it; that was luck rather than safety.

### Documentation

- The install guide covers the Windows route, and calls out the epubcheck jar step
  specifically, because skipping it does not fail loudly — it reports `SKIPPED`
  and passes.
- Development covers `Scripts\` rather than `bin/`, and the reinstall-while-running
  trap: Windows cannot replace an open file, so `pip install -e .` during a long
  `detect` leaves a half-uninstalled package and a venv that looks fine until it
  raises `ModuleNotFoundError`.
- A `.gitattributes` settles line endings in the repository rather than per clone.
  Not cosmetic here: the load-bearing invariant compares bytes, so a stray `\r` in
  a tracked fixture would look like the renderer corrupting the book.

### Infrastructure

- CI runs the suite on `windows-latest` at the Python floor and ceiling, five jobs
  in total. The Windows epubcheck step deliberately creates no launcher, so CI
  exercises the jar route a real user takes rather than a wrapper invented for its
  own convenience.

### Measured

A full run on Windows 11, against a different edition of *The Crossing* (149,995
words to the Knopf edition's 151,865) — RTX 4070 Ti SUPER, 32 GB, `qwen3:8b`:

| | macOS, Knopf edition | Windows, this edition |
|---|---|---|
| escalated to the LLM | 2,556 (21%) | 2,544 (21%) |
| footnotes produced | 726 | 731 |
| integrity checks | 12 of 12 | 12 of 12 |
| wall clock | 73 min | 12.2 min |

The escalation rate matches to within a percentage point and the footnote count to
within five on a book five thousand words shorter, which is the evidence that the
port changed behaviour nowhere. The wall clock is a GPU, not a platform: 0.3 s per
escalated call against 1.5 s on an M-series laptop.

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
