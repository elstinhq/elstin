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


# --- tolerant parsing, and what happens when it can't ------------------
# The verifier and QA pass no response_schema, so they always land on this
# path: a prompt asking for JSON plus whatever the model actually says.


def test_parses_a_fenced_json_block():
    """The most common chatty-model wrapper. The old greedy brace regex
    happened to survive this one, but only by accident."""
    from forge.services.generation import _parse_json_content

    assert _parse_json_content('```json\n{"passed": true, "issues": []}\n```') == {"passed": True, "issues": []}


def test_prefers_the_real_object_over_a_worked_example():
    """Two objects in one reply broke the greedy brace span outright: it
    spanned from the first { to the last }, which parses as nothing."""
    from forge.services.generation import _parse_json_content

    reply = 'For example a passing report looks like {"passed": true}.\n\nHere is mine:\n{"passed": false, "required_fixes": ["fix the thing"], "detail": {"file": "a.py"}}'

    assert _parse_json_content(reply) == {
        "passed": False, "required_fixes": ["fix the thing"], "detail": {"file": "a.py"},
    }


def test_nested_braces_and_strings_containing_braces_survive():
    from forge.services.generation import _parse_json_content

    assert _parse_json_content('{"note": "use {} for an empty dict", "a": {"b": {"c": 1}}}') == {
        "note": "use {} for an empty dict", "a": {"b": {"c": 1}},
    }


def test_a_bare_list_is_not_accepted_as_a_report():
    """Every caller immediately does .get() on the result, so handing back
    a list just moves the failure somewhere less obvious."""
    from forge.services.generation import GenerationError, _parse_json_content

    with pytest.raises(GenerationError):
        _parse_json_content('["a", "b"]')


def test_prose_with_no_json_raises_generation_error_not_json_decode_error():
    """This is the type forge/orchestration/graph.py catches. A bare
    JSONDecodeError escaping into the daemon's poll loop is what used to
    abandon the rest of the cycle."""
    from forge.services.generation import GenerationError, _parse_json_content

    with pytest.raises(GenerationError, match="no JSON object"):
        _parse_json_content("I was unable to review this change.")


async def test_unparseable_reply_surfaces_as_generation_error(monkeypatch):
    provider = FakeProvider(response=_response("Sorry, I can't help with that."))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    from forge.services.generation import GenerationError

    with pytest.raises(GenerationError):
        await generation.chat_completion_json(messages=[{"role": "user", "content": "review"}])


async def test_provider_error_is_a_generation_error_and_still_a_runtime_error(monkeypatch):
    """GenerationError subclasses RuntimeError so existing callers that
    catch RuntimeError keep working unchanged."""
    provider = FakeProvider(response=LlmBadRequestError("nope"))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    from forge.services.generation import GenerationError

    with pytest.raises(GenerationError):
        await generation.chat_completion_json(messages=[{"role": "user", "content": "go"}])
    assert issubclass(GenerationError, RuntimeError)


# --- the verifier and QA schemas ---------------------------------------
# Both roles used to ask for JSON in prose and rely on the tolerant
# extraction above. That is the pipeline's weakest link on a cheap model,
# so the shape is now provider-enforced wherever json_schema is honoured.


@pytest.mark.parametrize(
    "schema_name",
    ["verifier", "qa"],
)
def test_role_schemas_satisfy_strict_structured_output_rules(schema_name):
    """Strict json_schema mode requires every property in `required` at
    every level with additionalProperties false. A schema that breaks this
    is rejected at request time — on a real run, and in no test that fakes
    the provider."""
    if schema_name == "verifier":
        from forge.agents.roles.verifier import _VERIFIER_JSON_SCHEMA as schema
    else:
        from forge.agents.roles.qa import QA_JSON_SCHEMA as schema

    def check(node, path="root"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
            assert set(node.get("properties", {})) == set(node.get("required", [])), f"{path}: required != properties"
            for key, value in node["properties"].items():
                check(value, f"{path}.{key}")
        elif node.get("type") == "array":
            check(node.get("items", {}), f"{path}[]")

    check(schema)


def test_qa_schema_does_not_ask_the_model_for_the_test_suite_result():
    """The deterministic suite result is stamped on after parsing, because
    evidence outranks the model's opinion. Asking for it would invite an
    invented one."""
    from forge.agents.roles.qa import QA_JSON_SCHEMA

    assert "full_test_suite" not in QA_JSON_SCHEMA["properties"]


def test_verifier_schema_allows_a_finding_with_no_single_file():
    """A review finding can be about the change as a whole. Under strict
    mode that has to be a nullable type, not an omitted key."""
    from forge.agents.roles.verifier import _VERIFIER_JSON_SCHEMA

    file_type = _VERIFIER_JSON_SCHEMA["properties"]["issues"]["items"]["properties"]["file"]["type"]
    assert "null" in file_type


async def test_schema_is_forwarded_to_the_provider_when_supported(monkeypatch):
    provider = FakeProvider(response=_response('{"passed": true}', structured={"passed": True}))
    monkeypatch.setattr(generation, "get_provider", lambda: provider)

    from forge.agents.roles.verifier import _VERIFIER_JSON_SCHEMA

    await generation.chat_completion_json(
        messages=[{"role": "user", "content": "review"}],
        response_schema=_VERIFIER_JSON_SCHEMA,
        response_schema_name="verifier_review",
    )

    assert provider.last_request.response_format is not None
    assert provider.last_request.response_format.name == "verifier_review"
    assert provider.last_request.response_format.strict is True
