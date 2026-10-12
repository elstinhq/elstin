"""The PO agent's turn (elstin/agents/roles/po_loop.py), faked at the elstin/llm/
boundary (`get_provider`) like tests/test_planner_loop.py: the real prompts,
tool wiring, schema and draft gate all run; only the model is scripted."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

import elstin.agents.roles.po_loop as po_loop
from elstin.agents.roles.po_gate import DraftContext
from elstin.agents.roles.po_loop import PoLoopError, render_person_message, run_product_owner
from elstin.agents.roles.product_owner import PO_DRAFT_SCHEMA, PoInputs, build_po_system, build_po_user_text
from elstin.llm.base import Capabilities
from elstin.llm.types import LlmRequest, LlmResponse, TextBlock, ToolCallBlock, Usage
from elstin.services.run_activity import list_activity
from elstin.services.jira_service import JiraTicket
from elstin.tools.po_tools import PoResearch

pytestmark = pytest.mark.usefixtures("elstin_db")

RUN_ID = "po-7"


def _story(ref="s1", **overrides):
    story = {
        "ref": ref, "summary": f"Do thing {ref}", "description": f"Why {ref} exists.",
        "acceptance_criteria": [f"{ref} behaves"], "issue_type": "Story", "kind": "dev", "labels": [],
        "depends_on": [], "subtasks": [], "evidence": [], "rationale": "", "size": "M",
    }
    story.update(overrides)
    return story


def _draft(mode="brainstorm", **overrides):
    draft = {
        "mode": mode,
        "epic": {
            "summary": "Retry payments", "description": "Retry failed payments.", "acceptance_criteria": ["Retries happen"],
            "existing_key": None, "suggested_description": None,
        },
        "stories": [_story("s1"), _story("s2", depends_on=["s1"])],
        "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [], "questions": [], "reply": "Drafted two stories.",
    }
    draft.update(overrides)
    return draft


@dataclass
class FakeProvider:
    script: list
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list[LlmRequest] = field(default_factory=list)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _answer(draft) -> LlmResponse:
    return LlmResponse(
        content=[TextBlock(text="{}")], stop_reason="end_turn", usage=Usage(input_tokens=100, output_tokens=50),
        model="gpt-5.1", structured_output=draft,
    )


def _tool_call(name, **arguments) -> LlmResponse:
    return LlmResponse(
        content=[ToolCallBlock(id=f"call-{name}", name=name, arguments=arguments)], stop_reason="tool_calls",
        usage=Usage(input_tokens=100, output_tokens=20), model="gpt-5.1",
    )


def _configure(monkeypatch, provider) -> None:
    from elstin.config.store import set_config

    set_config("openai_api_key", "sk-test")
    set_config("planner_sdk_model", "gpt-5.1")
    # The investigation fan-out adds a scoping call and one per explorer; these tests
    # script an exact number of responses to exercise the drafting loop, so they opt out.
    set_config("po_explorer_max_agents", "0")
    monkeypatch.setattr(po_loop, "get_provider", lambda *_args, **_kwargs: provider)


def _ctx(mode="brainstorm", **overrides):
    values = {"mode": mode, "epic_key": None if mode == "brainstorm" else "PAY-1", "epic_summary": "Retry payments"}
    values.update(overrides)
    return DraftContext(**values)


async def _run(git_repo, provider=None, *, mode="brainstorm", inputs=None, **overrides):
    kwargs = dict(
        inputs=inputs or PoInputs(mode=mode, person_brief="Plan payment retries"), session_id=7, run_id=RUN_ID,
        project_id=None, ctx=_ctx(mode), research=PoResearch(), project_path=git_repo,
        repo_roots={"widgets": git_repo}, default_label="widgets",
    )
    kwargs.update(overrides)
    return await run_product_owner(**kwargs)


def _events(title_part):
    return [e for e in list_activity(RUN_ID) if title_part in e["title"]]


# -- the happy path ----------------------------------------------------------------------------------


async def test_a_clean_draft_comes_back_marked_and_the_turn_is_recorded(monkeypatch, git_repo):
    provider = FakeProvider(script=[_answer(_draft())])
    _configure(monkeypatch, provider)

    result = await _run(git_repo)

    assert result.draft["stories"][1]["depends_on"] == ["s1"]
    assert result.reply == "Drafted two stories." and result.questions == [] and result.attempts == 1
    assert [d for d in result.defects if d.get("blocking", True)] == []
    request = provider.calls[0]
    assert request.response_format.name == "po_draft" and request.response_format.schema == PO_DRAFT_SCHEMA
    # The planner's own code tools, and none of JIRA / Confluence / the board (none handed in).
    assert {t.name for t in request.tools} == {"read_file_tool", "outline_file_tool", "grep_tool", "find_files_tool"}
    assert "MODE: brainstorm" in request.messages[0].content[0].text
    assert request.system == build_po_system("brainstorm")

    from elstin.services.usage import _usage_for_run

    usage = _usage_for_run(RUN_ID)
    assert [p["phase"] for p in usage["phases"]] == ["po"]
    assert usage["phases"][0]["totals"]["input_tokens"] == 100
    assert all(e["phase"] == "po" for e in list_activity(RUN_ID))
    assert _events("Drafting the backlog")


async def test_the_po_model_is_used_before_the_planners(monkeypatch, git_repo):
    from elstin.config.store import set_config

    provider = FakeProvider(script=[_answer(_draft())])
    _configure(monkeypatch, provider)
    set_config("po_model", "po-model-x")
    await _run(git_repo)
    assert provider.calls[0].model == "po-model-x"

    provider.script = [_answer(_draft())]
    set_config("po_model", "")
    await _run(git_repo)
    assert provider.calls[1].model == "gpt-5.1"


# -- the gate and its retries --------------------------------------------------------------------------


async def test_a_rejected_draft_is_sent_back_with_what_failed_and_the_agent_keeps_its_research(monkeypatch, git_repo):
    bad = _draft(stories=[_story("s1", acceptance_criteria=[]), _story("s2", depends_on=["s9"])])
    provider = FakeProvider(script=[_tool_call("find_files_tool", glob_pattern="*.md"), _answer(bad), _answer(_draft())])
    _configure(monkeypatch, provider)

    result = await _run(git_repo)

    assert result.attempts == 2 and [d for d in result.defects if d.get("blocking", True)] == []
    first, second = provider.calls[1], provider.calls[2]
    feedback = second.messages[-1].content[0].text
    assert "YOUR PREVIOUS DRAFT WAS REJECTED" in feedback and "attempt 1/3" in feedback
    assert "story s1 has no acceptance criteria" in feedback
    assert "story s2 depends on 's9'" in feedback
    # The retry continues the conversation: the research is still in it, and so is the rejected draft.
    assert len(second.messages) == len(first.messages) + 2
    assert second.messages[0] == first.messages[0]
    assert [m.role for m in second.messages].count("tool") == 1
    gate = _events("Draft gate")
    assert gate and "sending it back" in gate[0]["title"] and "no_criteria" in gate[0]["meta"]["kinds"]


async def test_a_draft_that_is_only_imperfect_after_the_last_retry_is_kept_with_its_problems(monkeypatch, git_repo):
    cyclic = _draft(stories=[_story("s1", depends_on=["s2"]), _story("s2", depends_on=["s1"])])
    provider = FakeProvider(script=[_answer(cyclic), _answer(cyclic), _answer(cyclic)])
    _configure(monkeypatch, provider)

    result = await _run(git_repo)

    assert result.attempts == 3 and len(provider.calls) == 3
    hard = [d for d in result.defects if d.get("blocking", True)]
    assert [d["kind"] for d in hard] == ["dependency_cycle"]
    assert result.draft["stories"][0]["ref"] == "s1"  # the person gets it, problem showing


async def test_an_answer_that_is_not_a_draft_object_fails_the_turn(monkeypatch, git_repo):
    mangled = _draft()
    mangled["stories"] = "[]"
    provider = FakeProvider(script=[_answer(mangled)] * 3)
    _configure(monkeypatch, provider)

    with pytest.raises(PoLoopError, match="failed after 3 attempt"):
        await _run(git_repo)
    assert "not a well-formed draft object" in provider.calls[1].messages[-1].content[0].text


async def test_a_run_that_ends_without_a_draft_is_told_to_answer_with_one(monkeypatch, git_repo):
    prose = LlmResponse(
        content=[TextBlock(text="I looked around.")], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5),
        model="gpt-5.1", structured_output=None,
    )
    provider = FakeProvider(script=[prose, _answer(_draft())])
    _configure(monkeypatch, provider)

    result = await _run(git_repo)
    assert result.attempts == 2
    assert "you did not answer with the draft object" in provider.calls[1].messages[-1].content[0].text


async def test_a_wrong_mode_is_sent_back(monkeypatch, git_repo):
    provider = FakeProvider(script=[_answer(_draft("gap")), _answer(_draft("brainstorm"))])
    _configure(monkeypatch, provider)
    result = await _run(git_repo)
    assert result.attempts == 2
    assert "is in 'brainstorm' mode" in provider.calls[1].messages[-1].content[0].text


# -- evidence ------------------------------------------------------------------------------------------------


class _FakeJira:
    name = "jira"

    def search_tickets(self, query, max_results=50):
        return []

    def get_ticket_tree(self, key, max_tickets=None):
        return [JiraTicket(
            key="PAY-14", summary="Retries", description="Failed payments are retried three times.", status="Done",
            issue_type="Story", priority=None,
        )]


async def test_a_ticket_the_agent_read_can_be_cited_and_one_it_did_not_cannot(monkeypatch, git_repo):
    cited = _draft(stories=[_story("s1", evidence=[{"source": "jira", "ref": "PAY-14", "quote": "retried three times"}])])
    uncited = _draft(stories=[_story("s1", evidence=[{"source": "jira", "ref": "PAY-99", "quote": "anything"}])])
    provider = FakeProvider(script=[
        _tool_call("jira_get_issue", key="PAY-14"), _answer(uncited), _answer(cited),
    ])
    _configure(monkeypatch, provider)
    research = PoResearch()

    result = await _run(git_repo, ticket_source=_FakeJira(), jira_project_key="PAY", research=research)

    assert result.attempts == 2
    assert "jira_get_issue" in {t.name for t in provider.calls[0].tools}
    feedback = provider.calls[2].messages[-1].content[0].text
    assert "PAY-99" in feedback and "you did not read this session" in feedback
    assert result.draft["stories"][0]["evidence"][0]["verified"] is True
    assert research.knows_jira("PAY-14")
    assert _events("Called tool: jira_get_issue")


async def test_code_evidence_is_checked_against_the_files(monkeypatch, git_repo):
    good = _draft(stories=[_story("s1", evidence=[{"source": "code", "ref": "README.md", "quote": "hello"}])])
    bad = _draft(stories=[_story("s1", evidence=[{"source": "code", "ref": "README.md", "quote": "goodbye"}])])
    provider = FakeProvider(script=[_answer(bad), _answer(good)])
    _configure(monkeypatch, provider)

    result = await _run(git_repo)
    assert result.attempts == 2
    assert "not in README.md" in provider.calls[1].messages[-1].content[0].text
    assert result.draft["stories"][0]["evidence"][0]["verified"] is True


async def test_a_ticket_found_by_a_search_may_be_depended_on(monkeypatch, git_repo):
    class Source(_FakeJira):
        def search_tickets(self, query, max_results=50):
            return [JiraTicket(key="OTHER-5", summary="Ledger", description="", status="Done", issue_type="Story", priority=None)]

    draft = _draft(stories=[_story("s1", depends_on=["OTHER-5"])])
    provider = FakeProvider(script=[_tool_call("jira_search", jql="text ~ ledger"), _answer(draft)])
    _configure(monkeypatch, provider)
    result = await _run(git_repo, ticket_source=Source(), jira_project_key="PAY")
    assert result.attempts == 1


# -- the person and the budget --------------------------------------------------------------------------------------


async def test_a_message_sent_while_the_agent_works_joins_the_conversation_before_its_next_step(monkeypatch, git_repo):
    sent = {"queue": [render_person_message("Use Stripe, not Adyen.")]}

    def pending():
        taken, sent["queue"] = sent["queue"], []
        return taken

    provider = FakeProvider(script=[_tool_call("find_files_tool", glob_pattern="*.md"), _answer(_draft())])
    _configure(monkeypatch, provider)
    await _run(git_repo, pending_messages=pending)

    first_request = provider.calls[0]
    assert "Use Stripe, not Adyen." in first_request.messages[-1].content[0].text
    assert "you are only reading and drafting" in first_request.messages[-1].content[0].text


async def test_a_session_whose_budget_is_used_up_does_not_start_another_turn(monkeypatch, git_repo):
    from elstin.config.store import set_config

    provider = FakeProvider(script=[])
    _configure(monkeypatch, provider)
    set_config("po_max_budget_usd", "2.00")

    with pytest.raises(PoLoopError, match="spend ceiling"):
        await _run(git_repo, spent_usd=2.0)
    assert provider.calls == []

    # Under the ceiling it runs, and a ceiling of zero is unbounded.
    provider.script = [_answer(_draft())]
    await _run(git_repo, spent_usd=1.5)
    set_config("po_max_budget_usd", "0")
    provider.script = [_answer(_draft())]
    await _run(git_repo, spent_usd=999.0)


async def test_without_a_provider_the_turn_fails_with_the_reason(monkeypatch, git_repo):
    from elstin.llm.registry import LlmNotConfiguredError

    def broken(*_args, **_kwargs):
        raise LlmNotConfiguredError("No API key is set")

    monkeypatch.setattr(po_loop, "get_provider", broken)
    with pytest.raises(PoLoopError, match="No API key is set"):
        await _run(git_repo)


# -- revising ----------------------------------------------------------------------------------------------------------


async def test_a_revision_is_told_to_keep_a_persons_edits_and_is_shown_the_draft_and_the_conversation(monkeypatch, git_repo):
    current = {k: v for k, v in _draft().items() if k not in ("reply", "questions")}
    current["stories"][0]["summary"] = "A person's own wording"
    inputs = PoInputs(
        mode="brainstorm", person_brief="Plan payment retries",
        revise={
            "draft": current, "version": 4, "edited_by_person": True, "person_edits": ["stories.s1.summary"],
            "history": [{"role": "agent", "text": "Drafted two stories."}], "answers": [{"question": "Which provider?", "answer": "Stripe"}],
            "new_messages": ["Add a notification story."],
        },
    )
    provider = FakeProvider(script=[_answer(_draft())])
    _configure(monkeypatch, provider)

    await _run(git_repo, inputs=inputs)

    request = provider.calls[0]
    assert request.system == build_po_system("brainstorm", revising=True)
    assert "Their edits win" in request.system
    text = request.messages[0].content[0].text
    assert "CURRENT DRAFT (version 4)" in text and "A person's own wording" in text
    assert "A person has edited this draft by hand" in text and "stories.s1.summary" in text
    assert "You (PO agent): Drafted two stories." in text
    assert "Q: Which provider?" in text and "A: Stripe" in text
    assert "Add a notification story." in text
    assert _events("Revising the draft")


async def test_a_revision_does_not_investigate_the_code_again(monkeypatch, git_repo):
    from elstin.config.store import set_config

    provider = FakeProvider(script=[_answer(_draft())])
    _configure(monkeypatch, provider)
    set_config("po_explorer_max_agents", "3")  # on — but a revision already has the code in front of it
    inputs = PoInputs(mode="brainstorm", person_brief="x", revise={"draft": {}, "version": 1, "new_messages": ["hi"]})
    await _run(git_repo, inputs=inputs)
    assert len(provider.calls) == 1


async def test_the_first_turn_investigates_the_code_in_parallel_first(monkeypatch, git_repo):
    from elstin.config.store import set_config

    scope = LlmResponse(
        content=[TextBlock(text="{}")], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model="gpt-5.1",
        structured_output={"areas": [{"name": "retry code", "question": "How does the current retry work?", "path_hints": []}]},
    )
    report = LlmResponse(
        content=[TextBlock(text="`README.md:1` says hello.")], stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5), model="gpt-5.1",
    )
    provider = FakeProvider(script=[scope, report, _answer(_draft())])
    _configure(monkeypatch, provider)
    set_config("po_explorer_max_agents", "2")

    await _run(git_repo)

    # The scoping and the explorer speak for a backlog, not a plan…
    assert "backlog a product owner is about to write" in provider.calls[0].system
    assert "helping a product owner write a backlog" in provider.calls[1].system
    assert provider.calls[0].messages[0].content[0].text.startswith("Brief and repository map:")
    # …and what they found is in the draft turn's prompt.
    final = provider.calls[2].messages[0].content[0].text
    assert "REPOSITORY INVESTIGATION (read-only agents, run in parallel before drafting)" in final
    assert "`README.md:1` says hello." in final
    # Their work is filed under the PO's own phase.
    assert {"po"} == {e["phase"] for e in list_activity(RUN_ID)}
    assert _events("Scoped 1 parallel investigation")


# -- the prompt itself ---------------------------------------------------------------------------------------------------


def test_the_prompt_says_what_a_elstin_reviewer_can_and_cannot_check():
    """Each story's criteria are what elstin's own reviewer later holds a diff to.
    That reviewer sees the repository and the build — not production, not JIRA —
    so an author who is not told writes criteria that fail a story after three rounds."""
    system = build_po_system("split")
    assert "cannot reach a production database" in system and "NEVER write a criterion" in system
    assert "small enough for ONE agent run" in system
    assert "COPIED VERBATIM" in system
    assert "you never rewrite the epic" in system


def test_each_mode_is_worded_and_only_a_revision_carries_the_revise_rules():
    assert "MODE: brainstorm" in build_po_system("brainstorm") and "Problem" in build_po_system("brainstorm")
    assert "MODE: split" in build_po_system("split")
    gap = build_po_system("gap")
    assert "MODE: gap" in gap and "ONE entry for EVERY existing story" in gap and "never rewrite an existing ticket" in gap
    assert "REVISING" not in gap and "REVISING" in build_po_system("gap", revising=True)


def test_the_user_text_carries_every_input_it_was_given():
    inputs = PoInputs(
        mode="gap", person_brief="Check the retry epic", epic_key="PAY-1", epic_context="PAY-1 — Retry payments",
        board_context="elstin's board", linked_pages="Retry design page", skeleton="=== CODEBASE ===",
        guide={"found": True, "content": "Layout: src/", "generated": True, "lessons": [{"text": "Use uv", "scope_globs": []}]},
        capabilities=["JIRA (jira_search)"],
    )
    text = build_po_user_text(inputs, "=== REPOSITORY INVESTIGATION ===")
    for part in (
        "MODE: gap", "EPIC: PAY-1", "Tools available beyond the code: JIRA (jira_search)", "Check the retry epic",
        "elstin generated this guide", "Layout: src/", "Use uv", "PAY-1 — Retry payments", "elstin's board",
        "Retry design page", "=== CODEBASE ===", "=== REPOSITORY INVESTIGATION ===",
    ):
        assert part in text, part
    assert "CURRENT DRAFT" not in text


def test_the_inputs_are_not_mutated_by_building_the_text():
    inputs = PoInputs(mode="split", person_brief="x", revise={"draft": {"stories": []}, "version": 1})
    before = copy.deepcopy(inputs.revise)
    build_po_user_text(inputs)
    assert inputs.revise == before


# -- bug mode ---------------------------------------------------------------------------------------


def _bug_draft(**bug_overrides):
    bug = {
        "summary": "Report shows the list price", "observed": "List price shown.", "expected": "Sale price shown.",
        "steps_to_reproduce": [], "root_cause": "It reads the list price.", "confidence": "low", "cause_in": "code",
        "fix_location": "report.py", "to_confirm": ["Look at unit X's sale record"],
        "acceptance_criteria": ["The report shows the sale price"], "labels": [], "evidence": [],
    }
    bug.update(bug_overrides)
    return {"mode": "bug", "bug": bug, "assumptions": [], "out_of_scope": [], "questions": [], "reply": "Traced it."}


async def test_a_bug_turn_answers_in_the_bug_schema_under_the_debuggers_prompt(monkeypatch, git_repo):
    from elstin.agents.roles.product_owner import BUG_DRAFT_SCHEMA

    provider = FakeProvider(script=[_answer(_bug_draft())])
    _configure(monkeypatch, provider)

    result = await _run(git_repo, mode="bug", ctx=_ctx("bug", epic_key=None))

    assert result.draft["bug"]["summary"] == "Report shows the list price" and result.reply == "Traced it."
    request = provider.calls[0]
    assert request.response_format.schema == BUG_DRAFT_SCHEMA
    assert request.system == build_po_system("bug") and "debugging a reported fault" in request.system
    assert "=== THE BUG AS A PERSON REPORTED IT ===" in request.messages[0].content[0].text
    assert _events("Investigating the bug")


async def test_a_confident_bug_with_no_code_cited_is_sent_back(monkeypatch, git_repo):
    provider = FakeProvider(script=[
        _answer(_bug_draft(confidence="high")),
        _answer(_bug_draft(confidence="low")),
    ])
    _configure(monkeypatch, provider)

    result = await _run(git_repo, mode="bug", ctx=_ctx("bug", epic_key=None))

    assert result.attempts == 2 and result.draft["bug"]["confidence"] == "low"
    assert "without the code that shows it" in provider.calls[1].messages[-1].content[0].text


def test_the_bug_prompt_is_a_debuggers_and_keeps_the_reviewers_limits():
    system = build_po_system("bug")
    assert "find out WHY from the code" in system and "to_confirm" in system and "cause_in" in system
    assert "NEVER write a criterion that needs one of those" in system
    assert "MODE:" not in system and "Writing a story" not in system
    assert "REVISING" in build_po_system("bug", revising=True) and "bug ticket" in build_po_system("bug", revising=True)
    text = build_po_user_text(PoInputs(mode="bug", person_brief="Price inflated", epic_key="PAY-1"))
    assert "EPIC: PAY-1 (the bug is filed under it)" in text and "return the bug ticket as one JSON object" in text
