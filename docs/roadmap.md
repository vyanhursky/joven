# Roadmap

What comes next, as a sequence of pull requests. Each entry says what it is for,
how it should be built, and what would make it done — so the work can start from
this page rather than from a conversation nobody can find later.

The design principles from [DESIGN.md](../DESIGN.md) still bind every item here:
the prose is never edited, the sidecar is the source of truth, the book never
leaves the machine, and a spurious footnote is worse than a missed one.

> This page describes commands and flags that do not exist yet. It is deliberately
> *not* in the list `tools/check_docs.py` verifies, so it may sketch freely; when an
> item ships, its documentation moves to the README or `docs/` and gets checked.

---

## PR 1 — Small gaps *(this branch)*

Bugs and omissions found by reading the code cold after the Windows port. None
showed on a green suite.

- [x] The review page loses keyboard focus after every decision, so the next `a`
      re-approves the first card on the page. Focus now follows the annotation;
      `j`/`k` move; Ctrl-Enter saves an edit.
- [x] `detect` records the source's SHA-256 and nothing reads it back. `render` and
      `review` now say when a sidecar came from a different file.
- [x] The Ollama URL is a constant. `--ollama-url` and `JOVEN_OLLAMA_URL`.
- [x] No type checker. `mypy` with `lxml-stubs` runs in CI; 21 Optional narrowings.
- [x] Tags publish to PyPI but create no GitHub release. A `release` job builds one
      from the CHANGELOG section, and a tag without a section fails before upload.

Deferred from the gap list to PR 3, where it belongs: **editing a span** in the
review page. Today a wrong span can only be rejected or fixed through `joven add`.

---

## PR 2 — Finish the run *(done)*

Three things that make a 73-minute silent run into a short visible one against
whichever local server is already running, plus the configuration and command
surface they need. Shipped as described below; the measured run is in the
CHANGELOG. One finding worth carrying forward: with Ollama at its defaults,
`--workers 4` gained only 12% because the server queued the requests — the
guarantee that mattered (identical trace order) held, and `OLLAMA_NUM_PARALLEL` is
the user's side of the bargain.

### 2a. Progress

`detect()` in `detect/pipeline.py` gains an `on_progress` callback carrying a small
frozen dataclass: paragraphs done and total, segments seen, escalations, annotations
so far, errors, elapsed, and the last model latency. The total is known up front by
counting text units across the spine before the loop starts, which is cheap.

The CLI renders it with `rich`, which Typer already installs, as one progress bar
plus a one-line status: `2,556 escalated · 726 annotated · 0.3 s/call · ETA 4 min`.
`--quiet` turns it off for logs. The trace is untouched.

### 2b. Parallelism

A bounded pool of workers over the abstention band. Design constraints:

- **Paragraph is the unit.** Calls inside a paragraph stay sequential, because they
  share one context window and the merge step needs them all.
- **The trace stays in book order.** Submit N paragraphs ahead and consume results
  in order, so `trace.jsonl` and `--resume` behave exactly as they do today. The
  `Tracer` is written from the consuming thread only.
- **`--workers N`, default 1.** The default preserves current behaviour; the docs
  recommend 4 with `OLLAMA_NUM_PARALLEL=4`, because Ollama serialises requests
  otherwise and the flag would look broken.
- `httpx.Client` is thread-safe, so one `OllamaTranslator` serves all workers.

Done when a full run on the RTX 4070 Ti is measured with `--workers 4` and the
wall clock goes into the CHANGELOG next to the 12.2-minute single-threaded figure.

### 2c. An OpenAI-compatible backend

`--backend openai --base-url http://localhost:8081/v1 --model <name>`. This covers
llama.cpp's server, LM Studio, vLLM, and a llama.cpp router, all local — the design's
"offline, $0" stance is unchanged; only the wire protocol is added.

- `POST /v1/chat/completions` with `response_format` as `json_schema` and `strict`
  set, which llama.cpp, LM Studio and vLLM all honour. Fall back to `json_object`
  plus the schema in the system prompt when a server rejects `json_schema`.
- Reasoning models: there is no `think: false` in this protocol. For Qwen3 on
  llama.cpp, `chat_template_kwargs: {"enable_thinking": false}` works; test it, and
  fall back to `/no_think` in the system prompt.
- Move the message builder off `OllamaTranslator` to module level so both backends
  share the prompt, the few-shots and the context fence — the same reason
  `_user_turn` is already shared.
- `tier2_model` in the trace becomes `openai:<model>`. `--resume` matches on text
  and Tier-1 verdict, not on model, so a resumed run under a *different* model
  silently reuses the old model's answers. Either warn or add a strict mode.
- `JOVEN_API_KEY` for servers that require a bearer token. Document that a hosted
  endpoint works mechanically but is not a supported path, and that using one sends
  the book's paragraphs off the machine.
- `Verdict.cost_usd` exists and is always zero. Fill it from `usage` when a server
  returns pricing, or remove it; do not leave it decorative.

### 2d. Configuration

Precedence, highest first: CLI flag → `JOVEN_*` environment variable →
`./joven.toml` → `~/.config/joven/config.toml` (`%APPDATA%\joven\config.toml` on
Windows) → built-in default.

Keys: `backend`, `model`, `ollama_url`, `base_url`, `workers`, `epubcheck_jar`,
`context_chars`, and the thresholds (`accept_spanish`, `reject_english`,
`accept_spanish_stripped`, `similarity_veto`). Thresholds in a config file are for
experiments; the shipped defaults stay the measured ones, and changing them should
print the value and its source so a stale experiment cannot masquerade as the
default.

Implementation: a frozen dataclass in `joven/config.py`; Typer options default to
`None` and fall through to it. `joven config` prints the effective configuration with
the source of each value. The model tag currently appears as a literal in `cli.py`
and as `DEFAULT_MODEL` in `translate.py`; both move here.

### 2e. CLI completeness

| Command | Job |
|---|---|
| `joven reject annotations.json --find TEXT` | The counterpart of `add`: mark an annotation rejected without opening the review page |
| `joven reset annotations.json --find TEXT` | Return an annotation to `auto`, undoing a decision |
| `joven status annotations.json [--epub book.epub]` | Counts by status, flagged count, models used, title, and whether the sidecar matches the given book |
| `joven diff old.json new.json` | Added, removed, and changed translations or statuses, by id — the question after re-detecting with a new model or threshold |
| `joven detect --href OEBPS/part2.xhtml` and `--range 100:300` | Scan one document or a paragraph range, alongside `--limit`, for iterating against a real model on one chapter |

All of these are sidecar operations already implemented as methods on `Sidecar` and
`Annotation`; the work is the command surface and its tests, plus `check_docs.py`
coverage once documented.

---

## PR 3 — Joven in the browser

**The question:** instead of a TUI, can the whole workflow — choose a book, detect,
review, render, verify — live in one page launched by one command?

**The answer is yes, and the review page is already a third of it.** The rest is a
job runner and a few more routes on the same server.

### Why not a TUI

Review means reading paragraphs of prose and editing free text, which a browser does
better than any terminal widget, and the existing page is offline, dependency-free
and device-tested. A terminal dashboard for the long-running `detect` is a real want,
but the browser page in this PR covers it too. The TUI is not planned.

### Architecture

Three options were weighed:

| Option | Verdict |
|---|---|
| **A. Extend the stdlib `http.server` that serves the review page** | **Recommended.** Zero new dependencies, offline by construction, and the review page proves the pattern. The cost is a hand-rolled job runner, which is a hundred lines. |
| B. Add FastAPI or Flask | Richer, but adds dependencies to a tool whose install path is one `uv tool install`, for a single-user localhost page |
| C. Textual TUI | Rejected above |

`joven ui` starts the server on `127.0.0.1`, opens the browser, and serves one
static page with vanilla JavaScript and no build step. Every action calls the same
functions the CLI does — `detect()`, `render_epub()`, `run_verify()` — so the CLI
remains the primary interface and the page cannot drift from it.

### Choosing the book

A browser cannot hand a file path to a local server, so two routes, both needed:

1. **Drop the EPUB on the page.** The server stores it under a working directory
   keyed by its SHA-256 — `~/.joven/books/<sha>/` — alongside its sidecar, trace and
   outputs. This is the obvious first-run experience.
2. **Type or browse a path.** For a large library, a path box with completion over
   a configured books directory (PR 2d) avoids copying files around.

### Jobs

One job at a time, since Ollama is the bottleneck anyway. A job is a thread running
`detect()` with the PR 2a progress callback feeding an in-memory status the page
polls once a second (server-sent events are a later nicety). Cancel is a cooperative
flag checked once per paragraph. Because the trace is flushed per record, a crashed
or restarted server resumes with `--resume` semantics for free, and a page reload
re-syncs from the server rather than losing anything.

### Pages

- **Book** — `inspect` output, the DRM check, and the `scan_damage.py` report, so a
  bad source is caught before an hour is spent on it.
- **Detect** — settings from the config, the live progress, and at the end the same
  band samples and rejections the CLI prints.
- **Review** — the existing page, embedded, plus **span editing**: select text in
  the paragraph to set the Spanish run, validated server-side with the existing
  `validate_against`, saved as `edited`. This is the deferred item from PR 1 and the
  highest-value review feature.
- **Render & verify** — one button, the 12 findings, and links to the `.epub` and
  `.kepub.epub`.
- **Trace** — `explain` as a page: filter by outcome, search text, and a detail pane
  with the raw model reply. This replaces the grep-and-jq workflow for tuning.

### Security posture

Bind to loopback only, as now. Keep the JSON content-type check that stops a page in
another tab from posting into the sidecar. Add a per-session random token that the
page sends with every mutating request, because the server will now also write
uploaded files and start jobs. Never serve the working directory as static files.

### Open questions

- Should uploads be the default even when the book is already on disk? Copying a
  300 KB EPUB is cheap; the answer is probably yes for simplicity.
- Single HTML file with inline script, as today, or a small `static/` package
  directory once the page has five tabs? Split when the file passes ~1,500 lines.
- Whether `joven review` remains a separate command or becomes `joven ui --review`.

---

## PR 4 — Sharing annotations: a portable pack

**The idea:** publish the finished `annotations.json` for the Border Trilogy so
another reader with the same edition can render footnotes without a model, a GPU,
or a 73-minute run.

**The premise needs one correction first.** `annotations.json` *is* book content.
Every annotation carries `source_text`, the whole paragraph it sits in — for
*The Crossing* that is 726 paragraphs of McCarthy verbatim. The repository already
knows this: `.gitignore` excludes `annotations*.json` under the heading "Book
content: NEVER commit", and `tools/check_no_book_content.py` exists to enforce it.
Shipping the sidecar as-is would distribute a substantial excerpt of the novel,
which is exactly what the project has been careful never to do.

So the feature is not "attach the sidecar to a release". It is **a pack format that
carries the decisions and none of the prose**, and an importer that rebuilds the
sidecar against the reader's own copy.

### What a pack holds

| Field | Kept? | Why |
|---|---|---|
| `id` | yes | Already a content hash of (href, normalised paragraph, occurrence) — it identifies the paragraph without containing it |
| `href`, `para_index`, `occurrence` | yes | Where to look, and a fast pre-check |
| `spans`, `marker_offset` | yes | Offsets; meaningless without the paragraph, so they reveal nothing |
| `translation`, `status`, `model` | yes | The work being shared |
| `source_text` | **no** | The prose |
| `spanish_text` | **no** | Derivable from the spans; also a quotation |

Plus an edition fingerprint at the top: title, the EPUB's SHA-256, and a per-document
hash of the *normalised text*, so a copy re-saved by Calibre — different bytes, same
words — still matches and a different edition is diagnosed before anything else.

### Import

`joven import pack.json --epub book.epub -o annotations.json` walks the reader's
book, computes `annotation_id` for every paragraph exactly as `detect` does, and
matches ids. A match rebuilds the full annotation with `source_text` taken from
*their* file; a miss is counted and listed. One subtlety: ids normalise whitespace,
but spans are offsets into the raw text. Store spans against the normalised text in
the pack and re-map on import, and the same edition survives whitespace differences
in the markup.

`joven export annotations.json --pack out.jovenpack.json` is the reverse, and
`check_no_book_content.py` should learn to fail on any pack containing
`source_text` or `spanish_text`, so the guard culture extends to the new artefact.

### On copyright, plainly

This is not legal advice, and the author should make the call. What can be said
from the code:

- The **paragraphs** are the book, and a pack must not contain them. That is the
  whole point of the format.
- The **translations** are footnote-length renderings of Spanish phrases, produced
  by a local model and reviewed by hand. That is a far smaller exposure than the
  prose, but a translation of a copyrighted text is still a derivative of it. A
  separate repository with a clear notice and a willingness to take a pack down is
  the sensible posture.
- The pack applies only to a copy the reader already has. Keep the project's
  existing line — it reads a file you own, unlocks nothing — and do not document or
  link DRM removal anywhere, including in the packs' README.

### Publishing

A **separate repository** (`joven-packs` or similar) with one release per book and
edition, not the tool's own releases, so the tool's release notes stay about the
tool and a pack can be withdrawn without touching a version of the software.

Edition reality: *The Crossing* exists as many EPUBs — Knopf via Calibre, Vintage,
retail conversions — and today's `annotation_id` includes `href`, so two files with
identical text but different internal layout will not match. A **pack id** that
hashes paragraph text and a book-wide occurrence number, ignoring `href`, would let
one pack serve every layout of the same text. That is a v2 of the id, worth doing
only if the first packs show mismatches in practice.

### What a reader gets

With a matching edition: `uv tool install`, `joven import`, `joven render`, copy to
the Kobo. No Ollama, no model download, no GPU, no hour. They still need `kepubify`
for the Kobo file, and `epubcheck` only if they want `verify`.

---

## Not planned

Recorded so they are not re-proposed without the constraint attached.

- **A hosted-API backend.** Rejected in DESIGN.md §4.3 and still rejected. PR 2c
  adds a protocol, not a policy change.
- **A TUI.** See PR 3.
- **A Homebrew formula.** `lingua` ships no source distribution;
  see [releasing.md](releasing.md).
- **A Calibre plugin.** The largest possible audience, but Calibre embeds its own
  Python and the `lingua` wheel is 170 MB; a packaging project before it is a
  feature. Revisit after PR 4, when a pack-only mode needs neither `lingua` nor a
  model.
