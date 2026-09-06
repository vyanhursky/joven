# Changelog

Release notes. The GitHub release for each tag takes its text from the matching
section here, so every version has one.

## Unreleased

### Added

- **`joven ui`** — the whole workflow in one local page: drop an EPUB, detect with
  live progress and a Cancel that keeps every answer already given, review with
  span editing, render and verify with one button, download the KEPUB, browse the
  decision trace. Books live under `~/.joven/books/<sha256>/` with their sidecar,
  trace and outputs, so the command line can pick up where the page left off.
  See [docs/browser-ui.md](docs/browser-ui.md).
- **`joven strip`**, and `render` refuses a book that already carries Joven
  footnotes — rendering onto one doubled every marker while looking plausible.
  `inspect` shows the marker count; `detect` warns and carries on.
- **A progress bar** on `detect`: paragraphs, escalations, footnotes, last model
  latency, time left. Hidden when stderr is not a terminal; `--quiet` turns it off.
- **`detect --workers N`.** Paragraphs are scanned concurrently and finished in
  book order, so the trace, `--resume` and the sidecar are identical at any worker
  count. Ollama needs `OLLAMA_NUM_PARALLEL` to match or the requests queue.
- **`detect --backend openai`** for any server speaking the OpenAI chat protocol —
  llama.cpp, LM Studio, vLLM — with the same prompt, few-shots and JSON contract.
  Strict `json_schema` and the `enable_thinking` switch are learned from the
  server's first refusal rather than assumed.
- **Configuration.** `joven.toml`, a user config file and `JOVEN_*` variables, in
  the order flag › environment › project file › user file › default. `joven config`
  shows every value and its source. Unknown keys and wrong types are errors.
  `JOVEN_EPUBCHECK_JAR` is unchanged.
- **`joven reject`, `reset`, `status`, `diff`**, and `detect --href` / `--range`.
- **`detect --ollama-url`**, also `JOVEN_OLLAMA_URL`.
- Review page: `j` / `k` move between cards, Ctrl-Enter saves an edit, and the
  review API accepts new spans.

### Fixed

- The review page kept keyboard focus after a decision; the next `a` used to hit
  the first card on the page.
- `render` and `review` warn when a sidecar was detected from a different file.

### Infrastructure

- mypy runs in CI with `lxml-stubs`.
- A GitHub release is created from every tag, with the wheel and sdist attached
  and this file's section as the notes. A tag with no section fails before the
  PyPI upload.

### Measured

A Vintage edition of *The Crossing* (160,262 words, 13,878 segments) on an RTX
4070 Ti SUPER with `qwen3:8b`, Ollama at its defaults:

| | one worker | `--workers 4` |
|---|---|---|
| escalated to the LLM | 3,046 (22%) | 3,046 |
| footnotes | 735 | 734 |
| trace order | — | identical |
| wall clock | 15.2 min | 13.4 min |

The small gain is Ollama queueing the requests (`OLLAMA_NUM_PARALLEL` unset).
*Cities of the Plain*, 91,326 words, through the browser UI end to end: 2,272
escalations, 343 footnotes, 9 minutes with two workers, all 12 checks passed.

## v1.0.0b4 — 2026-09-02

Windows is a supported platform. Fixed: external tools run by resolved path, so
`.cmd` launchers work; epubcheck is found via `JOVEN_EPUBCHECK_JAR` when there is
no launcher; the review server answers a refused POST instead of resetting the
connection; output is UTF-8 even when redirected. CI runs on Windows and macOS.
Measured: a full run of *The Crossing* on Windows matched the macOS run to within a
percentage point of escalations and five footnotes, in 12.2 minutes on a GPU.

## v1.0.0b3 — 2026-08-28

Two more books through the pipeline, *All the Pretty Horses* and *Suttree*. Fixed:
the EPUB 2 → 3 upgrade takes the identifier `@unique-identifier` names, not the
first one; Latin is no longer annotated as Spanish (a Tier-1 veto on the accept
path, and the prompt says so); `--resume` checks the Tier-1 verdict before reusing
an answer. The *Suttree* control run went from six false positives to two in
177,257 words.

## v1.0.0b2 — 2026-08-28

Fixed: books using HTML named entities parse; font obfuscation is no longer
reported as DRM; an unparseable document is an error, not a traceback. Added:
releases publish to PyPI from a tag via Trusted Publishing, and `detect --resume`
picks up an interrupted run from its trace.

## v1.0.0b1 — 2026-08-19

First public release. Finds untranslated Spanish in an English EPUB and inserts
tappable translation footnotes, prose byte-for-byte unchanged, entirely offline
against a local model. Verified on *The Crossing*: 12,302 segments, 2,556 model
calls, 726 footnotes, 12 of 12 integrity checks, device-verified on a Kobo.
