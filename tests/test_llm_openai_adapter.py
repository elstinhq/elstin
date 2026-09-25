"""forge/llm/openai_compat.py — translation between forge's normalized
forge/llm/types.py vocabulary and the OpenAI Chat Completions wire shape,
plus the exception-mapping boundary (forge/llm/errors.py). No network: the
underlying AsyncOpenAI client's `chat.completions.create` is replaced with
a fake returning SimpleNamespace stand-ins shaped like the real SDK's
response objects.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import openai
import pytest

from forge.llm.errors import (
    LlmAuthError,
    LlmBadRequestError,
    LlmContextOverflowError,
    LlmRateLimitError,
    is_context_overflow_message,
)
from forge.llm.openai_compat import OpenAiCompatProvider
from forge.llm.types import (
    ImageBlock,
    LlmRequest,
    Message,
    ResponseFormat,
    TextBlock,
    ToolCallBlock,
    ToolDef,
    ToolResultBlock,
    text_message,
)


def _fake_completion(*, content=None, tool_calls=None, finish_reason="stop", usage=None, model="gpt-5.1"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
        model=model,
    )


def _fake_usage(prompt=10, completion=5, cached=0, reasoning=0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


@pytest.fixture()
def provider():
    return OpenAiCompatProvider(api_key="sk-test")


async def test_complete_returns_text_and_usage(provider, monkeypatch):
    completion = _fake_completion(content="hello there", usage=_fake_usage(prompt=100, completion=20, cached=30))

    async def fake_create(**kwargs):
        return completion

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    response = await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))

    assert response.text() == "hello there"
    assert response.stop_reason == "end_turn"
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.cache_read_tokens == 30


async def test_complete_parses_tool_calls(provider, monkeypatch):
    tool_call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="read_file_tool", arguments='{"path": "a.py"}')
    )
    completion = _fake_completion(content=None, tool_calls=[tool_call], finish_reason="tool_calls", usage=_fake_usage())

    async def fake_create(**kwargs):
        return completion

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    response = await provider.complete(
        LlmRequest(
            model="gpt-5.1", messages=[text_message("user", "read a.py")],
            tools=[ToolDef(name="read_file_tool", description="read", parameters={"type": "object", "properties": {}})],
        )
    )

    assert response.stop_reason == "tool_calls"
    calls = response.tool_calls()
    assert len(calls) == 1
    assert calls[0].name == "read_file_tool"
    assert calls[0].arguments == {"path": "a.py"}


async def test_complete_parses_structured_output(provider, monkeypatch):
    completion = _fake_completion(content='{"answer": 42}', usage=_fake_usage())

    async def fake_create(**kwargs):
        return completion

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    response = await provider.complete(
        LlmRequest(
            model="gpt-5.1", messages=[text_message("user", "compute")],
            response_format=ResponseFormat(name="answer", schema={"type": "object", "properties": {"answer": {"type": "integer"}}}),
        )
    )

    assert response.structured_output == {"answer": 42}


async def test_tool_result_message_becomes_one_message_per_call(provider, monkeypatch):
    captured = {}

    async def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _fake_completion(content="done", usage=_fake_usage())

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    history = [
        Message(role="assistant", content=[ToolCallBlock(id="c1", name="t", arguments={}), ToolCallBlock(id="c2", name="t", arguments={})]),
        Message(role="tool", content=[ToolResultBlock(tool_call_id="c1", content="ok1"), ToolResultBlock(tool_call_id="c2", content="ok2", is_error=True)]),
    ]
    await provider.complete(LlmRequest(model="gpt-5.1", messages=history))

    tool_messages = [m for m in captured["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert tool_messages[0]["content"] == "ok1"
    assert tool_messages[1]["content"] == "ERROR: ok2"


async def test_image_block_becomes_data_uri(provider, monkeypatch):
    captured = {}

    async def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _fake_completion(content="ok", usage=_fake_usage())

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    message = Message(role="user", content=[TextBlock(text="what is this"), ImageBlock(media_type="image/png", data="QUJD")])
    await provider.complete(LlmRequest(model="gpt-5.1", messages=[message]))

    parts = captured["messages"][0]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


async def test_auth_error_maps_to_llm_auth_error(provider, monkeypatch):
    async def fake_create(**kwargs):
        raise openai.AuthenticationError("bad key", response=_fake_http_response(401), body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    with pytest.raises(LlmAuthError):
        await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


async def test_rate_limit_error_maps_to_llm_rate_limit_error(provider, monkeypatch):
    async def fake_create(**kwargs):
        raise openai.RateLimitError("slow down", response=_fake_http_response(429), body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    with pytest.raises(LlmRateLimitError):
        await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


async def test_bad_request_error_maps_to_llm_bad_request_error(provider, monkeypatch):
    async def fake_create(**kwargs):
        raise openai.BadRequestError("bad schema", response=_fake_http_response(400), body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    with pytest.raises(LlmBadRequestError):
        await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


def _fake_http_response(status_code: int):
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return httpx.Response(status_code, request=request)


# --- reasoning_effort routing ------------------------------------------
# Sending the field to a model that rejects it, or sending a LEVEL a model
# rejects, is a 400 -> LlmBadRequestError, which the agent loop never
# retries. So the adapter decides per model, and drops the field rather
# than gamble. See forge/llm/openai_compat.py's _EFFORT_SUPPORT.


@pytest.mark.parametrize(
    "model,effort,expected",
    [
        # OpenAI's own families: unchanged, any configured level passes through.
        ("gpt-5.1", "medium", "medium"),
        ("gpt-5.1-mini", "xhigh", "xhigh"),
        ("o3", "low", "low"),
        # A gateway that namespaces the vendor must match the same rules.
        ("openai/gpt-5.1", "medium", "medium"),
        # Documented levels for these two OpenRouter reasoning models.
        ("z-ai/glm-5.3", "max", "max"),
        ("z-ai/glm-5.3", "high", "high"),
        ("moonshotai/kimi-k3", "low", "low"),
        # GLM accepts low/high/max but NOT medium — which happens to be
        # forge's default dev_sdk_effort, so forwarding it would 400 every
        # single call. Dropped instead.
        ("z-ai/glm-5.3", "medium", None),
        ("moonshotai/kimi-k3", "high", None),
        # Non-reasoning / unknown models never get the field.
        ("deepseek/deepseek-v4-pro-0813", "medium", None),
        ("some-future-model", "high", None),
        # No effort configured means nothing to send.
        ("gpt-5.1", None, None),
        ("z-ai/glm-5.3", "", None),
    ],
)
def test_resolve_reasoning_effort(model, effort, expected):
    from forge.llm.openai_compat import resolve_reasoning_effort

    assert resolve_reasoning_effort(model, effort) == expected


async def test_reasoning_effort_is_sent_for_a_supported_model(provider, monkeypatch):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_completion(content="{}", model="z-ai/glm-5.3")

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    await provider.complete(LlmRequest(model="z-ai/glm-5.3", messages=[text_message("user", "hi")], effort="max"))

    assert captured["reasoning_effort"] == "max"


async def test_unsupported_effort_level_is_omitted_from_the_request(provider, monkeypatch):
    """The whole point: the request still goes out, just without the field."""
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _fake_completion(content="{}", model="z-ai/glm-5.3")

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    response = await provider.complete(
        LlmRequest(model="z-ai/glm-5.3", messages=[text_message("user", "hi")], effort="medium")
    )

    assert "reasoning_effort" not in captured
    assert response.stop_reason == "end_turn"


# --- the overall deadline on one call -------------------------------------


def _request() -> LlmRequest:
    return LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")])


async def test_a_call_that_never_answers_is_abandoned_at_the_deadline(monkeypatch):
    """One run's single model call sat for 2h39m. The SDK's own timeout is per
    read and a keep-alive gateway never trips it, so the deadline is ours."""
    from forge.llm.errors import LlmTimeoutError

    provider = OpenAiCompatProvider(api_key="sk-test", request_timeout_seconds=0.05)
    cancelled = asyncio.Event()

    async def hangs(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(provider._client.chat.completions, "create", hangs)

    with pytest.raises(LlmTimeoutError, match="no response from the model within 0.05s"):
        await asyncio.wait_for(provider.complete(_request()), timeout=2)

    assert cancelled.is_set()  # the request itself was cancelled, not left running


async def test_a_call_that_answers_in_time_is_unaffected(monkeypatch):
    provider = OpenAiCompatProvider(api_key="sk-test", request_timeout_seconds=5)

    async def quick(**kwargs):
        return _fake_completion(content="hello", usage=_fake_usage())

    monkeypatch.setattr(provider._client.chat.completions, "create", quick)

    assert (await provider.complete(_request())).text() == "hello"


@pytest.mark.parametrize("off", [None, 0, -1])
async def test_no_deadline_when_it_is_off(monkeypatch, off):
    provider = OpenAiCompatProvider(api_key="sk-test", request_timeout_seconds=off)

    async def slowish(**kwargs):
        await asyncio.sleep(0.05)
        return _fake_completion(content="late but fine", usage=_fake_usage())

    monkeypatch.setattr(provider._client.chat.completions, "create", slowish)

    assert (await provider.complete(_request())).text() == "late but fine"


@pytest.mark.usefixtures("forge_db")
def test_the_configured_deadline_reaches_the_provider():
    from forge.config.store import set_config
    from forge.llm.registry import get_provider

    set_config("openai_api_key", "sk-test")
    assert get_provider()._request_timeout == 900  # the default: a legitimate dev step tops out near 7.5 minutes

    set_config("llm_request_timeout_seconds", "12")
    assert get_provider()._request_timeout == 12

    set_config("llm_request_timeout_seconds", "0")
    assert get_provider()._request_timeout is None


# --- usage: cache fields and the provider-reported cost ------------------------


async def _complete_with_usage(provider, monkeypatch, usage):
    async def fake_create(**kwargs):
        return _fake_completion(content="ok", usage=usage)

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    return await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


async def test_openrouter_reported_cost_and_cache_writes_are_read(provider, monkeypatch):
    usage = SimpleNamespace(
        prompt_tokens=4785, completion_tokens=5005, total_tokens=9790, cost=0.00915,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4096, cache_write_tokens=12),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=4951),
    )

    response = await _complete_with_usage(provider, monkeypatch, usage)

    assert response.usage.cost_usd == pytest.approx(0.00915)
    assert response.usage.cache_read_tokens == 4096
    assert response.usage.cache_creation_tokens == 12
    assert response.usage.reasoning_tokens == 4951


async def test_deepseek_style_cache_hits_are_read(provider, monkeypatch):
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=10, total_tokens=1010, prompt_cache_hit_tokens=768)

    response = await _complete_with_usage(provider, monkeypatch, usage)

    assert response.usage.cache_read_tokens == 768
    assert response.usage.cost_usd is None


# --- malformed tool-call arguments ------------------------------------------------


@pytest.mark.parametrize("raw,fragment", [('{"path": "a.py"', "not valid JSON"), ('["a.py"]', "must be a JSON object")])
async def test_tool_call_arguments_that_are_not_a_json_object_are_flagged(provider, monkeypatch, raw, fragment):
    tool_call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="read_file_tool", arguments=raw))

    async def fake_create(**kwargs):
        return _fake_completion(tool_calls=[tool_call], finish_reason="tool_calls", usage=_fake_usage())

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)
    response = await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))

    [call] = response.tool_calls()
    assert call.arguments == {}
    assert fragment in call.arguments_error


# --- context overflow ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 128000 tokens. However, your messages resulted in 131072 tokens.",
        "prompt is too long: 210000 tokens > 200000 maximum",
        "Input is too long for requested model.",
    ],
)
async def test_a_context_overflow_400_maps_to_its_own_error(provider, monkeypatch, message):
    async def fake_create(**kwargs):
        raise openai.BadRequestError(message, response=_fake_http_response(400), body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    with pytest.raises(LlmContextOverflowError):
        await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


async def test_a_413_maps_to_context_overflow(provider, monkeypatch):
    async def fake_create(**kwargs):
        raise openai.APIStatusError("Request Entity Too Large", response=_fake_http_response(413), body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", fake_create)

    with pytest.raises(LlmContextOverflowError):
        await provider.complete(LlmRequest(model="gpt-5.1", messages=[text_message("user", "hi")]))


def test_a_rate_limit_that_mentions_tokens_is_not_an_overflow():
    assert not is_context_overflow_message("Rate limit reached: too many tokens per minute; context_length_exceeded")
