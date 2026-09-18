"""forge/agents/roles/planner_loop.py — the planner's structured-output
schema, its retry policy, and how it feeds attachments/tools into
forge/agents/loop.py. Faked at the forge/llm/ boundary (get_provider), so
the real prompt building, tool wiring and schema all run."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import forge.agents.roles.planner_loop as planner_module
from forge.agents.roles.planner_loop import _PLAN_JSON_SCHEMA, PlannerLoopError, run_planner
from forge.llm.base import Capabilities
from forge.llm.errors import LlmBadRequestError
from forge.llm.types import ImageBlock, LlmRequest, LlmResponse, TextBlock, Usage

pytestmark = pytest.mark.usefixtures("forge_db")

_MINIMAL_PLAN = {
    "feature_summary": "Add a widget",
    "reasoning": None,
    "questions": [],
    "files_to_create": [{"path": "widget.py", "purpose": "the widget"}],
    "files_to_modify": [],
    "context_files": [],
    "architecture": None,
    "acceptance_criteria": ["it works"],
    "discovery_evidence": [],
    "plan_markdown": "# Plan",
}


@dataclass
class FakeProvider:
    script: list[LlmResponse | Exception]
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list[LlmRequest] = field(default_factory=list)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _plan_response(plan: dict | None = None) -> LlmResponse:
    return LlmResponse(
        content=[TextBlock(text="{}")], stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=50), model="gpt-5.1",
        structured_output=_MINIMAL_PLAN if plan is None else plan,
    )


def _no_plan_response() -> LlmResponse:
    """A run that finished cleanly but produced no plan object."""
    return LlmResponse(
        content=[TextBlock(text="I need more information")], stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=50), model="gpt-5.1", structured_output=None,
    )


def _configure(monkeypatch, provider) -> None:
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    monkeypatch.setattr(planner_module, "get_provider", lambda: provider)


def test_plan_schema_satisfies_strict_structured_output_rules():
    """Strict json_schema mode requires every property to be listed in
    `required` at every nesting level, with `additionalProperties: false`
    — optional fields are expressed as nullable types instead of being
    omitted from `required`. A schema that breaks this is rejected by the
    provider at request time, i.e. on a real run and not in any test that
    fakes the provider — so assert the rules here directly."""

    def check(node, path="root"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            props = set(node.get("properties", {}))
            required = set(node.get("required", []))
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
            assert props == required, f"{path}: required {sorted(required)} != properties {sorted(props)}"
            for key, value in node["properties"].items():
                check(value, f"{path}.{key}")
        elif node.get("type") == "array":
            check(node.get("items", {}), f"{path}[]")

    check(_PLAN_JSON_SCHEMA)


async def test_returns_plan_questions_and_acceptance_criteria(monkeypatch, git_repo):
    provider = FakeProvider(script=[_plan_response()])
    _configure(monkeypatch, provider)

    result = await run_planner(user_request="Add a widget", project_path=git_repo)

    assert result["plan"]["feature_summary"] == "Add a widget"
    assert result["acceptance_criteria"] == ["it works"]
    assert result["questions"] == []


async def test_hard_provider_error_is_not_retried(monkeypatch, git_repo):
    """forge/agents/loop.py already exhausted its own retries for anything
    retryable, so an errored loop result will fail identically on a
    retry — re-running it just pays for the same failure N times."""
    provider = FakeProvider(script=[LlmBadRequestError("schema rejected")])
    _configure(monkeypatch, provider)

    with pytest.raises(PlannerLoopError):
        await run_planner(user_request="Add a widget", project_path=git_repo)

    assert len(provider.calls) == 1


async def test_missing_plan_is_retried_then_raises(monkeypatch, git_repo):
    """The one case retrying *is* worth it: the run completed fine, the
    model just didn't emit a valid plan object."""
    from forge.config.store import set_config

    provider = FakeProvider(script=[_no_plan_response(), _no_plan_response()])
    _configure(monkeypatch, provider)
    set_config("planner_sdk_max_retries", "1")  # -> 2 attempts

    with pytest.raises(PlannerLoopError, match="2 attempt"):
        await run_planner(user_request="Add a widget", project_path=git_repo)

    assert len(provider.calls) == 2


async def test_missing_plan_then_success_on_retry(monkeypatch, git_repo):
    provider = FakeProvider(script=[_no_plan_response(), _plan_response()])
    _configure(monkeypatch, provider)

    result = await run_planner(user_request="Add a widget", project_path=git_repo)

    assert result["plan"]["feature_summary"] == "Add a widget"
    assert len(provider.calls) == 2


async def test_attachment_becomes_an_image_block_alongside_the_prompt(monkeypatch, git_repo, tmp_path):
    provider = FakeProvider(script=[_plan_response()])
    _configure(monkeypatch, provider)
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"\x89PNG fake bytes")

    await run_planner(user_request="Match this design", project_path=git_repo, attachment_paths=[str(screenshot)])

    first_message = provider.calls[0].messages[0]
    kinds = [type(block) for block in first_message.content]
    assert kinds == [TextBlock, ImageBlock]
    assert first_message.content[1].media_type == "image/png"


async def test_missing_attachment_path_is_skipped_not_fatal(monkeypatch, git_repo):
    provider = FakeProvider(script=[_plan_response()])
    _configure(monkeypatch, provider)

    await run_planner(user_request="go", project_path=git_repo, attachment_paths=["/nonexistent/shot.png"])

    assert len(provider.calls[0].messages[0].content) == 1  # text only


async def test_planner_gets_read_only_tools(monkeypatch, git_repo):
    provider = FakeProvider(script=[_plan_response()])
    _configure(monkeypatch, provider)

    await run_planner(user_request="go", project_path=git_repo)

    tool_names = {tool.name for tool in provider.calls[0].tools}
    assert tool_names == {"read_file_tool", "grep_tool", "find_files_tool"}
    # No write/edit tool may ever reach the planner.
    assert not any("write" in name or "edit" in name for name in tool_names)


async def test_not_configured_raises_planner_error(git_repo):
    with pytest.raises(PlannerLoopError, match="No OpenAI API key"):
        await run_planner(user_request="go", project_path=git_repo)
