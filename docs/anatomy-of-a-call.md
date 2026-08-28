# Anatomy of one Tier-2 call

What the local model is actually asked, what it is allowed to answer, and what
happens to its answer afterwards. The [README](../README.md#how-it-works) covers
the shape of a call and why there are two prompts; this is the detail underneath.

For why the pipeline is built this way at all, see [DESIGN.md](../DESIGN.md).

---

## The request

One `POST /api/chat` to a local Ollama server. Nothing leaves the machine, and a
run costs nothing.

```jsonc
{
  "model": "qwen3:8b",
  "messages": [ /* system, 6 few-shot pairs, then the paragraph — below */ ],
  "format": { /* JSON schema, so the reply shape is guaranteed */ },
  "stream": false,
  "think": false,                              // qwen3 reasons by default; not wanted here
  "options": { "temperature": 0, "num_predict": 400 }
}
```

Three of those settings are doing real work:

- **`format`** is a JSON schema requiring `is_spanish`, `spanish_text`, and
  `translation`. The model cannot reply with prose, an apology, or a code fence,
  which removes an entire class of parsing failure rather than defending against
  it downstream.
- **`temperature: 0`** makes a re-run reproducible, which is what allows a trace
  to be treated as evidence.
- **`think: false`** because `qwen3` is a reasoning model by default, and the
  thinking tokens are latency spent on a question that few-shot examples already
  answer.

## What the model receives

Six few-shot pairs, then the paragraph. The examples are not decoration — they
carry most of the span precision (see
[model-selection.md](model-selection.md)), and the last two exist because a
full-book run caught the model rewriting McCarthy's dialect into standard English
and calling the result a translation.

```
system     You analyse single paragraphs from an English-language novel set on
           the US-Mexico border in the 1930s... The novel's English is
           deliberately dialectal. **That is English.** Rewriting it into
           standard English is NOT translation.

user       Vaya con Dios.                          ─┐  A: whole-paragraph Spanish
assistant   {"is_spanish": true, "spanish_text":    │
             "Vaya con Dios.", "translation":       │
             "Go with God."}                        │
user       Cuántos años tienes? the old man said.   │  B: Spanish + English tag
assistant   {"is_spanish": true, "spanish_text":    │     — the span stops before
             "Cuántos años tienes?", ...}           │       the tag
user       The matríz will not help you, ...        │  C: loanword in English prose
assistant   {"is_spanish": false, ...}              │
user       Go on.                                   │  short English
assistant   {"is_spanish": false, ...}              │
user       Yessir.                                  │  dialect trap
assistant   {"is_spanish": false, ...}              │
user       He's done ate. She come up out of Mexico.│  dialect trap
assistant   {"is_spanish": false, ...}             ─┘

user       <context>
           ...up to 400 characters of the preceding paragraphs...
           </context>
           The context above is background only. Do NOT translate it and do
           NOT include any of it in your answer.

           <paragraph>
           Ella vino de Mexico, the boy said.
           </paragraph>
           Answer about the paragraph above, and nothing else.
```

### The context is load-bearing, and it bleeds

`Bastante.` means nothing in isolation. With the preceding paragraph attached,
the model can tell a Spanish utterance from an English word it does not
recognise — which is why up to 400 characters of prior prose ride along on every
call.

The cost is that the model will happily answer about the wrong thing. Observed on
the local model: with a context ending `...Se fué.`, the paragraph
`Ay. Ándale, joven.` came back as *"He went away. Come on, young man."* — a
translation of the **context**, not of the paragraph.

Hence the hard delimiters and the explicit refusal line. Both prompts build the
user turn through the same helper, so the fix cannot drift between them.

## Two prompts, and why

| Tier 1 said | Prompt | Because |
|---|---|---|
| abstained (the band) | **adjudicate** — is this Spanish, and if so which part? | The genuine open question |
| confident Spanish | **translate only** — this *is* Spanish; do not second-guess it | A combined prompt let the model veto Tier 1's correct calls. It labels `Dieciseis.` English. |

The obvious alternative — skip the model entirely when Tier 1 is already
confident — was the first implementation, and it shipped annotations with **no
translation at all**: blank popups on the device. So a confident Spanish segment
still costs an LLM call. It simply is not allowed to reopen the language
question, and it gets its own three-example few-shot set.

## One real call, start to finish

From the decision trace of a full-book run:

| | |
|---|---|
| paragraph | `Ella vino de Mexico, the boy said.` |
| Tier 1 | **ENGLISH, 0.59** → abstains (in band) |
| prompt | adjudicate |
| raw reply | `{"is_spanish": true, "spanish_text": "Ella vino de Mexico", "translation": "She came from Mexico"}` |
| latency | 2.2 s |
| outcome | annotated — the marker lands after `Mexico`, before the English tag |

Tier 1 leaned the *wrong way* on this one and was right not to commit to it. That
is the argument for a wide abstention band, in one row of trace.

## Three deterministic gates run after the model

The model is never the last word. Each of these exists because a traced run
showed a bad footnote reaching the device.

**1. Similarity veto.** If the translation is the source tidied up, the footnote
teaches the reader nothing:

| source | "translation" | ratio | |
|---|---|---|---|
| `Yessir.` | `Yes sir.` | 0.92 | vetoed |
| `She come up out of Mexico.` | `She came up out of Mexico.` | 0.96 | vetoed |
| `Bastante.` | `Enough.` | 0.14 | kept |

Dialogue tags are stripped from both sides before comparing, because McCarthy's
`, he said.` survives translation verbatim — it is shared text that was never
translated, and it once dragged **35 genuinely Spanish segments** over the
threshold.

**2. Embedded loanword.** A lone Spanish word inside English narration gets no
footnote — `You know what is orgullo?` is texture, not dialogue. The check that
matters runs first: a span of two or more words is never a loanword, however much
English surrounds it.

**3. Span narrowing.** The annotation is shrunk to the `spanish_text` the model
reported, so the marker lands at the end of the Spanish rather than after the
English dialogue tag.

Every one records its own outcome, so a suppression is visible rather than
silent:

```bash
joven explain trace.jsonl --outcome tier2_vetoed
joven explain trace.jsonl --outcome embedded_loanword
```

## Replaying the gates without re-running the book

Each escalated segment's trace record keeps the model's verbatim reply, so all
three gates can be re-evaluated offline against the whole corpus:

```bash
python tools/replay_gates.py trace.jsonl
```

That turns "did this threshold change help, and what did it break?" from a
73-minute run into a one-second question, answered against the real book rather
than invented cases. See [troubleshooting.md](troubleshooting.md) for when a full
re-run is still required.
