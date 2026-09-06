# Roadmap

What is done, what is deferred, and what is deliberately not planned. Design
notes live with the item so the work can start from this page.

> This page may describe commands and flags that do not exist yet. It is
> deliberately *not* in the list `tools/check_docs.py` verifies.

---

## Done

- **Small gaps** — review-page focus, the different-file warning on `render` and
  `review`, `--ollama-url`, mypy in CI, GitHub releases from tags.
- **Finish the run** — a progress bar, `--workers`, `--backend openai` for any
  OpenAI-compatible local server, `joven.toml` and `JOVEN_*` configuration, the
  sidecar commands `reject` / `reset` / `status` / `diff`, and `--href` / `--range`.
- **The browser UI** — `joven ui`: the whole workflow in one page, with span
  editing, cancel and resume, and a trace browser. See
  [browser-ui.md](browser-ui.md). Building it against a real library turned up
  `joven strip` and the refusal to render onto a Joven output.

Two findings from those runs worth carrying forward:

- With Ollama at its defaults, `--workers 4` gained only 12% because the server
  queued the requests. The ordering guarantee held; `OLLAMA_NUM_PARALLEL` is the
  user's side of the bargain, and the docs say so.
- A 27B Qwen3 on llama.cpp agreed with `qwen3:8b` on 112 of 118 verdicts at
  5.5 s a call — a quality experiment, not a speed one.

---

## Deferred: sharing annotations as a portable pack

**The idea:** publish the finished sidecars for the Border Trilogy so another
reader with the same edition can render footnotes without a model, a GPU, or an
hour.

**Why it is deferred.** `annotations.json` *is* book content: every annotation
carries `source_text`, the whole paragraph — for *The Crossing*, 726 paragraphs of
McCarthy verbatim. The repository already treats sidecars as book content in
`.gitignore` and `tools/check_no_book_content.py`. Shipping one would distribute a
substantial excerpt of the novel. The translations themselves are footnote-length
renderings of Spanish phrases, a far smaller exposure but still a derivative
of the text; this is not legal advice, and the call is the author's.

**If it is picked up**, the shape is a pack format that carries the decisions
and none of the prose, plus an importer that rebuilds the sidecar against the
reader's own copy:

| Field | Kept? | Why |
|---|---|---|
| `id` | yes | Already a content hash; identifies the paragraph without containing it |
| `href`, `para_index`, `occurrence` | yes | Where to look |
| `spans`, `marker_offset` | yes | Offsets, meaningless without the paragraph |
| `translation`, `status`, `model` | yes | The work being shared |
| `source_text`, `spanish_text` | **no** | The prose |

Plus an edition fingerprint: title, the EPUB's SHA-256, and a per-document hash
of the normalised text so a Calibre re-save still matches. Import walks the
reader's book, recomputes ids, matches, and rebuilds `source_text` from their
file; spans are stored against normalised text and re-mapped. Publish from a
separate repository with a clear notice, never document DRM removal, and extend
`check_no_book_content.py` to fail on a pack containing prose. Today's id
includes `href`, so two layouts of the same text will not match; a pack id that
hashes text and a book-wide occurrence is the v2 if the first packs show
mismatches.

---

## Ideas with groundwork done

**Kobo highlight round-trip.** Highlighting a missed passage on the device could
be the bug report: `.kobo/KoboReader.sqlite` on the mounted Kobo holds every
highlight's text in the `Bookmark` table (`Type='highlight'`; `Annotation`, the
typed note, is otherwise unused and would carry a marker such as `x`). Confirmed
on firmware 4.45.23697: `StartContainerPath` and `ContentID` are kepubify
artefacts, so matching must be by normalised text with a similarity threshold,
never by path, and highlights can be one character long. The command would read
the table, keep marked highlights for this book, fuzzy-match them to paragraphs,
translate just those, and append them as `edited`. `joven add --find` covers the
same need with a phrase you type yourself, which is why this has not been built.

**Other language pairs.** The detector languages, both prompts, the few-shot
sets and the speech-verb vocabulary are Spanish and McCarthy specific.
Parametrising them behind a `--language` option opens up Hemingway's Spanish and
Italian, Tolstoy's French, Eco's Latin; the Latin veto generalises to "any third
language".

**Other readers.** Apple Books already works. Verifying Kindle via Calibre
conversion, with a `--target` that skips `kepubify`, would widen the audience.

**A public-domain fixture** with real Spanish in it would let the `realbook`
tests run in CI without a copyrighted book.

---

## Not planned

- **A hosted-API backend.** The design is offline end to end; the OpenAI
  protocol is supported for local servers only.
- **A terminal UI.** Reviewing prose is a browser job, and the browser page
  exists.
- **A Homebrew formula.** `lingua` ships no source distribution; see
  [releasing.md](releasing.md).
- **A Calibre plugin.** The largest possible audience, but Calibre embeds its own
  Python and the `lingua` wheel is 170 MB — a packaging project before a feature.
