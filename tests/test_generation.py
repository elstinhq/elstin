"""forge/services/generation.py:chat_completion_json — the single-shot,
no-tools JSON completion verifier/QA share. Exercised against a fake
LlmProvider via forge.services.generation.get_provider, never the real
OpenAI adapter."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import forge.services.generation as generation
from forge.llm.base import Capabilities
from forge.llm.errors import LlmBadRequestError
from forge.llm.types import LlmRequest, LlmResponse, TextBlock, Usage

pytestmark = pytest.mark.usefixtures("forge_db")


@dataclass
class FakeProvider:
    response: LlmResponse
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    last_request: LlmRequest | None = None

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.last_request = request
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _response(text: str, *, structured=None, usage=None) -> LlmResponse:
    return LlmResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=usage or Usage(input_tokens=50, output_tokens=10), model="gpt-5.1-mini", structured_output=structured)


async def test_parses_plain_json_content(monkeypatch):
    provider = FakeProvider(response=_response('{"passed": true}'))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    parsed, usage = await generation.chat_completion_json(messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "review this"}])

    assert parsed == {"passed": True}
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 10
    assert usage["model"] == "gpt-5.1-mini"


async def test_tolerates_prose_around_json():
    from forge.services.generation import _parse_json_content

    assert _parse_json_content('Sure, here you go:\n{"a": 1}\nHope that helps!') == {"a": 1}


async def test_uses_structured_output_when_provider_supports_it(monkeypatch):
    provider = FakeProvider(response=_response('{"a": 1}', structured={"a": 1}))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    parsed, _ = await generation.chat_completion_json(
        messages=[{"role": "user", "content": "go"}],
        response_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
    )

    assert parsed == {"a": 1}
    assert provider.last_request.response_format is not None
    assert provider.last_request.response_format.schema["properties"]["a"]["type"] == "integer"


async def test_skips_structured_output_when_provider_lacks_support(monkeypatch):
    provider = FakeProvider(response=_response('{"a": 1}'), capabilities=Capabilities(supports_structured_output=False))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    await generation.chat_completion_json(
        messages=[{"role": "user", "content": "go"}],
        response_schema={"type": "object"},
    )

    assert provider.last_request.response_format is None


async def test_system_and_user_messages_split_correctly(monkeypatch):
    provider = FakeProvider(response=_response('{}'))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    await generation.chat_completion_json(messages=[
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "part one"},
    ])

    assert provider.last_request.system == "be terse"
    assert provider.last_request.messages[0].content[0].text == "part one"


async def test_provider_error_is_wrapped_as_runtime_error(monkeypatch):
    provider = FakeProvider(response=LlmBadRequestError("bad schema"))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    with pytest.raises(RuntimeError, match="bad schema"):
        await generation.chat_completion_json(messages=[{"role": "user", "content": "go"}], label="verifier")
