# Local model selection

Why `qwen3:8b` is the default Tier-2 model, and how to measure another.

- **Hardware for the benchmark:** Apple Silicon, 16 GB unified memory. Budget
  8–10 GB for the model, so 7–14B at 4-bit quantization.
- **Runner:** Ollama, `temperature=0`, JSON-schema-constrained output, the shipped
  few-shot examples.
- **Cases:** 27 hand-labelled — 19 Spanish, 8 English — drawn from *The Crossing*
  and weighted toward the band where statistical language ID is a coin flip.

Reproduce, or score a new candidate:

```bash
ollama serve &
python tools/bench_models.py                    # the LLM in isolation
python tools/bench_pipeline.py aya-expanse:8b   # the two-tier system that ships
```

---

## Result

| Model | Size | Pipeline classification | Span precision | False positives | s/case |
|---|---|---|---|---|---|
| **qwen3:8b** | 5.2 GB | **27/27** | 4/4 | **0** | **1.9** |
| gemma3:12b | 8.1 GB | **27/27** | 4/4 | **0** | 5.8 |
| aya-expanse:8b | 5.1 GB | 26/27 | 4/4 | 1 | 2.5 |

`qwen3:8b` scores top and is the fastest — three times quicker than `gemma3:12b`
for the same accuracy. `gemma3:12b` is the fallback if qwen3 ever regresses.

A one-case gap on 27 cases is within noise. The honest reading is "all three are
good enough; take the fastest that scores top", which is qwen3.

### Standalone, with no Tier 1 in front

| Model | Classification | False positives | False negatives |
|---|---|---|---|
| gemma3:12b | 25/27 | **0** | 2 |
| aya-expanse:8b | 24/27 | 1 (`matríz`) | 2 |
| qwen3:8b | 24/27 | 1 (dialect English) | 2 |

Standalone, `gemma3:12b` is the best classifier and the only one with no false
positives — the direction to err. That advantage disappears inside the pipeline,
because Tier 1 already neutralises the other models' false positives. It is the
model to reach for only if Tier 1 were ever removed.

## The tiers are complementary

Every model error in the benchmark is caught by the tier in front of it, which is
the empirical case for the two-tier design in [architecture.md](architecture.md):

| Case | LLM alone | Tier 1 | Pipeline outcome |
|---|---|---|---|
| `Dieciseis.` | both 8B models miss it | **SPANISH 1.00** | accepted at Tier 1, never reaches the LLM |
| `He dont remember so good sometimes…` | qwen3 false-positives | **ENGLISH 0.99** | rejected at Tier 1 |
| `The matríz will not help you…` | aya false-positives | **ENGLISH 0.99** | rejected at Tier 1 |
| `Tantos, said the man.` | correct, tag excluded | 0.50 → abstains | resolved by Tier 1 tag-strip (0.98) |
| `Cuántos años tienes? the old man said.` | span excludes the tag | 0.98 whole-string | escalated, LLM splits it correctly |

Tier 1 is good at isolated Spanish words with distinctive orthography and at
confidently dismissing English prose carrying loanwords. Tier 2 is good at mixed
sentences where a short Spanish utterance is welded to an English dialogue tag.
Neither is good at the other's job.

**Span precision** — excluding the English dialogue tag from the translated span
(`Cuántos años tienes?`, not `Cuántos años tienes? the old man said.`) — was the
risk most expected to fail. All three models scored 100% on it, standalone and in
the pipeline; the few-shot examples showing the exclusion do the work.

## What a small benchmark cannot see

Twenty-seven cases size a model well and size a book badly. Two conclusions drawn
from them were overturned by a full-book trace, and both are recorded so they are
not re-proposed:

- **The accept threshold cannot come down.** No true-English case in the
  benchmark scored above 0.58, which suggested 0.90 could drop to 0.75 and save a
  third of the model calls. At book scale, `Yessir.` sits at 0.70–0.75 and
  `Astrolabe or sextant.` at 0.75–0.80 — English that would be auto-accepted as
  Spanish where no model can veto it. It stays at 0.90.
- **Throughput.** The benchmark's escalation rate predicted about 450 model calls
  for the book. The real run escalated 2,556 of 12,302 segments: a lower *rate*
  spread over far more segments than the sample implied.

## Trying a larger model

`--backend openai` reaches any OpenAI-compatible local server, which is the way
to try a model Ollama does not have or a machine with more memory
([configuration.md](configuration.md)). Measured once: a 27B Qwen3 on
llama.cpp agreed with `qwen3:8b` on 112 of 118 verdicts over a 200-paragraph
slice of *The Crossing*, at 5.5 s a call against 0.3 s. The six disagreements
were genuinely ambiguous — `Vaquero.`, `Nuevo Mexico.`, `Momentito, she said.` —
so the larger model is a quality experiment rather than a speed one, and
`joven diff` is how to see what it changed.

## Rejected options

| Option | Why not |
|---|---|
| Argos Translate / OPUS-MT | Pure NMT: translates whatever it is handed. No detection, no span selection, no context. Would "translate" `Yes mam.` |
| Models above 14B on 16 GB | Exceed the memory budget; use the OpenAI backend on a machine that has it |
| Hosted APIs | Better on literary idiom, but a price and an API key on every run, and the book's text leaves the machine |
