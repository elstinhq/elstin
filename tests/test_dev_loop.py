"""forge/agents/roles/dev_loop.py — the dev role's use of
forge/agents/loop.py's run_agent_loop. classify_loop_result is pure
(tested standalone, no provider needed); run_dev itself is exercised
end-to-end against a real git repo worktree and real dev tools, with only
forge.llm.registry.get_provider replaced by a scripted fake — matching
this repo's convention of faking at the service boundary (forge/llm/)
rather than mocking run_dev itself."""
from __future__ import annotations

import os
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
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_args, **_kwargs: FakeProvider(script=[LlmBadRequestError("bad schema")]))

    with pytest.raises(DevLoopError, match="bad schema"):
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)


async def test_run_dev_completes_with_no_tool_calls(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_args, **_kwargs: FakeProvider(script=[_text_response("nothing to change")]))

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
        lambda *_args, **_kwargs: FakeProvider(script=[
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
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_args, **_kwargs: FakeProvider(script=[truncated]))

    with pytest.raises(DevLoopError, match="max_tokens"):
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)


async def test_run_dev_soft_stop_on_max_turns(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    set_config("dev_sdk_max_turns", "1")
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda *_args, **_kwargs: FakeProvider(script=[_tool_call_response("search_codebase_tool", {"query": "foo"})]),
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

    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_args, **_kwargs: CapturingProvider(script=[_text_response("resumed and done")]))

    await run_dev(
        plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo,
        prior_history=prior_history, verifier_report={"required_fixes": ["fix the thing"]},
    )

    assert len(captured_requests[0].messages) == 3  # 2 prior turns + this iteration's brief
    assert captured_requests[0].messages[0].content[0].text == "earlier turn"


async def test_run_dev_uses_gate_feedback_as_the_brief_on_top_of_prior_history(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """The Tier 1 gate's fix message is appended to the existing history
    as the next user turn — no fresh FIX MODE brief, no re-injected file
    contents — so the model keeps its context and the prompt prefix
    stays cacheable."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config
    from forge.llm.types import history_to_json, text_message

    set_config("openai_api_key", "sk-test")
    prior_history = history_to_json([text_message("user", "the plan"), text_message("assistant", "wrote a.py")])
    captured_requests = []

    @dataclass
    class CapturingProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            captured_requests.append(request)
            return await super().complete(request)

    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_args, **_kwargs: CapturingProvider(script=[_text_response("fixed")]))

    await run_dev(
        plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo,
        prior_history=prior_history, verifier_report={"required_fixes": ["stale review finding"]},
        gate_feedback="Deterministic checks FAILED with 1 problem(s) in a.py (fix attempt 1/3).\n1. analyze failed",
    )

    messages = captured_requests[0].messages
    assert len(messages) == 3
    assert messages[2].role == "user"
    assert messages[2].content[0].text.startswith("Deterministic checks FAILED with 1 problem(s) in a.py")
    assert "FIX MODE" not in messages[2].content[0].text
    assert "stale review finding" not in messages[2].content[0].text


async def test_run_dev_no_progress_stop_is_a_soft_stop(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """The idle-turn budget ends the loop like a turn cap does: truncated,
    history kept for the next round, no DevLoopError."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    set_config("dev_sdk_max_idle_turns", "2")
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda *_args, **_kwargs: FakeProvider(script=[
            _tool_call_response("search_codebase_tool", {"query": "foo"}, call_id="c1"),
            _tool_call_response("search_codebase_tool", {"query": "bar"}, call_id="c2"),
            _text_response("never reached"),
        ]),
    )

    result = await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)

    assert result["truncated"] is True
    assert result["dev_history"]


@pytest.mark.parametrize(
    "base,files,expected",
    [
        (4, 0, 4), (4, 1, 4), (4, 2, 4),  # a small plan keeps the configured budget
        (4, 3, 5), (4, 5, 6), (4, 15, 11),  # +1 turn per two further files
        (4, 200, 16),  # but bounded: a looping model must still be stopped
        (2, 15, 8),
        (0, 15, 0), (-1, 5, 0),  # off stays off
    ],
)
def test_idle_budget_grows_with_the_plan_and_is_bounded(base, files, expected):
    from forge.agents.roles.dev_loop import idle_budget_for_plan

    assert idle_budget_for_plan(base, files) == expected


async def _run_with_read_only_turns(git_repo: str, monkeypatch: pytest.MonkeyPatch, *, planned_files: int, read_turns: int):
    """A dev run that explores for `read_turns` turns and then finishes
    without ever writing — the shape of the run that failed three fix rounds."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    set_config("dev_sdk_max_idle_turns", "2")
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda *_args, **_kwargs: FakeProvider(script=[
            _tool_call_response("search_codebase_tool", {"query": f"q{i}"}, call_id=f"c{i}") for i in range(read_turns)
        ] + [_text_response("done")]),
    )
    plan = {"files_to_create": [{"path": f"new_{i}.py"} for i in range(planned_files)]}
    return await run_dev(plan=plan, context_bundle={}, worktree_path=git_repo, project_path=git_repo)


async def test_a_multi_file_plan_is_not_cut_off_by_the_exploration_a_small_one_would_be(
    git_repo: str, monkeypatch: pytest.MonkeyPatch
):
    """Five read-only turns with a base budget of 2: one planned file gets 2
    and is stopped; twelve get 2 + 5 = 7 and are still reading when they
    finish. The base is the same — the plan size is the difference."""
    small = await _run_with_read_only_turns(git_repo, monkeypatch, planned_files=1, read_turns=5)
    large = await _run_with_read_only_turns(git_repo, monkeypatch, planned_files=12, read_turns=5)

    assert small["truncated"] is True
    assert large["truncated"] is False


async def test_a_multi_file_plan_that_only_loops_is_still_stopped(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """Scaling must not remove the guard: 12 files allows 7 idle turns, not 20."""
    result = await _run_with_read_only_turns(git_repo, monkeypatch, planned_files=12, read_turns=20)

    assert result["truncated"] is True


async def test_dev_run_seeds_the_checklist_from_the_plan_and_reshows_it_each_turn(
    git_repo: str, monkeypatch: pytest.MonkeyPatch
):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    captured = []

    @dataclass
    class CapturingProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            captured.append(request)
            return await super().complete(request)

    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda *_args, **_kwargs: CapturingProvider(script=[
            _tool_call_response("update_progress_tool", {"updates": [{"path": "widget.py", "status": "doing"}]}, call_id="c1"),
            _text_response("all done"),
        ]),
    )

    plan = {"files_to_create": [{"path": "widget.py"}], "files_to_modify": [{"path": "README.md"}]}
    result = await run_dev(plan=plan, context_bundle={}, worktree_path=git_repo, project_path=git_repo)

    # Turn one sees the seeded board, turn two sees the model's own update.
    assert "[ ] create widget.py" in captured[0].messages[-1].content[0].text
    assert "[>] create widget.py" in captured[1].messages[-1].content[0].text
    # And it comes back out for the next fix round.
    assert result["dev_progress"] == [
        {"path": "widget.py", "action": "create", "status": "doing"},
        {"path": "README.md", "action": "modify", "status": "pending"},
    ]


async def test_dev_run_resumes_a_prior_checklist_instead_of_reseeding(git_repo: str, monkeypatch: pytest.MonkeyPatch):
    """A fix round must not report already-written files as pending."""
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    captured = []

    @dataclass
    class CapturingProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            captured.append(request)
            return await super().complete(request)

    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_args, **_kwargs: CapturingProvider(script=[_text_response("fixed")]))

    prior = [{"path": "widget.py", "action": "create", "status": "done"}]
    result = await run_dev(
        plan={"files_to_create": [{"path": "widget.py"}]}, context_bundle={},
        worktree_path=git_repo, project_path=git_repo,
        prior_history=[{"role": "user", "content": [{"type": "text", "text": "earlier"}]}],
        prior_progress=prior,
    )

    assert "[x] create widget.py" in captured[0].messages[-1].content[0].text
    assert result["dev_progress"][0]["status"] == "done"
    # The caller's list is not mutated in place across runs.
    assert prior[0]["status"] == "done"


def test_changes_left_to_a_person_are_named_but_not_in_dev_s_plan():
    """A read-only repo's changes (plan_gate.normalize_plan) are a person's:
    dev codes against them but neither makes them nor copies them elsewhere."""
    from forge.agents.roles.dev_loop import _build_prompt

    plan = {
        "files_to_modify": [{"path": "src/Search.java", "purpose": "columns"}],
        "read_only_changes": [{"path": "@contracts/orders.yaml", "purpose": "add the columns to the contract"}],
        "plan_markdown": "# Plan",
    }

    prompt = _build_prompt(continuing=False, plan=plan, context_bundle={}, brief="the brief")

    approved, _, rest = prompt.partition("\n\n")
    assert "read_only_changes" not in approved and "src/Search.java" in approved
    assert "Not yours to make" in rest
    assert "- @contracts/orders.yaml: add the columns to the contract" in rest


async def test_files_the_story_already_changed_stay_writable_off_the_plan(git_repo: str, tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A Retry keeps the story's worktrees, and the new plan may take their
    work as done without listing it. A fix there must still be possible —
    in the default repo and in another repo the plan does not even name."""
    import subprocess

    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config

    portal = tmp_path / "portal"
    subprocess.run(["git", "-C", git_repo, "worktree", "add", "-q", "-b", "feat/p", str(portal)], check=True)
    (portal / "export.ts").write_text("export const x = 1;\n")  # the earlier run's portal work
    with open(f"{git_repo}/Search.java", "w") as handle:  # ...and its backend work
        handle.write("class Search {}\n")
    set_config("openai_api_key", "sk-test")
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda *_args, **_kwargs: FakeProvider(script=[
            _tool_call_response("edit_file_tool", {"path": "@portal/export.ts", "target_content": "x = 1", "replacement_content": "x = 2"}, "c1"),
            _tool_call_response("edit_file_tool", {"path": "Search.java", "target_content": "{}", "replacement_content": "{ int n; }"}, "c2"),
            _tool_call_response("write_file_tool", {"path": "Other.java", "content": "class Other {}\n"}, "c3"),
            _text_response("fixed"),
        ]),
    )

    await run_dev(
        plan={"files_to_modify": [{"path": "README.md"}]}, context_bundle={}, worktree_path=git_repo, project_path=git_repo,
        default_label="api", repo_roots={"api": git_repo, "portal": str(portal)},
        story_worktrees={"portal": {"repo_path": git_repo, "worktree_path": str(portal), "branch": "feat/p"}},
    )

    assert (portal / "export.ts").read_text() == "export const x = 2;\n"
    assert open(f"{git_repo}/Search.java").read() == "class Search { int n; }\n"
    assert not os.path.exists(f"{git_repo}/Other.java")  # neither planned nor the story's: still refused


async def test_dev_runs_on_its_projects_model_and_asks_for_its_projects_provider(git_repo: str, monkeypatch: pytest.MonkeyPatch, project_id):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config, set_project_config

    set_config("openai_api_key", "sk-test")
    set_config("dev_sdk_model", "global-dev")
    set_project_config(project_id, "dev_sdk_model", "project-dev")
    captured, asked = [], []

    @dataclass
    class CapturingProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            captured.append(request.model)
            return await super().complete(request)

    def provider_for(project=None):
        asked.append(project)
        return CapturingProvider(script=[_text_response("done"), _text_response("done")])

    monkeypatch.setattr(dev_loop_module, "get_provider", provider_for)

    await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo, project_id=project_id)
    await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo)

    assert captured[0] == "project-dev" and captured[-1] == "global-dev"
    assert asked == [project_id, None]


async def test_an_explicit_model_beats_the_projects_choice(git_repo: str, monkeypatch: pytest.MonkeyPatch, project_id):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.config.store import set_config, set_project_config

    set_config("openai_api_key", "sk-test")
    set_project_config(project_id, "dev_sdk_model", "project-dev")
    captured = []

    @dataclass
    class CapturingProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            captured.append(request.model)
            return await super().complete(request)

    monkeypatch.setattr(dev_loop_module, "get_provider", lambda *_a, **_k: CapturingProvider(script=[_text_response("done")]))

    await run_dev(
        plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo,
        project_id=project_id, sdk_model="explicit",
    )

    assert captured == ["explicit"]
