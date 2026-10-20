"""The "already satisfied" outcome: dev can say a planned file already does
what the ticket needs, instead of padding it with an edit to satisfy the gate.

One run did exactly that. The feature was already in the repo, dev noticed, and
then — because the gate demands every planned file be modified and had no
honest way to hear "this one needs nothing" — wrote javadoc comments and a
`required` marker "so every planned file is actually written". The pieces here:
the dev tool and its guardrails, the gate accepting a declaration (never for a
file that is missing), the reviewer being made to check the claim, and a run
that changed nothing ending as finished rather than trying to open an empty PR.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from elstin.agents.roles import verifier
from elstin.tools.dev_tools import BOOKKEEPING_TOOLS, MIN_DECLARATION_REASON_CHARS, PROGRESS_TOOLS, TOOL_SCHEMAS, make_dev_tools
from elstin.tools.progress import seed_progress_items

REASON = "PriceRecord already carries a `currency` component and PriceResponse maps it, so the response exposes it."


# --- the dev tool -----------------------------------------------------------


def _tools(root: Path, plan: dict, declared: dict | None):
    items = seed_progress_items(plan)
    functions, _schemas = make_dev_tools(
        worktree_path=str(root), allowed_paths={i["path"] for i in items}, analyze_targets=[], allow_test_run=False,
        planned_test_files=[], progress_items=items, declared_unchanged=declared,
    )
    return functions["declare_already_satisfied_tool"], items


def _repo_with(tmp_path: Path, *files: str) -> Path:
    for rel in files:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    return tmp_path


PLAN = {"files_to_modify": [{"path": "src/PriceRecord.java"}], "files_to_create": [{"path": "src/New.java"}]}


def test_declaring_a_planned_file_satisfied_records_it_and_marks_it_done(tmp_path):
    root = _repo_with(tmp_path, "src/PriceRecord.java")
    declared: dict[str, str] = {}
    declare, items = _tools(root, PLAN, declared)

    result = declare("src/PriceRecord.java", REASON)

    assert result.startswith("Recorded: src/PriceRecord.java needs no change")
    assert declared == {"src/PriceRecord.java": REASON}
    assert {i["path"]: i["status"] for i in items}["src/PriceRecord.java"] == "done"
    assert "PROGRESS CHECKLIST" in result  # shows the updated board, like update_progress_tool


@pytest.mark.parametrize(
    "path,reason,expected",
    [
        ("src/Other.java", REASON, "is not in the approved plan"),
        ("src/New.java", REASON, "the plan says to create src/New.java"),
        ("src/PriceRecord.java", "already done", "give the specific reason"),
        ("src/PriceRecord.java", "   ", "give the specific reason"),
    ],
)
def test_a_declaration_that_would_excuse_nothing_real_is_refused(tmp_path, path, reason, expected):
    root = _repo_with(tmp_path, "src/PriceRecord.java", "src/New.java", "src/Other.java")
    declared: dict[str, str] = {}
    declare, items = _tools(root, PLAN, declared)

    assert expected in declare(path, reason) and declared == {}
    assert all(i["status"] == "pending" for i in items)  # nothing was marked done


def test_a_file_that_is_not_there_cannot_already_satisfy_the_ticket(tmp_path):
    declare, _items = _tools(tmp_path, PLAN, {})

    assert "does not exist in the worktree" in declare("src/PriceRecord.java", REASON)


def test_the_tool_is_inert_when_the_run_does_not_track_declarations(tmp_path):
    root = _repo_with(tmp_path, "src/PriceRecord.java")
    declare, _ = _tools(root, PLAN, None)

    assert "not enabled for this run" in declare("src/PriceRecord.java", REASON)


def test_declaring_is_bookkeeping_and_not_progress(tmp_path):
    """It must not reset the idle-turn budget (that would let a model stall
    forever declaring), and it must not clear the repeat-call guard."""
    assert "declare_already_satisfied_tool" in BOOKKEEPING_TOOLS
    assert "declare_already_satisfied_tool" not in PROGRESS_TOOLS
    assert "declare_already_satisfied_tool" in {s["function"]["name"] for s in TOOL_SCHEMAS}


def test_the_tool_finds_a_file_in_another_repo_by_its_prefix(tmp_path):
    ui, api = tmp_path / "ui", tmp_path / "api"
    _repo_with(ui, "a.ts")
    _repo_with(api, "src/PriceRecord.java")
    plan = {"files_to_modify": [{"path": "@api/src/PriceRecord.java"}]}
    items = seed_progress_items(plan)
    declared: dict[str, str] = {}
    functions, _ = make_dev_tools(
        worktree_path=str(ui), allowed_paths={i["path"] for i in items}, analyze_targets=[], allow_test_run=False,
        planned_test_files=[], progress_items=items, declared_unchanged=declared, default_label="ui",
        write_roots={"ui": str(ui), "api": str(api)}, read_roots={"ui": str(ui), "api": str(api)},
    )

    assert functions["declare_already_satisfied_tool"]("@api/src/PriceRecord.java", REASON).startswith("Recorded")
    assert list(declared) == ["@api/src/PriceRecord.java"]


def test_the_dev_prompt_offers_the_honest_option_and_forbids_padding():
    from elstin.agents.roles.dev import DEV_SYSTEM_LOOP_CORE

    assert "declare_already_satisfied" in DEV_SYSTEM_LOOP_CORE
    assert "NEVER add a comment, rename or reformat just to make a planned file count as changed" in DEV_SYSTEM_LOOP_CORE
    assert MIN_DECLARATION_REASON_CHARS >= 20


# --- the gate ---------------------------------------------------------------


def _git_repo(path: Path, *files: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    for rel in files:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text("x\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return str(path)


def test_a_declared_unchanged_file_is_not_a_blocker_and_is_reported_for_the_reviewer(tmp_path):
    repo = _git_repo(tmp_path / "r", "README.md")
    plan = {"files_to_modify": [{"path": "README.md"}]}

    undeclared = verifier.compare_to_plan(plan, repo)
    declared = verifier.compare_to_plan(plan, repo, declared_unchanged={"README.md": REASON})

    assert undeclared["passed"] is False and undeclared["issues"][0]["kind"] == verifier.KIND_UNMODIFIED_TARGET
    assert declared["passed"] is True and declared["issues"] == []
    assert declared["assumed_satisfied"] == [{"path": "README.md", "reason": REASON}]


def test_a_declaration_never_excuses_a_missing_file_or_a_file_to_create(tmp_path):
    repo = _git_repo(tmp_path / "r", "README.md")
    plan = {"files_to_modify": [{"path": "nowhere.py"}], "files_to_create": [{"path": "new.py"}]}

    report = verifier.compare_to_plan(plan, repo, declared_unchanged={"nowhere.py": REASON, "new.py": REASON})

    assert {i["kind"] for i in report["issues"]} == {verifier.KIND_MISSING_TARGET, verifier.KIND_MISSING_NEW_FILE}
    assert report["assumed_satisfied"] == []


def test_a_file_that_was_in_fact_changed_is_not_reported_as_assumed(tmp_path):
    repo = _git_repo(tmp_path / "r", "README.md")
    Path(repo, "README.md").write_text("edited\n")

    report = verifier.compare_to_plan({"files_to_modify": [{"path": "README.md"}]}, repo, declared_unchanged={"README.md": REASON})

    assert report["passed"] is True and report["assumed_satisfied"] == []


@pytest.mark.asyncio
async def test_declarations_reach_the_right_repo_and_come_back_prefixed(tmp_path):
    ui = _git_repo(tmp_path / "ui", "README.md")
    api = _git_repo(tmp_path / "api", "README.md")
    plan = {"files_to_modify": [{"path": "README.md"}, {"path": "@api/README.md"}]}

    gate = await verifier.run_deterministic_gate(
        plan=plan, worktree_path=ui, file_changes=[], default_label="ui",
        story_worktrees={"ui": {"worktree_path": ui}, "api": {"worktree_path": api}},
        declared_unchanged={"README.md": "ui reason " * 4, "@api/README.md": "api reason " * 4},
    )

    assert gate["passed"] is True
    assert sorted(e["path"] for e in gate["assumed_satisfied"]) == ["@api/README.md", "README.md"]


def test_the_feedback_offers_declaring_only_where_a_file_may_really_need_no_change():
    def issue(kind, path):
        return {"severity": "blocker", "file": path, "reason": path, "kind": kind}

    unmodified = {"passed": False, "issues": [issue(verifier.KIND_UNMODIFIED_TARGET, "a.java")]}
    create_only = {"passed": False, "issues": [issue(verifier.KIND_MISSING_NEW_FILE, "b.js")]}

    offered = verifier.format_gate_feedback(unmodified, attempt=1, max_attempts=3, wrote_nothing=True)
    assert "declare_already_satisfied_tool(path, reason)" in offered
    assert "worse than declaring it" in offered
    # a file that must be created cannot already satisfy anything
    assert "declare_already_satisfied_tool" not in verifier.format_gate_feedback(create_only, attempt=1, max_attempts=3)


# --- the reviewer -----------------------------------------------------------


def test_the_review_prompt_tells_the_reviewer_to_check_declarations_and_not_to_fail_a_run_for_having_few_changes():
    prompt = " ".join(verifier._VERIFIER_REVIEW_CORE.split())  # the prompt wraps lines

    assert "assumed_satisfied" in prompt and "required_fixes" in prompt
    assert "Do not fail a run merely because it made few or no changes" in prompt


@pytest.mark.asyncio
@pytest.mark.usefixtures("elstin_db")
async def test_a_run_with_declarations_is_always_reviewed_even_when_trivial_diffs_are_skipped(git_repo, monkeypatch):
    """With the trivial-diff skip on, a run that changed nothing would pass
    unreviewed — exactly when the developer's claim most needs checking."""
    from elstin.config.store import set_config

    set_config("verifier_skip_llm_trivial_chars", "100000")
    reviewed: list[dict] = []

    async def fake_review(**kwargs):
        reviewed.append(kwargs)
        return {"passed": True, "issues": [], "required_fixes": []}, {}

    monkeypatch.setattr(verifier, "chat_completion_json", fake_review)
    plan = {"files_to_modify": [{"path": "README.md"}]}
    gate = verifier.compare_to_plan(plan, git_repo, declared_unchanged={"README.md": REASON})

    declared_run = await verifier.run_verifier(
        plan=plan, acceptance_criteria=["shows currency"], worktree_path=git_repo, file_changes=[], precomputed_gate=gate,
    )
    assert len(reviewed) == 1 and declared_run["verifier_report"]["passed"] is True
    assert "assumed_satisfied" in reviewed[0]["messages"][1]["content"] and REASON in reviewed[0]["messages"][1]["content"]

    trivial = verifier.compare_to_plan({"files_to_modify": []}, git_repo)
    await verifier.run_verifier(plan={"files_to_modify": []}, acceptance_criteria=[], worktree_path=git_repo, file_changes=[], precomputed_gate=trivial)
    assert len(reviewed) == 1  # the skip still applies when nothing was declared


# --- the graph and the daemon ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_declarations_travel_from_dev_to_the_gate_and_into_the_next_dev_round(database_url, monkeypatch):
    import uuid

    import elstin.orchestration.graph as graph_module
    from elstin.orchestration.graph import checkpointer_from_url, compile_graph
    from elstin.orchestration.state import initial_state

    declared = {"a.py": REASON}
    gate_calls: list[dict] = []
    dev_calls: list[dict] = []
    failing = {"passed": False, "issues": [{"severity": "blocker", "file": "b.py", "reason": "x", "kind": "unmodified_target"}], "required_fixes": ["x"]}
    gates = [failing, {"passed": True, "issues": [], "required_fixes": []}]

    async def scripted_gate(**kwargs):
        gate_calls.append(kwargs)
        return gates.pop(0)

    async def scripted_dev(**kwargs):
        dev_calls.append(kwargs)
        return {"file_changes": [{"path": "a.py"}], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}, "declared_unchanged": declared}

    async def passing_verifier(**kwargs):
        return {"verifier_report": {"passed": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_deterministic_gate", scripted_gate)
    monkeypatch.setattr(graph_module, "run_dev", scripted_dev)
    monkeypatch.setattr(graph_module, "run_verifier", passing_verifier)

    thread_id = str(uuid.uuid4())
    state = initial_state(thread_id, "Add a widget", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"
    async with checkpointer_from_url(database_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await graph.ainvoke(state, {"configurable": {"thread_id": thread_id}})

    assert gate_calls[0]["declared_unchanged"] == declared
    assert dev_calls[0]["prior_declared"] is None and dev_calls[1]["prior_declared"] == declared


async def _drive(git_repo, tmp_path, project_id, monkeypatch, *, dev_result):
    """ENG-2 taken through plan approval to the end of dev, with dev returning `dev_result`."""
    import elstin.orchestration.graph as graph_module
    from elstin.config.store import set_config
    from elstin.daemon.main import poll_once
    from elstin.state.manager import StateManager

    set_config("worktrees_base_dir", str(tmp_path / "wt"))
    set_config("openai_api_key", "sk-test")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Show the currency")

    async def dev(**kwargs):
        return {"messages": [], "dev_history": [], "dev_build_runner": {}, **dev_result}

    monkeypatch.setattr(graph_module, "run_dev", dev)
    await poll_once(sm, wait=True)           # dispatch -> plan gate
    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm, wait=True)           # dev, gate, verifier
    return sm


@pytest.mark.asyncio
@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_run_that_changed_nothing_because_the_files_already_satisfy_the_ticket_is_finished(git_repo, tmp_path, project_id, monkeypatch):
    sm = await _drive(git_repo, tmp_path, project_id, monkeypatch, dev_result={
        "file_changes": [], "truncated": False, "declared_unchanged": {"src/PriceRecord.java": REASON},
    })

    story = sm.get_story("ENG-2")
    assert (story["status"], story["parked_reason"], story["failure_stage"]) == ("done", "already_satisfied", None)
    assert sm.get_pull_request("ENG-2") is None  # nothing to open a pull request for
    from elstin.services.run_activity import list_activity

    finished = [e for e in list_activity("ENG-2") if e["title"].startswith("Finished with no changes")]
    assert len(finished) == 1 and "src/PriceRecord.java" in finished[0]["detail"] and REASON in finished[0]["detail"]

    from elstin.daemon.main import poll_once

    await poll_once(sm, wait=True)
    assert sm.get_epic("ENG-1")["phase"] == "complete"  # a finished story completes its epic


@pytest.mark.asyncio
@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_run_with_real_changes_still_goes_to_the_pr_gate_whatever_it_declared(git_repo, tmp_path, project_id, monkeypatch):
    sm = await _drive(git_repo, tmp_path, project_id, monkeypatch, dev_result={
        "file_changes": [{"path": "b.py"}], "truncated": False, "declared_unchanged": {"a.py": REASON},
    })

    assert sm.get_story("ENG-2")["status"] == "ready_to_merge"


@pytest.mark.asyncio
@pytest.mark.usefixtures("elstin_db", "fake_agents")
async def test_a_run_that_changed_nothing_and_declared_nothing_is_not_mistaken_for_satisfied(git_repo, tmp_path, project_id, monkeypatch):
    """Only an explicit, reasoned declaration ends a story with no changes."""
    sm = await _drive(git_repo, tmp_path, project_id, monkeypatch, dev_result={"file_changes": [], "truncated": False})

    assert sm.get_story("ENG-2")["status"] == "ready_to_merge"
