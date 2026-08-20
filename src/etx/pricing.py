"""Model prices and cache thresholds, plus the arithmetic for a spend estimate.

Kept in one module because the numbers are load-bearing in three places — the
pre-flight estimate, the live spend cap, and the run summary — and because they
are the part most likely to go stale.

Two facts drive every cost decision in this project:

**The prefix dominates.** 75% of all input on a book run is the same system
prompt and few-shots, re-sent once per call (2,544 times on *The Crossing*). The
Spanish being translated is the minority of the bill.

**Prompt caching has a minimum, and it is not monotonic across models.** A prefix
shorter than the minimum silently does not cache — no error, just
``cache_creation_input_tokens: 0``. The thresholds are far apart:

======================  =========  =====================================
model                   minimum    our 620-token adjudicate prefix
======================  =========  =====================================
``claude-opus-5``            512    caches as-is
``claude-sonnet-5``         1024    too short — needs a bigger few-shot set
``claude-haiku-4-5``        4096    impractical to reach; runs uncached
======================  =========  =====================================

So the same prompt is cacheable on one model and silently uncacheable on
another, and the fix — a *larger* prompt — is the opposite of the usual advice.
Growing the few-shot set past the threshold is cheaper than staying under it
(1024 tokens at the 0.1x read rate beats 620 at full rate) and buys better
translations at the same time.

Prices are USD per million tokens and change; :func:`estimate` reports them so a
stale table is visible in the output rather than silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_READ = 0.10
"""Cached input bills at a tenth of the base rate."""

CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.00
"""Writing the cache costs a premium once; 1h is worth it for a batch run."""

BATCH_DISCOUNT = 0.50
"""The Batches API is half price, and a book run has no latency requirement."""


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    input_per_mtok: float
    output_per_mtok: float
    cache_minimum: int
    note: str = ""


MODELS: dict[str, Model] = {
    "claude-opus-5": Model("claude-opus-5", 5.00, 25.00, 512),
    # Priced at the standard rate on purpose. Introductory pricing is lower, so
    # estimates stay conservative while it lasts — a budget guard that understates
    # the bill is worse than useless.
    "claude-sonnet-5": Model(
        "claude-sonnet-5",
        3.00,
        15.00,
        1024,
        "billed at $2/$10 until 2026-08-31, so this estimate is conservative until then",
    ),
    "claude-haiku-4-5": Model(
        "claude-haiku-4-5", 1.00, 5.00, 4096, "cache minimum is impractical to reach"
    ),
}

DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True, slots=True)
class Estimate:
    """A pre-flight cost projection, with the inputs kept so it can be audited."""

    model: str
    calls: int
    prefix_tokens: int
    variable_input_tokens: int
    output_tokens: int
    cached: bool
    batched: bool
    input_cost: float
    output_cost: float

    @property
    def total(self) -> float:
        return self.input_cost + self.output_cost

    def format(self) -> str:
        m = MODELS[self.model]
        lines = [
            f"model                {self.model}"
            + (f"  ({m.note})" if m.note else ""),
            f"rates                ${m.input_per_mtok:.2f} in / "
            f"${m.output_per_mtok:.2f} out per MTok",
            f"LLM calls            {self.calls:,}",
            f"prefix per call      {self.prefix_tokens:,} tokens"
            + (
                "  (cached at 0.1x)"
                if self.cached
                else f"  (UNCACHED — below the {m.cache_minimum}-token minimum)"
            ),
            f"variable input       {self.variable_input_tokens:,} tokens",
            f"output               {self.output_tokens:,} tokens",
            f"batch API            {'yes (-50%)' if self.batched else 'no'}",
            "",
            f"estimated input      ${self.input_cost:.2f}",
            f"estimated output     ${self.output_cost:.2f}",
            f"ESTIMATED TOTAL      ${self.total:.2f}",
        ]
        return "\n".join(lines)


def estimate(
    *,
    model: str,
    calls: int,
    prefix_tokens: int,
    variable_input_tokens: int,
    output_tokens: int,
    cached: bool = True,
    batched: bool = False,
    cache_ttl: str = "1h",
) -> Estimate:
    """Project the cost of a run from measured token counts.

    ``prefix_tokens`` is the per-call constant (system + few-shots);
    ``variable_input_tokens`` and ``output_tokens`` are totals across all calls.

    Caching is applied only when the prefix actually clears the model's minimum —
    asking for it below the threshold is a silent no-op on the API, so silently
    pricing it in here would understate the bill.
    """
    m = MODELS[model]
    write = CACHE_WRITE_1H if cache_ttl == "1h" else CACHE_WRITE_5M
    effective_cache = cached and prefix_tokens >= m.cache_minimum

    if effective_cache:
        prefix_total = prefix_tokens * write + calls * prefix_tokens * CACHE_READ
    else:
        prefix_total = calls * prefix_tokens

    scale = BATCH_DISCOUNT if batched else 1.0
    input_cost = (prefix_total + variable_input_tokens) / 1e6 * m.input_per_mtok * scale
    output_cost = output_tokens / 1e6 * m.output_per_mtok * scale

    return Estimate(
        model=model,
        calls=calls,
        prefix_tokens=prefix_tokens,
        variable_input_tokens=variable_input_tokens,
        output_tokens=output_tokens,
        cached=effective_cache,
        batched=batched,
        input_cost=input_cost,
        output_cost=output_cost,
    )


def call_cost(
    model: str,
    *,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> float:
    """Actual cost of one completed call, from the API's own usage figures.

    This is what the live spend cap accumulates — the estimate above projects,
    this one measures.
    """
    m = MODELS[model]
    billable_input = (
        input_tokens
        + cache_read_tokens * CACHE_READ
        + cache_write_tokens * CACHE_WRITE_1H
    )
    return billable_input / 1e6 * m.input_per_mtok + output_tokens / 1e6 * m.output_per_mtok
