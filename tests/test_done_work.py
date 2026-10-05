"""Work from tickets closed as done in the source, not in the default branch
yet (forge/daemon/main.py's _bring_done_work_into_epic and
_replan_behind_done_work). One such case: two item-search tickets Done
in JIRA, their branches unmerged, and the next story planning against a main
that had no item search at all. Real Postgres and git; agents are fakes."""
from __future__ import annotations

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import forge.daemon.main as daemon
from forge.config.store import set_config
from forge.orchestration.integration import integration_branch_name
from forge.services.run_activity import append_activity
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _push_branch(repo: str, branch: str, files: dict[str, str]) -> str:
    _git(repo, "checkout", "-q", "-b", branch, "main")
    for path, text in files.items():
        with open(os.path.join(repo, path), "w") as handle:
            handle.write(text)
        _git(repo, "add", path)
        _git(repo, "commit", "-q", "-m", f"{branch}: {path}")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", branch)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", branch)
    return tip


def _setup(tmp_path, project_id, repo, *, done: dict[str, str], pending: list[str], approval: bool = False) -> StateManager:
    """`done`: {story key: its status in the source}, closed before forge
    ever started them."""
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "true" if approval else "false")
    sm = StateManager()
    keys = [*done, *pending]
    sm.create_epic("ENG-1", project_id=project_id, levels=[keys], manual_gate=False, summary="Customer details")
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, repo))
    for key in keys:
        sm.create_story(key, epic_key="ENG-1", wave=0, summary=f"{key} work", depends_on=[])
    for key, status in done.items():
        sm.set_external_closed("story", key, closed=True, status_text=status)
        sm.set_story_status(key, "skipped", parked_reason="closed_externally")
    return sm


def _planner_log(monkeypatch) -> list[dict]:
    """A fake planner that records its calls and, like the real one, logs
    when planning starts."""
    import forge.orchestration.graph as graph
    from tests.conftest import fake_run_planner

    calls: list[dict] = []

    async def planner(**kwargs):
        append_activity(kwargs["run_id"], type="status", phase="planner", title="Planning started")
        calls.append(kwargs)
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph, "run_planner", planner)
    return calls


def _merge_result(sm: StateManager, key: str) -> dict:
    return sm.get_story(key)["prior_work"]["epic_merge"]["widgets"]


async def test_a_done_tickets_unmerged_work_is_in_the_epic_branch_before_any_story_branches(git_repo, tmp_path, project_id, monkeypatch):
    tip = _push_branch(git_repo, "feature/ENG-2-customer-api", {"CustomerSearchController.java": "class CustomerSearchController {}\n"})
    sm = _setup(tmp_path, project_id, git_repo, done={"ENG-2": "Done"}, pending=["ENG-3"])
    calls = _planner_log(monkeypatch)

    await daemon.poll_once(sm, wait=True)

    epic_branch = integration_branch_name("ENG-1")
    assert subprocess.run(["git", "merge-base", "--is-ancestor", tip, epic_branch], cwd=git_repo).returncode == 0
    assert "done in its source" in _git(git_repo, "log", "-1", "--format=%B", epic_branch)
    worktree = sm.get_story("ENG-3")["worktree_path"]
    assert os.path.exists(os.path.join(worktree, "CustomerSearchController.java"))  # what the planner reads
    assert calls[0]["project_path"] == worktree
    assert _merge_result(sm, "ENG-2")["result"] == "merged"
    assert sm.get_story("ENG-2")["status"] == "skipped"  # still not forge's to run
    assert "`feature/ENG-2-customer-api` is merged into this one" in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))

    await daemon.poll_once(sm, wait=True)  # nothing to do twice
    assert _git(git_repo, "rev-list", "--count", "--merges", epic_branch) == "1"


async def test_a_ticket_closed_as_removed_is_not_taken(git_repo, tmp_path, project_id):
    _push_branch(git_repo, "feature/ENG-2-abandoned", {"old.py": "x\n"})
    sm = _setup(tmp_path, project_id, git_repo, done={"ENG-2": "Removed"}, pending=["ENG-3"])

    await daemon.poll_once(sm, wait=True)

    assert _merge_result(sm, "ENG-2")["result"] == "not_done"
    assert not os.path.exists(os.path.join(sm.get_story("ENG-3")["worktree_path"], "old.py"))


async def test_done_work_that_conflicts_is_flagged_not_forced(git_repo, tmp_path, project_id):
    _push_branch(git_repo, "feature/ENG-2-readme", {"README.md": "from ENG-2\n"})
    _push_branch(git_repo, "feature/ENG-3-readme", {"README.md": "from ENG-3\n"})
    sm = _setup(tmp_path, project_id, git_repo, done={"ENG-2": "Done", "ENG-3": "Closed"}, pending=["ENG-4"])

    await daemon.poll_once(sm, wait=True)

    assert _merge_result(sm, "ENG-2")["result"] == "merged"
    conflict = _merge_result(sm, "ENG-3")
    assert conflict["result"] == "conflict" and "README.md" in conflict["detail"]
    epic_branch = integration_branch_name("ENG-1")
    assert _git(git_repo, "show", f"{epic_branch}:README.md") == "from ENG-2"  # never a half-merge
    worktree = daemon.existing_epic_worktree(str(tmp_path / "worktrees"), "ENG-1", git_repo)
    assert _git(worktree, "status", "--porcelain") == ""


async def test_a_plan_made_before_done_work_arrived_is_made_again(git_repo, tmp_path, project_id, monkeypatch):
    """ENG-2 is Done but its branch is only pushed after ENG-3 has planned
    (and asked a question): the next poll merges it and ENG-3 plans again,
    reading it — its question and answer cleared."""
    sm = _setup(tmp_path, project_id, git_repo, done={"ENG-2": "Done"}, pending=["ENG-3"], approval=True)
    calls = _planner_log(monkeypatch)

    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-3")["status"] == "awaiting_plan_approval" and len(calls) == 1
    sm.set_story_clarification_questions("ENG-3", [{"id": "q1", "question": "BE or FE?", "options": []}])

    _push_branch(git_repo, "feature/ENG-2-customer-api", {"CustomerSearchController.java": "class C {}\n"})
    TestClient(__import__("forge.app", fromlist=["create_app"]).create_app()).post("/api/epics/ENG-1/prior-work/refresh")
    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-3")
    assert len(calls) == 2  # planned again, in the same pass
    assert story["status"] == "awaiting_plan_approval" and story["clarification_questions"] == []
    assert os.path.exists(os.path.join(story["worktree_path"], "CustomerSearchController.java"))

    await daemon.poll_once(sm, wait=True)
    assert len(calls) == 2  # the new plan already has it: not made a third time


async def test_a_done_ticket_with_diverged_branches_waits_for_a_pick(git_repo, tmp_path, project_id):
    _push_branch(git_repo, "feature/ENG-2-a", {"a.py": "a\n"})
    _push_branch(git_repo, "ENG-2-b", {"b.py": "b\n"})
    sm = _setup(tmp_path, project_id, git_repo, done={"ENG-2": "Done"}, pending=["ENG-3"])

    await daemon.poll_once(sm, wait=True)
    assert _merge_result(sm, "ENG-2")["result"] == "choose"

    from forge.app import create_app

    picked = TestClient(create_app()).post("/api/stories/ENG-2/prior-work", json={"repo_label": "widgets", "branch": "ENG-2-b"})
    assert picked.status_code == 200
    await daemon.poll_once(sm, wait=True)

    assert _merge_result(sm, "ENG-2")["result"] == "merged"
    assert _git(git_repo, "show", f"{integration_branch_name('ENG-1')}:b.py") == "b"
