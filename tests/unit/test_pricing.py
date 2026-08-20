"""Cost arithmetic and the guards that keep a paid run inside its budget.

Never touches the network — the paid backend is exercised against a fake client.
"""

from __future__ import annotations

import pytest

from joven import pricing
from joven.estimate import _prefix_text
from joven.translate import (
    FEWSHOT,
    STRICT_SCHEMA,
    SYSTEM_PROMPT,
    TRANSLATE_FEWSHOT,
    TRANSLATE_ONLY_PROMPT,
    ClaudeTranslator,
    SpendCapExceeded,
    Verdict,
    get_translator,
)

CHARS_PER_TOKEN = 3.6


# ------------------------------------------------------------------ estimating


def test_uncached_prefix_is_billed_on_every_call() -> None:
    """The whole cost problem in one assertion: the prefix dominates."""
    e = pricing.estimate(
        model="claude-opus-5",
        calls=1000,
        prefix_tokens=1000,
        variable_input_tokens=0,
        output_tokens=0,
        cached=False,
    )
    assert e.input_cost == pytest.approx(1_000_000 / 1e6 * 5.00)


def test_caching_bills_reads_at_a_tenth() -> None:
    cached = pricing.estimate(
        model="claude-opus-5",
        calls=1000,
        prefix_tokens=1000,
        variable_input_tokens=0,
        output_tokens=0,
    )
    uncached = pricing.estimate(
        model="claude-opus-5",
        calls=1000,
        prefix_tokens=1000,
        variable_input_tokens=0,
        output_tokens=0,
        cached=False,
    )
    assert cached.total < uncached.total / 5


def test_caching_below_the_minimum_is_not_priced_in() -> None:
    """Requesting the cache below the threshold is a silent no-op on the API.

    Pricing it as a hit would understate the bill — the exact failure a spend
    guard exists to prevent.
    """
    short = pricing.MODELS["claude-haiku-4-5"].cache_minimum - 1
    e = pricing.estimate(
        model="claude-haiku-4-5",
        calls=100,
        prefix_tokens=short,
        variable_input_tokens=0,
        output_tokens=0,
        cached=True,
    )
    assert not e.cached
    assert e.input_cost == pytest.approx(100 * short / 1e6 * 1.00)


def test_batch_halves_the_total() -> None:
    kw = dict(
        model="claude-sonnet-5",
        calls=500,
        prefix_tokens=2000,
        variable_input_tokens=50_000,
        output_tokens=10_000,
    )
    assert pricing.estimate(**kw, batched=True).total == pytest.approx(
        pricing.estimate(**kw).total / 2
    )


def test_cheapest_per_token_is_not_cheapest_in_practice() -> None:
    """Haiku's 4096-token cache minimum is unreachable for this prompt.

    It therefore pays full price on the prefix for every one of ~2,500 calls,
    which costs more than Sonnet paying a tenth. Documented as a test because it
    inverts the obvious "use the cheap model" instinct.
    """
    kw = dict(calls=2544, prefix_tokens=1361, variable_input_tokens=234_565, output_tokens=51_587)
    haiku = pricing.estimate(model="claude-haiku-4-5", **kw)
    sonnet = pricing.estimate(model="claude-sonnet-5", **kw)
    assert not haiku.cached and sonnet.cached
    assert haiku.total > sonnet.total


def test_estimate_reports_the_rates_it_used() -> None:
    """A stale price table must be visible in the output, not silent."""
    text = pricing.estimate(
        model="claude-opus-5",
        calls=1,
        prefix_tokens=600,
        variable_input_tokens=10,
        output_tokens=10,
    ).format()
    assert "$5.00 in" in text and "$25.00 out" in text


def test_unknown_model_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        ClaudeTranslator(model="claude-does-not-exist")


# ------------------------------------------------------------- cache threshold


@pytest.mark.parametrize(
    ("label", "system", "fewshot"),
    [
        ("adjudicate", SYSTEM_PROMPT, FEWSHOT),
        ("translate", TRANSLATE_ONLY_PROMPT, TRANSLATE_FEWSHOT),
    ],
)
def test_prefix_clears_the_cache_minimum(label: str, system: str, fewshot: list) -> None:
    """Both prompts must stay above the highest threshold we intend to cache on.

    This is a real regression risk: trimming a few-shot to tidy the prompt would
    drop the prefix under the line and silently stop caching, tripling the cost of
    a book run with no error anywhere. Haiku's 4096 is excluded deliberately — it
    is documented as out of reach.
    """
    target = max(
        spec.cache_minimum
        for name, spec in pricing.MODELS.items()
        if name != "claude-haiku-4-5"
    )
    tokens = len(_prefix_text(system, fewshot)) / CHARS_PER_TOKEN
    assert tokens >= target, (
        f"{label} prefix is ~{tokens:.0f} tokens, below the {target}-token "
        f"minimum — caching would silently stop working"
    )


# -------------------------------------------------------------- the spend cap


class _FakeUsage:
    input_tokens = 1_000_000  # $5 on Opus 5 — one call blows any sane cap
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    output_tokens = 0


class _FakeMessage:
    stop_reason = "end_turn"
    usage = _FakeUsage()
    content = [type("B", (), {"type": "text", "text": '{"is_spanish": false}'})()]


class _FakeClient:
    """Counts calls so we can prove the cap stops them."""

    def __init__(self) -> None:
        self.calls = 0
        self.messages = type(
            "M", (), {"create": lambda _self, **kw: self._create(**kw)}
        )()

    def _create(self, **kw):
        self.calls += 1
        return _FakeMessage()


def _translator(cap: float) -> tuple[ClaudeTranslator, _FakeClient]:
    t = ClaudeTranslator(max_cost_usd=cap)
    client = _FakeClient()
    t._client = client
    return t, client


def test_spend_accumulates_from_reported_usage() -> None:
    t, _ = _translator(100.0)
    t.adjudicate("Se fué.")
    assert t.spent == pytest.approx(5.00)  # 1M input tokens at $5/MTok
    assert t.calls == 1


def test_cap_aborts_before_the_next_call_is_made() -> None:
    """The cap must stop the *next* call, not merely report the overrun."""
    t, client = _translator(2.00)
    t.adjudicate("Se fué.")  # spends $5, overshooting the $2 cap
    assert client.calls == 1

    with pytest.raises(SpendCapExceeded) as caught:
        t.adjudicate("Vaya con Dios.")
    assert client.calls == 1, "a second request reached the API despite the cap"
    assert "$2.00" in str(caught.value)


def test_cap_message_names_the_way_out() -> None:
    t, _ = _translator(0.01)
    t.spent = 1.0
    with pytest.raises(SpendCapExceeded, match="--max-cost"):
        t.adjudicate("x")


# ------------------------------------------------------------- request shaping


def test_cache_breakpoint_sits_on_the_last_fewshot_not_the_paragraph() -> None:
    """Marking the final user turn would write a new entry per call and read none."""
    t = ClaudeTranslator()
    messages = t._messages("Se fué.", "context here", FEWSHOT)

    marked = [
        i
        for i, m in enumerate(messages)
        if isinstance(m["content"], list) and "cache_control" in m["content"][0]
    ]
    assert len(marked) == 1, "exactly one breakpoint"
    assert marked[0] == len(messages) - 2, "breakpoint is the turn before the paragraph"
    assert messages[-1]["role"] == "user"
    assert "Se fué." in messages[-1]["content"]


def test_breakpoint_requests_the_long_ttl() -> None:
    """A batch can run for an hour; a 5-minute entry would expire mid-run."""
    t = ClaudeTranslator()
    messages = t._messages("x", "", FEWSHOT)
    block = messages[-2]["content"][0]
    assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_context_is_fenced_off_on_the_paid_path_too() -> None:
    """The context-bleed fix is shared, not reimplemented per backend."""
    t = ClaudeTranslator()
    turn = t._messages("Ay. Ándale, joven.", "Se fué.", FEWSHOT)[-1]["content"]
    assert "<context>" in turn and "Do NOT translate it" in turn


def test_structured_output_schema_forbids_unknown_keys() -> None:
    """Structured outputs reject a schema without it."""
    assert STRICT_SCHEMA["additionalProperties"] is False
    assert set(STRICT_SCHEMA["required"]) == {"is_spanish", "spanish_text", "translation"}


def test_refusal_is_handled_before_reading_content() -> None:
    """A refusal is a 200 with empty content — indexing content[0] would crash."""

    class _Refused(_FakeMessage):
        stop_reason = "refusal"
        content = []

    t, client = _translator(100.0)
    client._create = lambda **kw: _Refused()
    verdict = t.adjudicate("something")
    assert not verdict.ok
    assert "refusal" in verdict.error
    assert verdict.cost_usd > 0, "a refusal still bills for what it consumed"


def test_backend_registry_exposes_claude() -> None:
    assert isinstance(get_translator("claude"), ClaudeTranslator)


def test_unknown_backend_lists_claude_as_an_option() -> None:
    with pytest.raises(ValueError, match="claude"):
        get_translator("gpt")


def test_offline_verdicts_cost_nothing() -> None:
    assert Verdict(is_spanish=True).cost_usd == 0.0
