"""The OpenAI-compatible backend, against a fake server.

``httpx.MockTransport`` stands in for llama.cpp / LM Studio / vLLM, so these run
offline and can script exactly the refusals a real server might send.
"""

from __future__ import annotations

import json

import httpx
import pytest

from joven.translate import (
    FEWSHOT,
    STRICT_SCHEMA,
    OpenAITranslator,
    get_translator,
)


def _reply(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


GOOD = json.dumps(
    {"is_spanish": True, "spanish_text": "Se fué.", "translation": "He is gone."}
)


def _translator(handler, **kwargs) -> OpenAITranslator:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAITranslator(model="qwen3.8-27b", _client=client, **kwargs)


# --------------------------------------------------------------- request shape


def test_request_carries_the_shared_prompt_and_a_strict_schema() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _reply(GOOD)

    verdict = _translator(handler, base_url="http://box:8081/v1/").adjudicate("Se fué.")

    assert verdict.ok and verdict.translation == "He is gone."
    assert verdict.model == "qwen3.8-27b"
    (request,) = seen
    assert str(request.url) == "http://box:8081/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "qwen3.8-27b"
    assert body["temperature"] == 0
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "joven_verdict", "schema": STRICT_SCHEMA, "strict": True},
    }
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # the same conversation the Ollama backend sends: system, few-shots, paragraph
    assert body["messages"][0]["role"] == "system"
    assert len(body["messages"]) == 1 + 2 * len(FEWSHOT) + 1
    assert body["messages"][-1] == {"role": "user", "content": "Se fué."}
    assert "Authorization" not in request.headers


def test_api_key_becomes_a_bearer_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _reply(GOOD)

    translator = OpenAITranslator(model="m", api_key="sk-local")
    # the client is built lazily so the header can be checked on the real one
    translator.client()._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    translator.translate("Se fué.")
    assert seen[0].headers["Authorization"] == "Bearer sk-local"


# ------------------------------------------------------------ learned switches


def test_a_400_drops_the_thinking_switch_then_the_schema_and_remembers() -> None:
    """Two refusals cost two extra requests once, not on every call."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "chat_template_kwargs" in body or body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "unknown field"})
        return _reply(GOOD)

    translator = _translator(handler)
    first = translator.adjudicate("Se fué.")
    assert first.ok, first.error
    assert len(bodies) == 3
    assert "chat_template_kwargs" in bodies[0]
    assert "chat_template_kwargs" not in bodies[1]
    assert bodies[1]["response_format"]["type"] == "json_schema"
    assert bodies[2]["response_format"] == {"type": "json_object"}

    translator.adjudicate("Vaya con Dios.")
    assert len(bodies) == 4, "the second call goes straight to what worked"
    assert "chat_template_kwargs" not in bodies[3]
    assert bodies[3]["response_format"] == {"type": "json_object"}


def test_a_400_with_nothing_left_to_drop_is_an_error_verdict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad model"})

    verdict = _translator(handler).adjudicate("Se fué.")
    assert not verdict.ok
    assert "400" in verdict.error
    assert verdict.is_spanish is False


# ------------------------------------------------------------------ the reply


def test_a_thinking_block_and_a_code_fence_are_stripped_before_parsing() -> None:
    noisy = "<think>\nIs this Spanish? Yes.\n</think>\n```json\n" + GOOD + "\n```"
    verdict = _translator(lambda r: _reply(noisy)).adjudicate("Se fué.")
    assert verdict.ok, verdict.error
    assert verdict.translation == "He is gone."


def test_non_json_is_an_error_verdict_that_keeps_the_raw_reply() -> None:
    verdict = _translator(lambda r: _reply("Sorry, I cannot help with that.")).adjudicate("x")
    assert not verdict.ok
    assert "unparseable JSON" in verdict.error
    assert verdict.raw.startswith("Sorry")


def test_a_dead_server_is_an_error_verdict_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    verdict = _translator(handler).translate("Se fué.")
    assert not verdict.ok
    assert "ConnectError" in verdict.error
    assert verdict.latency_s >= 0


# ------------------------------------------------------------------- factory


def test_get_translator_builds_it_and_trims_the_slash() -> None:
    translator = get_translator("openai", "qwen3.8-27b", base_url="http://localhost:8081/v1/")
    assert isinstance(translator, OpenAITranslator)
    assert translator.base_url == "http://localhost:8081/v1"
    assert translator.name == "openai:qwen3.8-27b"


def test_get_translator_defaults_the_openai_address() -> None:
    translator = get_translator("openai", "m")
    assert isinstance(translator, OpenAITranslator)
    assert translator.base_url.endswith("/v1")


def test_unknown_backend_lists_the_real_ones() -> None:
    with pytest.raises(ValueError, match="ollama, openai, stub"):
        get_translator("telepathy")
