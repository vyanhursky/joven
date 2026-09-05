# Configuration, backends, and the sidecar commands

Where a setting comes from and which one wins; how to point Tier 2 at a model
server other than Ollama on localhost; how to run more than one paragraph at a
time; and the small commands for working on a sidecar without opening the review
page.

---

## Where settings come from

Highest first:

1. a command-line flag — `--model`, `--workers`, …
2. an environment variable — `JOVEN_MODEL`, `JOVEN_WORKERS`, …
3. `./joven.toml` in the working directory
4. the user config file: `~/.config/joven/config.toml`, or
   `%APPDATA%\joven\config.toml` on Windows
5. the built-in default

`joven config` prints every setting with its value and the source that set it,
which is the first thing to run when a run is not behaving the way you meant:

```bash
joven config
```

A setting is a flat key in the TOML file, spelled exactly as it is below:

```toml
# joven.toml — kept next to the books you are working on
model = "qwen3:8b"
workers = 4
ollama_url = "http://192.168.1.20:11434"
epubcheck_jar = "C:/tools/epubcheck-5.1.0/epubcheck.jar"
```

An unknown key, a value of the wrong type, or a file that does not parse is an
**error**, not a warning: a silently ignored `worker = 4` is a run four times slower
than you meant with nothing to say why.

`JOVEN_CONFIG=path/to/file.toml` reads *that* file instead of the two above — for
an experiment that should carry its own settings without touching the ones you
normally use.

| Setting | Default | What it is |
|---|---|---|
| `backend` | `ollama` | `ollama`, `openai`, `stub`, or `none` for Tier 1 only |
| `model` | `qwen3:8b` | The model tag, in whichever server's spelling |
| `ollama_url` | `http://localhost:11434` | Where Ollama listens |
| `base_url` | `http://localhost:8080/v1` | An OpenAI-compatible server, ending in `/v1` |
| `api_key` | *(empty)* | Bearer token, only for a server that insists on one |
| `workers` | `1` | Paragraphs in flight at once — see below |
| `epubcheck_jar` | *(empty)* | Path to `epubcheck.jar` when there is no launcher on `PATH` |
| `context_chars` | `400` | Preceding prose handed to the model with each paragraph |
| `accept_spanish` | `0.90` | Tier-1 thresholds. The defaults are the measured ones — |
| `reject_english` | `0.90` | [DESIGN.md §2](../DESIGN.md) and |
| `accept_spanish_stripped` | `0.95` | [model-selection.md](model-selection.md) say why each |
| `similarity_veto` | `0.75` | sits where it does. Move them for an experiment, then replay the gates. |

The `[config: …]` markers in `joven detect --help` show which flags fall back this
way.

## Backends

### Ollama

The default, and the one every published measurement was made with. `detect`
checks that the server answers and that the model is pulled before it starts, so
a typo in the tag fails in a second rather than after the first paragraph.

```bash
joven detect book.epub --ollama-url http://192.168.1.20:11434
```

### Any OpenAI-compatible server

`--backend openai` speaks the chat-completions protocol that llama.cpp's server,
LM Studio, vLLM and most local model runners expose. Same prompt, same few-shot
examples, same JSON contract — only the wire format differs, and the run is as
offline as the server is.

```bash
# llama.cpp server, or a router in front of several of them
joven detect book.epub --backend openai --base-url http://localhost:8081/v1 --model qwen3.8-27b

# LM Studio's default port
joven detect book.epub --backend openai --base-url http://localhost:1234/v1 --model qwen3-8b
```

Two request features vary between servers, and the backend learns what yours
supports rather than assuming:

- **Structured output.** It asks for a strict `json_schema` first, which llama.cpp,
  LM Studio and vLLM honour. A server that answers 400 gets `json_object` and the
  schema already in the prompt.
- **Thinking off.** It sends `chat_template_kwargs: {"enable_thinking": false}`,
  which is how llama.cpp and vLLM silence a Qwen3-style reasoning model. A server
  that rejects the field gets the request without it, and any `<think>` block that
  arrives in a reply is stripped before parsing.

Each is dropped after its first refusal and stays dropped, so a book costs at most
two extra requests to find out.

`api_key` exists for a server that requires a bearer token. Prefer `JOVEN_API_KEY`
to `--api-key`, which lands in your shell history. A hosted endpoint works
mechanically over this protocol, but it is not a supported path: the design is
offline end to end, and using one sends the book's paragraphs off your machine.

### Resuming under a different model

`--resume` reuses recorded answers by segment text and Tier-1 verdict, not by
model. Resume a run under a different `--model` and it says so, then reuses them
anyway — a mixed sidecar is usually what you want after switching models
mid-book, and the trace records which model answered each segment. Re-run without
`--resume` to measure a model on its own.

## Workers

```bash
joven detect book.epub --workers 4
```

Scans four paragraphs at once. The trace, the sidecar and the progress bar still
see paragraphs in book order: the model may be asked about paragraph 12 before 11
has answered, but nothing is written down until 11 has, so `--resume` and
`joven explain` behave exactly as with one worker.

**Ollama serialises requests unless told otherwise.** Set `OLLAMA_NUM_PARALLEL=4`
in the environment Ollama runs in, or `--workers` waits in a queue and looks
broken. llama.cpp's server needs `--parallel 4` for the same reason; a router in
front of one instance with `--parallel 1` gains nothing from workers above 1.

The default stays at 1 so that a run behaves exactly as every measurement in
these docs describes until you ask otherwise.

## The progress bar

`detect` shows paragraphs done, escalations, footnotes so far, the last model
latency and an estimate of the time left. It goes to stderr, disappears when the
run finishes, and is suppressed when stderr is not a terminal; `--quiet` turns it
off explicitly. Nothing else about the output changed.

## Scoping a scan

Iterating on a prompt or a threshold against a real model does not need the whole
book:

```bash
joven detect book.epub --href OEBPS/part2.xhtml      # one spine document
joven detect book.epub --range 100:300              # paragraphs 100–299
joven detect book.epub --limit 200                  # the first 200, as before
```

`--range` counts the way `--limit` does, after any `--href` filter, so
`--limit N` and `--range 0:N` agree. Either side may be left off: `--range 500:`.

## The sidecar commands

Everything the review page does to a single annotation, from the shell:

```bash
joven status annotations.json --epub book.epub   # counts, flags, model, does it match this book
joven reject annotations.json --find "No suh"     # not Spanish; never render it
joven reset  annotations.json --find "No suh"     # undo that — back to auto
joven diff   before.json after.json               # what a re-detection changed
```

`reject` and `reset` take `--find TEXT` (a substring of the annotated paragraph;
it must match exactly one) or `--id`. A rejected id is never re-added by
re-detection, however confident the detector is; `reset` makes it eligible again.
Resetting an *edited* annotation keeps the edited wording until the next `detect`
overwrites it — there is no earlier wording to return to.

`diff` reports, by annotation id, what appeared, what vanished, which translations
read differently and which statuses changed. It is the question to ask after
re-running with a different model or threshold, and the reason ids are content
hashes rather than positions.
