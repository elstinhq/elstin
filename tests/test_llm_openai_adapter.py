"""forge/llm/openai_compat.py — translation between forge's normalized
forge/llm/types.py vocabulary and the OpenAI Chat Completions wire shape,
plus the exception-mapping boundary (forge/llm/errors.py). No network: the
underlying AsyncOpenAI client's `chat.completions.create` is replaced with
a fake returning SimpleNamespace stand-ins shaped like the real SDK's
response objects.
"""
from __future__ import annotations

from types import SimpleNamespace

import openai
import pytest

from forge.llm.errors import LlmAuthError, LlmBadRequestError, LlmRateLimitError
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
