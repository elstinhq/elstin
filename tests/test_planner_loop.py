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
    "read_only_changes": [],
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


def _text_response(text: str) -> LlmResponse:
    """A plain prose reply — what an explorer sub-agent returns."""
    return LlmResponse(
        content=[TextBlock(text=text)], stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=50), model="gpt-5.1",
    )


def _no_plan_response() -> LlmResponse:
    """A run that finished cleanly but produced no plan object."""
    return LlmResponse(
        content=[TextBlock(text="I need more information")], stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=50), model="gpt-5.1", structured_output=None,
    )


def _configure(monkeypatch, provider, *, explorers: bool = False) -> None:
    from forge.config.store import set_config

    set_config("openai_api_key", "sk-test")
    # Explorers are on by default in production, but they add a scoping
    # call plus one call per explorer ahead of the plan loop. Every test
    # below scripts an exact number of provider responses to exercise the
    # PLAN loop, so they opt out; the explorer path has its own tests at
    # the bottom of this file and in tests/test_explorer.py.
    set_config("planner_explorers_enabled", "true" if explorers else "false")
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


async def test_a_plan_naming_no_files_is_rejected_and_retried_with_the_reason(monkeypatch, git_repo):
    """The bug this exists for: a plan with no files and no acceptance
    criteria passes every later check vacuously — the deterministic gate
    loops over the plan's files and finds none to fail, and the review is
    asked to reject only a "planned acceptance criterion clearly unmet", of
    which there are none. The story then reaches the PR gate green with an
    empty diff. Nothing downstream can catch it, so the planner must."""
    empty = {**_MINIMAL_PLAN, "files_to_create": [], "files_to_modify": [], "acceptance_criteria": []}
    provider = FakeProvider(script=[_plan_response(empty), _plan_response()])
    _configure(monkeypatch, provider)

    result = await run_planner(user_request="Show the currency next to each price", project_path=git_repo)

    assert result["plan"]["files_to_create"] == [{"path": "widget.py", "purpose": "the widget"}]
    assert len(provider.calls) == 2
    # The retry starts from a fresh history, so the defect has to travel in
    # the prompt or the second attempt just repeats the first.
    retry_prompt = provider.calls[1].messages[0].content[-1].text
    assert "YOUR PREVIOUS PLAN WAS REJECTED" in retry_prompt
    assert "names no files to create and none to modify" in retry_prompt
    assert "states no acceptance criteria" in retry_prompt


async def test_a_plan_with_files_but_no_acceptance_criteria_is_rejected(monkeypatch, git_repo):
    """Half the vacuous pass on its own: the gate has files to check, but
    the review's whole rule is about criteria it would not have."""
    provider = FakeProvider(script=[_plan_response({**_MINIMAL_PLAN, "acceptance_criteria": []}), _plan_response()])
    _configure(monkeypatch, provider)

    await run_planner(user_request="Add a widget", project_path=git_repo)

    retry_prompt = provider.calls[1].messages[0].content[-1].text
    assert "states no acceptance criteria" in retry_prompt
    assert "names no files" not in retry_prompt


async def test_an_unusable_plan_that_never_improves_fails_the_run(monkeypatch, git_repo):
    """Better a story a human can see failed than a green PR with nothing
    in it."""
    from forge.config.store import set_config

    empty = {**_MINIMAL_PLAN, "files_to_create": [], "files_to_modify": [], "acceptance_criteria": []}
    provider = FakeProvider(script=[_plan_response(empty), _plan_response(empty)])
    _configure(monkeypatch, provider)
    set_config("planner_sdk_max_retries", "1")  # -> 2 attempts

    with pytest.raises(PlannerLoopError, match="plan gate rejected the plan"):
        await run_planner(user_request="Add a widget", project_path=git_repo)

    assert len(provider.calls) == 2


async def test_a_plan_naming_a_file_that_does_not_exist_is_rejected_with_the_real_path(monkeypatch, git_repo):
    """End to end through the real plan gate: the planner names a path it
    never verified, the gate resolves it against the repo the planner's own
    tools read, and the retry carries the correction rather than a scolding."""
    import os

    os.makedirs(os.path.join(git_repo, "pricing-web"), exist_ok=True)
    with open(os.path.join(git_repo, "pricing-web", "openapi.json"), "w", encoding="utf-8") as handle:
        handle.write("{}\n")

    guessed = {**_MINIMAL_PLAN, "files_to_create": [], "files_to_modify": [{"path": "openapi.json", "purpose": "add currency"}]}
    provider = FakeProvider(script=[_plan_response(guessed), _plan_response()])
    _configure(monkeypatch, provider)

    result = await run_planner(user_request="Show the currency", project_path=git_repo)

    assert result["plan"]["files_to_create"] == [{"path": "widget.py", "purpose": "the widget"}]
    retry_prompt = provider.calls[1].messages[0].content[-1].text
    assert "openapi.json is listed under files_to_modify" in retry_prompt
    assert "a file of that name exists at: pricing-web/openapi.json" in retry_prompt


async def test_a_change_in_a_read_only_repo_is_set_aside_not_rejected(monkeypatch, git_repo, tmp_path):
    """MKT2-866: the plan needed a contract change in a read-only repo, and
    another repo's paths without their `@`. Both are fixed in place — one
    attempt, no retry — and the fixes are in the activity feed."""
    import os

    from forge.services.run_activity import list_activity

    portal, contracts = tmp_path / "portal", tmp_path / "contracts"
    (portal / "src").mkdir(parents=True)
    (portal / "src" / "search.ts").write_text("x\n")
    contracts.mkdir()
    (contracts / "orders.yaml").write_text("openapi: 3.0\n")
    with open(os.path.join(git_repo, "widget.py"), "w", encoding="utf-8") as handle:
        handle.write("x\n")
    plan = {
        **_MINIMAL_PLAN,
        "files_to_create": [],
        "files_to_modify": [
            {"path": "widget.py", "purpose": "the API"},
            {"path": "portal/src/search.ts", "purpose": "the columns"},
            {"path": "@contracts/orders.yaml", "purpose": "the contract"},
        ],
    }
    provider = FakeProvider(script=[_plan_response(plan)])
    _configure(monkeypatch, provider)

    result = await run_planner(
        user_request="Customer search columns", project_path=git_repo, run_id="ENG-9", default_label="app",
        repo_roots={"app": git_repo, "portal": str(portal), "contracts": str(contracts)}, read_only_labels=["contracts"],
    )

    assert len(provider.calls) == 1
    assert [e["path"] for e in result["plan"]["files_to_modify"]] == ["widget.py", "@portal/src/search.ts"]
    assert result["plan"]["read_only_changes"] == [{"path": "@contracts/orders.yaml", "purpose": "the contract"}]
    adjusted = [e for e in list_activity("ENG-9") if e["title"].startswith("Plan paths")]
    assert len(adjusted) == 1 and "@contracts/orders.yaml is in a read-only repo" in adjusted[0]["detail"]


async def test_a_plan_asking_clarification_questions_may_be_empty(monkeypatch, git_repo):
    """A question round-trip's plan is a placeholder until the human
    answers — planner_node parks it at awaiting_clarification and it never
    reaches dev, so the emptiness is correct rather than a defect."""
    asking = {
        **_MINIMAL_PLAN,
        "files_to_create": [], "files_to_modify": [], "acceptance_criteria": [],
        "questions": [{"id": "q0", "question": "Which layer?", "context": None, "options": []}],
    }
    provider = FakeProvider(script=[_plan_response(asking)])
    _configure(monkeypatch, provider)

    result = await run_planner(user_request="Add a widget", project_path=git_repo)

    assert len(result["questions"]) == 1
    assert len(provider.calls) == 1


async def test_stub_placeholder_questions_are_rejected_and_retried(monkeypatch, git_repo):
    """Regression for MKT2-858: the planner emitted a question whose id,
    question, and option label were all literally "placeholder", and it
    reached the human as if it were a real clarification question because
    a clarification round-trip skips the plan gate entirely. It must now
    be rejected like any other bad plan, with a correction telling the
    planner what a real question looks like."""
    stub = {
        **_MINIMAL_PLAN,
        "files_to_create": [], "files_to_modify": [], "acceptance_criteria": [],
        "questions": [{
            "id": "placeholder", "question": "placeholder", "context": "placeholder",
            "options": [{"id": "A", "label": "placeholder", "description": None}],
        }],
    }
    provider = FakeProvider(script=[_plan_response(stub), _plan_response()])
    _configure(monkeypatch, provider)

    result = await run_planner(user_request="Add a widget", project_path=git_repo)

    assert result["questions"] == []  # the retry's clean plan won
    assert len(provider.calls) == 2
    retry_prompt = provider.calls[1].messages[0].content[-1].text
    assert "stub" in retry_prompt
    assert "'placeholder'" in retry_prompt


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
    assert tool_names == {"read_file_tool", "outline_file_tool", "grep_tool", "find_files_tool"}
    # No write/edit tool may ever reach the planner.
    assert not any("write" in name or "edit" in name for name in tool_names)


async def test_not_configured_raises_planner_error(git_repo):
    with pytest.raises(PlannerLoopError, match="No OpenAI API key"):
        await run_planner(user_request="go", project_path=git_repo)


# --- explorer fan-out (forge/agents/roles/explorer.py) ------------------


def _scope_response(*areas: dict) -> LlmResponse:
    import json

    payload = {"areas": list(areas)}
    return LlmResponse(
        content=[TextBlock(text=json.dumps(payload))], stop_reason="end_turn",
        usage=Usage(input_tokens=20, output_tokens=5), model="gpt-5.1", structured_output=payload,
    )


async def test_explorer_findings_reach_the_planner_prompt(monkeypatch, git_repo):
    """End to end through the real explorer wiring: scope, two concurrent
    read-only agents, then the plan loop with their reports in its prompt."""
    provider = FakeProvider(script=[
        _scope_response(
            {"name": "cart UI layer", "question": "How is the cart rendered?", "path_hints": []},
            {"name": "api client", "question": "How are cart calls made?", "path_hints": ["README.md"]},
        ),
        _text_response("The cart renders in `cart.py:12`."),
        _text_response("Calls go through `client.py:40`."),
        _plan_response(),
    ])
    _configure(monkeypatch, provider, explorers=True)

    result = await run_planner(user_request="Add a widget", project_path=git_repo)

    assert result["plan"]["feature_summary"] == "Add a widget"
    assert len(provider.calls) == 4

    # The scoping call sees the ticket and asks for no tools.
    assert provider.calls[0].tools == []
    # Each explorer gets the read-only trio and its own standalone question.
    explorer_calls = provider.calls[1:3]
    for call in explorer_calls:
        assert {t.name for t in call.tools} == {"read_file_tool", "outline_file_tool", "grep_tool", "find_files_tool"}
    questions = {call.messages[0].content[0].text for call in explorer_calls}
    assert any("How is the cart rendered?" in q for q in questions)
    assert any("How are cart calls made?" in q for q in questions)
    # An explorer never learns about the plan or the other explorers.
    assert all("Add a widget" not in q for q in questions)

    # The plan loop's prompt carries the findings, not the raw file reads.
    plan_prompt = provider.calls[3].messages[0].content[0].text
    assert "REPOSITORY INVESTIGATION" in plan_prompt
    assert "cart.py:12" in plan_prompt
    assert "client.py:40" in plan_prompt
    assert "### cart UI layer" in plan_prompt


async def test_empty_scope_skips_the_fan_out_entirely(monkeypatch, git_repo):
    """A ticket that names its own files should cost one scoping call and
    nothing more — that is the whole reason scoping may return nothing."""
    provider = FakeProvider(script=[_scope_response(), _plan_response()])
    _configure(monkeypatch, provider, explorers=True)

    result = await run_planner(user_request="Fix the typo in widget.py", project_path=git_repo)

    assert result["plan"]["feature_summary"] == "Add a widget"
    assert len(provider.calls) == 2
    assert "REPOSITORY INVESTIGATION" not in provider.calls[1].messages[0].content[0].text


async def test_failed_scoping_does_not_fail_the_plan(monkeypatch, git_repo):
    """Exploration is a head start. Losing it must cost the planner a head
    start and nothing else."""
    provider = FakeProvider(script=[LlmBadRequestError("scope schema rejected"), _plan_response()])
    _configure(monkeypatch, provider, explorers=True)

    result = await run_planner(user_request="Add a widget", project_path=git_repo)

    assert result["plan"]["feature_summary"] == "Add a widget"
    assert "REPOSITORY INVESTIGATION" not in provider.calls[-1].messages[0].content[0].text


async def test_explorer_tokens_are_billed_to_the_planner_phase(monkeypatch, git_repo):
    """Sub-agent usage must roll up, or every planned story quietly
    under-reports its cost."""
    recorded = {}

    def fake_record(run_id, **kwargs):
        recorded.update(kwargs)

    monkeypatch.setattr(planner_module, "record_phase_usage", fake_record)
    provider = FakeProvider(script=[
        _scope_response({"name": "ui", "question": "How is it rendered?", "path_hints": []}),
        _text_response("Rendered in `a.py:1`."),
        _plan_response(),
    ])
    _configure(monkeypatch, provider, explorers=True)

    await run_planner(user_request="Add a widget", project_path=git_repo, run_id="ENG-1")

    # 20 (scope) + 100 (explorer) + 100 (plan) — all three calls counted.
    assert recorded["input_tokens"] == 220


async def test_clarification_round_trip_skips_exploration(monkeypatch, git_repo):
    """The codebase was already explored on the first pass; the only new
    input is the human's answer, so re-reading the repo is pure waste."""
    provider = FakeProvider(script=[_plan_response()])
    _configure(monkeypatch, provider, explorers=True)

    await run_planner(
        user_request="Add a widget", project_path=git_repo,
        clarification_answers=[{"question": "Which layer?", "option_label": "the UI"}],
    )

    assert len(provider.calls) == 1


async def test_answers_from_an_earlier_run_are_followed_in_a_fresh_plan(monkeypatch, git_repo):
    """MKT2-866: a person answered "leave out Webshop ID", the run then
    failed, and Retry's fresh plan never saw the answer. Carried into a new
    run, the answer is a decision to follow — but this run has not explored
    yet, and may still ask about something the answer does not settle."""
    provider = FakeProvider(script=[_scope_response(), _plan_response()])
    _configure(monkeypatch, provider, explorers=True)

    await run_planner(
        user_request="Export customers", project_path=git_repo, answers_from_earlier_run=True,
        clarification_answers=[{
            "question": "Include Webshop ID?", "option_label": "Use the proposed columns except Webshop ID",
            "option_description": "Export the first 24 columns",
        }],
    )

    assert len(provider.calls) == 2  # scoping ran
    prompt = provider.calls[1].messages[0].content[0].text
    assert "Decisions a person already made on this ticket (follow them; do not ask these again):" in prompt
    assert "- Q: Include Webshop ID?\n  A: Use the proposed columns except Webshop ID — Export the first 24 columns" in prompt
    assert "do NOT emit questions" not in prompt


async def test_exploration_and_the_plan_loop_split_one_planner_cap(monkeypatch, git_repo):
    """Regression: explorers were funded from half the cap and the plan
    loop was then handed the full cap again, so the phase could spend
    about 1.5x its limit and breach the story ceiling with it."""
    from forge.config.store import set_config

    provider = FakeProvider(script=[
        _scope_response({"name": "ui", "question": "How is it rendered?", "path_hints": []}),
        _text_response("Rendered in `a.py:1`."),
        _plan_response(),
    ])
    _configure(monkeypatch, provider, explorers=True)
    set_config("planner_sdk_max_budget_usd", "1.00")

    import forge.agents.roles.planner_loop as module

    budgets: list[float] = []
    real_loop = module.run_agent_loop

    async def capturing_loop(config, messages):
        budgets.append(config.max_budget_usd)
        return await real_loop(config, messages)

    monkeypatch.setattr(module, "run_agent_loop", capturing_loop)

    await run_planner(user_request="Add a widget", project_path=git_repo)

    # Scope, one explorer, then the plan loop all happened.
    assert len(provider.calls) == 3
    # Only the plan loop is routed through this module's own reference to
    # run_agent_loop (explorer.py imports its own), and it gets HALF the
    # cap — the other half having funded the fan-out — rather than the
    # whole cap a second time.
    assert budgets == [pytest.approx(0.50)]


async def test_the_plan_loop_keeps_the_whole_cap_when_nothing_was_explored(monkeypatch, git_repo):
    """No fan-out means no reservation to honour, so halving would be a
    silent, unasked-for tightening of the operator's cap."""
    import forge.agents.roles.planner_loop as module
    from forge.config.store import set_config

    captured = {}
    real_loop = module.run_agent_loop

    async def capturing_loop(config, messages):
        captured["budget"] = config.max_budget_usd
        return await real_loop(config, messages)

    monkeypatch.setattr(module, "run_agent_loop", capturing_loop)
    provider = FakeProvider(script=[_plan_response()])
    _configure(monkeypatch, provider, explorers=False)
    set_config("planner_sdk_max_budget_usd", "1.00")

    await run_planner(user_request="Add a widget", project_path=git_repo)

    assert captured["budget"] == pytest.approx(1.00)
