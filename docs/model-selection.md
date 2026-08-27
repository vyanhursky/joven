# Local model selection

Which local model ships as the default Tier-2 adjudicator. Measured, not guessed.

- **Hardware:** Apple Silicon (arm64), 16 GB unified memory. Budget ~8–10 GB for
  the model, so 7–14B at 4-bit quantization.
- **Runner:** Ollama 0.32.9, `temperature=0`, JSON-schema-constrained output,
  4 few-shot examples.
- **Benchmark:** `tools/bench_models.py` (LLM alone) and
  `tools/bench_pipeline.py` (the two-tier system that actually ships).
- **Cases:** 27 hand-labelled — 19 Spanish, 8 English — drawn from the real book
  and deliberately weighted toward the adversarial band in DESIGN.md §2.2.

Reproduce:

```bash
ollama serve &
python tools/bench_models.py                 # LLM in isolation
python tools/bench_pipeline.py aya-expanse:8b  # the shipping pipeline
```

---

## Result: `qwen3:8b` is the default

| Model | Size | Pipeline classification | Span precision | False positives | s/case |
|---|---|---|---|---|---|
| **qwen3:8b** | 5.2 GB | **27/27 (100%)** | 4/4 (100%) | **0** | **1.9** |
| gemma3:12b | 8.1 GB | **27/27 (100%)** | 4/4 (100%) | **0** | 5.8 |
| aya-expanse:8b | 5.1 GB | 26/27 (96%) | 4/4 (100%) | 1 | 2.5 |

`qwen3:8b` scores top **and** is the fastest — a 3× latency advantage over
gemma3 for the same accuracy. `gemma3:12b` is the fallback if qwen3 ever
regresses; `aya-expanse:8b`'s single miss is the `A Casas Grandes` case below.

`s/case` is from the standalone run, where every case hits the LLM. Projected
full-book escalated pass: ~14 min (qwen3), ~19 min (aya), ~43 min (gemma3) — all
$0. The measured qwen3 run came in at **73 minutes**; see the throughput note below
for why the projection was low.

### Standalone scores (LLM with no Tier 1 in front of it)

| Model | Classification | Span | False pos | False neg | s/case |
|---|---|---|---|---|---|
| gemma3:12b | 25/27 (93%) | 11/11 (100%) | **0** | 2 | 5.8 |
| aya-expanse:8b | 24/27 (89%) | 12/12 (100%) | 1 (`matríz`) | 2 | 2.5 |
| qwen3:8b | 24/27 (89%) | 12/12 (100%) | 1 (dialect English) | 2 | 1.9 |

Standalone, **gemma3:12b is the best classifier and the only one with zero false
positives** — a conservative error profile (misses Spanish rather than annotating
English), which is the direction to err. That advantage disappears inside the
pipeline, because Tier 1 already neutralises the other models' false positives.
gemma3 is therefore the model to reach for *only if Tier 1 were ever removed*.

> ⚠️ **A 1-case gap on 27 cases is within noise.** The honest reading is "all three
> are good enough; take the fastest that scores top", which is qwen3. Do not treat
> the ordering as settled until the ~250-case golden set exists (M2).

### Revision: the `A Casas Grandes` label changed the ranking

Originally `A Casas Grandes, said Billy.` was labelled Spanish, which made aya the
only model to "get it right" and put it at 27/27. On review the case was
**relabelled English** — `A Casas Grandes` is "To Casas Grandes", a bare place
name behind a preposition, and a footnote there tells the reader nothing.

This is recorded because it flipped the recommendation from aya to qwen3, and
that kind of thing should be visible rather than quietly rewritten. The decision
is encoded **only as a benchmark label — there is no place-name detection code**.
The case escalates to Tier 2 like any other and the model decides; qwen3 and
gemma3 call it English unprompted.

`aya-expanse` is Cohere's explicitly multilingual model, which is presumably why
it was expected to edge the others on Spanish fragments. On the corrected labels it
does not: qwen3 matches gemma3 on accuracy and beats both on speed.

**Throughput, predicted and measured.** At 2.5 s/case this benchmark predicted
roughly **450 LLM calls and ~19 minutes** for the full book, extrapolating a 37 %
escalation rate from 27 paragraphs. The real run escalated **2,556 of 12,302
segments (21 %)** and took **73 minutes** — a lower escalation *rate* spread over
far more segments than the small sample implied. Same lesson as the threshold
correction below: 27 cases size a model well and size a book badly. With the
on-disk cache, re-runs are instant.

---

## The finding that matters: the tiers are complementary

Standalone, the 8B models score 89 % (24/27) with two false negatives and one
false positive each; gemma3:12b manages 93 %. In the two-tier pipeline all three
land at 96–100 % with **zero false positives**. The gain is not noise — Tier 1
systematically covers Tier 2's exact failure modes:

| Case | LLM alone | lingua (Tier 1) | Pipeline outcome |
|---|---|---|---|
| `Dieciseis.` | ✗ both 8B models miss it | **SPANISH 1.00** | ✓ accepted at Tier 1, never reaches the LLM |
| `She touched her temple. He dont remember so good sometimes…` | ✗ qwen3 false-positives | **ENGLISH 0.99** | ✓ rejected at Tier 1 |
| `The matríz will not help you…` | ✗ aya false-positives | **ENGLISH 0.99** | ✓ rejected at Tier 1 |
| `Tantos, said the man.` | ✓ correct, tag excluded | 0.50 — abstains | ✓ resolved by Tier 1 tag-strip (0.98) |
| `Cuántos años tienes? the old man said.` | ✓ span excludes the tag | 0.98 whole-string | ✓ escalated, LLM splits it correctly |

Tier 1 is good at isolated Spanish words with distinctive orthography and at
confidently dismissing English prose carrying loanwords. Tier 2 is good at mixed
sentences where a short Spanish utterance is welded to an English dialogue tag.
Neither is good at the other's job. **Every LLM error in this benchmark is caught
by the tier in front of it** — which is the empirical justification for the
two-tier design in DESIGN.md §3.

## Span precision was the real worry, and it's solved

The hardest requirement is excluding the English dialogue tag from the translated
span (`Cuántos años tienes?` **not** `Cuántos años tienes? the old man said.`).
All three models scored **100 %** on this, standalone and in-pipeline. Few-shot
examples showing the exclusion appear to do the work; this was the risk I most
expected to fail.

## Tag-stripping halves the LLM workload

Escalation rate dropped from the **74 %** predicted by raw sentence-level
confidence (DESIGN.md §2.2) to **37 %** measured, because stripping trailing
English dialogue tags rescues short Spanish into the confident band:

```
Tantos, said the man.         0.50  ->  Tantos            0.98
Sí, said the Mexican.         0.51  ->  Sí                1.00
A Casas Grandes, said Billy.  0.50  ->  A Casas Grandes   0.95
```

On the 27 cases: 12 accepted by Tier 1, 5 rejected by Tier 1, 10 escalated.

## Remaining known gaps

1. ~~**Threshold placement is under-evidenced.** `Ay. Ándale, joven. Ándale pues.`
   scores 0.888 — just under the 0.90 accept threshold, so it escalates. No true
   English sentence measured above 0.58, so there is a wide safe gap and the
   accept threshold could likely drop to ~0.75, cutting LLM work further.~~

   **CORRECTED — do not lower the accept threshold.** That claim came from the
   27-case benchmark and is wrong at book scale. A full Tier-1 trace over all
   12,120 segments of the real book shows genuine English sitting well inside the
   0.70–0.90 range:

   | Segment | Tier-1 Spanish confidence | Truth |
   |---|---|---|
   | `Yessir.` | 0.70–0.75 | **English** |
   | `Astrolabe or sextant.` | 0.75–0.80 | **English** |
   | `La Charca.` | 0.85–0.90 | place name, not worth a footnote |

   Dropping the accept threshold to 0.75 would auto-accept `Yessir.` as Spanish
   at Tier 1, where no LLM can veto it — the exact damaging error class the design
   is built to avoid. **Keep 0.90.** The wide abstention band is earning its keep;
   the ~26–38% of LLM calls a lower threshold would save is not worth spurious
   footnotes on `Yessir.`

   This is why the decision trace exists: a 27-case benchmark cannot see this.
2. **The stripped-accept threshold has a natural floor at 0.95.** Measured:
   `A Casas Grandes` (tag-stripped) scores **0.9491** — just 0.0009 under
   `accept_spanish_stripped = 0.95`. Lowering that threshold would auto-accept
   bare Spanish place names at Tier 1, where no LLM can veto them. So while the
   *unstripped* threshold has room to fall, the stripped one does not. Keep 0.95
   and let this case escalate.
3. **Sample size.** 27 cases is enough to pick a model, not enough to publish a
   precision/recall figure. M2 expands to ~250.

## Rejected options

| Option | Why not |
|---|---|
| Argos Translate / OPUS-MT | Pure NMT: translates whatever it's handed. No detection, no span selection, no context. Would "translate" `Yes mam.` Only viable behind a separate detector, and Tier 2 needs to do adjudication, not just translation. |
| Models >14B | Exceed the memory budget on 16 GB. |
