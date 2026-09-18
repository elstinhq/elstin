"""forge/agents/roles/dev_loop.py — the dev role's use of
forge/agents/loop.py's run_agent_loop. classify_loop_result is pure
(tested standalone, no provider needed); run_dev itself is exercised
end-to-end against a real git repo worktree and real dev tools, with only
forge.llm.registry.get_provider replaced by a scripted fake — matching
this repo's convention of faking at the service boundary (forge/llm/)
rather than mocking run_dev itself."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from forge.agents.roles.dev_loop import (
    DevLoopError,
    classify_loop_result,
    run_dev,
)
from forge.llm.base import Capabilities
from forge.llm.errors import LlmBadRequestError
from forge.llm.types import LlmRequest, LlmResponse, TextBlock, ToolCallBlock, Usage

pytestmark = pytest.mark.usefixtures("forge_db")


@dataclass
class FakeProvider:
    script: list[LlmResponse | Exception]
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model="gpt-5.1")


def _tool_call_response(name: str, arguments: dict, call_id: str = "call_1") -> LlmResponse:
    return LlmResponse(content=[ToolCallBlock(id=call_id, name=name, arguments=arguments)], stop_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=5), model="gpt-5.1")


@pytest.mark.parametrize(
    "terminal_reason,is_error,expected",
    [
        ("max_turns", False, "soft_stop"),
        ("max_budget_usd", False, "soft_stop"),
        ("end_turn", False, "success"),
        ("error", True, "hard_failure"),
        (None, True, "hard_failure"),
    ],
)
def test_classify_loop_result(terminal_reason, is_error, expected):
    assert classify_loop_result(terminal_reason, is_error) == expected


async def test_run_dev_raises_when_provider_not_configured(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """No OpenAI key configured — forge/llm/registry.py raises
    LlmNotConfiguredError, which run_dev must translate into its own
    DevLoopError rather than letting a registry-internal exception
    type leak to the graph node."""
    with pytest.raises(DevLoopError, match="No OpenAI API key"):
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)


async def test_run_dev_raises_on_hard_provider_error(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda: FakeProvider(script=[LlmBadRequestError("bad schema")]))

    with pytest.raises(DevLoopError, match="bad schema"):
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)


async def test_run_dev_completes_with_no_tool_calls(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda: FakeProvider(script=[_text_response("nothing to change")]))

    result = await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo, run_id="ENG-1")

    assert result["truncated"] is False
    assert result["summary"] == "nothing to change"
    assert result["file_changes"] == []
    assert result["dev_history"]  # history persisted for a future resume


async def test_run_dev_dispatches_a_real_dev_tool(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """End-to-end through the real make_dev_tools() wiring — the model
    calls write_file_tool for a file inside the plan's allowed paths, and
    that file actually lands in the worktree."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda: FakeProvider(script=[
            _tool_call_response("write_file_tool", {"path": "widget.py", "content": "widget = True\n"}),
            _text_response("created widget.py"),
        ]),
    )

    plan = {"files_to_create": [{"path": "widget.py"}]}
    result = await run_dev(plan=plan, context_bundle={}, worktree_path=git_repo, project_path=git_repo)

    assert any(fc.get("path") == "widget.py" for fc in result["file_changes"])
    with open(f"{git_repo}/widget.py") as f:
        assert f.read() == "widget = True\n"


async def test_run_dev_raises_when_the_turn_was_truncated(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """A provider-truncated turn (stop_reason="max_tokens") must surface as
    a hard failure, not a clean run with no file changes — the latter is
    indistinguishable from "the model correctly decided nothing needed
    changing" and would sail through to verification and a PR-open
    attempt. This is the post-migration counterpart of the old
    "no result message" guard."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config
    from forge.llm.types import LlmResponse, Usage

    set_config("openai_api_key", "sk-test")
    truncated = LlmResponse(
        content=[TextBlock(text="starting to write the fi")], stop_reason="max_tokens",
        usage=Usage(input_tokens=10, output_tokens=5), model="gpt-5.1",
    )
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda: FakeProvider(script=[truncated]))

    with pytest.raises(DevLoopError, match="max_tokens"):
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)


async def test_run_dev_soft_stop_on_max_turns(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    set_config("dev_sdk_max_turns", "1")
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda: FakeProvider(script=[_tool_call_response("search_codebase_tool", {"query": "foo"})]),
    )

    result = await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)

    assert result["truncated"] is True
    assert result["dev_history"]


async def test_run_dev_resumes_from_prior_history(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """A retry passes back the dev_history a prior soft-stopped run
    returned — run_dev must feed it straight back into the loop as
    prior conversation, not discard it."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config
    from forge.llm.types import history_to_json, text_message

    set_config("openai_api_key", "sk-test")
    prior_history = history_to_json([text_message("user", "earlier turn"), text_message("assistant", "earlier reply")])

    captured_requests = []

    @dataclass
    class CapturingProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            captured_requests.append(request)
            return await super().complete(request)

    monkeypatch.setattr(dev_loop_module, "get_provider", lambda: CapturingProvider(script=[_text_response("resumed and done")]))

    await run_dev(
        plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo,
        prior_history=prior_history, verifier_report={"required_fixes": ["fix the thing"]},
    )

    assert len(captured_requests[0].messages) == 3  # 2 prior turns + this iteration's brief
    assert captured_requests[0].messages[0].content[0].text == "earlier turn"
