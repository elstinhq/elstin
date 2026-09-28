"""The merge queue (forge/daemon/main.py's `_merge_story_into_epic`, over
forge/orchestration/integration.py): a finished story is brought up to date
with the epic branch, re-verified, and fast-forwarded into it — locally.

Real Postgres, real git repos, the daemon's own poll loop. Only the agents
are fakes (`fake_agents`, conftest.py); each test's dev writes real files, so
what lands on the epic branch is real content."""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.config.store import set_config
from forge.daemon.main import _MAX_MERGE_ATTEMPTS, poll_once
from forge.orchestration.integration import integration_branch_name
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _configure(tmp_path) -> None:
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "false")


def _epic(sm: StateManager, project_id: int, git_repo: str, stories: list[str]) -> None:
    sm.create_epic("ENG-1", project_id=project_id, levels=[stories], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    for key in stories:
        sm.create_story(key, epic_key="ENG-1", wave=0, summary=f"{key} work", depends_on=[])


def _dev_writing(contents: dict[str, tuple[str, str]], calls: list[dict] | None = None):
    """A fake dev that writes {story: (path, text)} into its worktree."""
    async def run_dev(**kwargs) -> dict:
        if calls is not None:
            calls.append(kwargs)
        path, text = contents[kwargs["run_id"]]
        with open(os.path.join(kwargs["worktree_path"], path), "w") as handle:
            handle.write(text)
        return {
            "file_changes": [{"path": path, "action": "modify"}], "truncated": False, "messages": [],
            "dev_history": [], "dev_build_runner": {},
        }
    return run_dev


def _show(repo: str, ref: str, path: str) -> str:
    return subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True).stdout


async def _state(story_key: str) -> dict:
    from forge.daemon.main import _story_run_values

    return await _story_run_values(story_key)


async def test_parallel_stories_on_different_files_both_land(git_repo, tmp_path, project_id, monkeypatch):
    """N1 and N2 run in parallel; whichever finishes first lands, the other is
    brought up to date with it, re-verified, and lands too."""
    import forge.daemon.main as daemon
    import forge.orchestration.graph as graph

    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, ["ENG-2", "ENG-3"])
    monkeypatch.setattr(graph, "run_dev", _dev_writing({"ENG-2": ("a.txt", "a\n"), "ENG-3": ("b.txt", "b\n")}))
    gate_runs: list[str] = []

    async def counting_gate(**kwargs):
        gate_runs.append(kwargs["run_id"])
        return {"passed": True, "issues": [], "required_fixes": []}

    monkeypatch.setattr(daemon, "run_deterministic_gate", counting_gate)

    await poll_once(sm, wait=True)  # both run to completion
    assert {sm.get_story(k)["status"] for k in ("ENG-2", "ENG-3")} == {"ready_to_merge"}

    await poll_once(sm, wait=True)  # the merge queue lands both
    assert {sm.get_story(k)["status"] for k in ("ENG-2", "ENG-3")} == {"merged"}
    epic_branch = integration_branch_name("ENG-1")
    assert _show(git_repo, epic_branch, "a.txt") == "a\n"
    assert _show(git_repo, epic_branch, "b.txt") == "b\n"
    # Only the second needed re-verifying: the first saw nothing new on the epic.
    assert gate_runs == ["ENG-3"]


async def test_a_conflict_sends_the_story_back_to_dev_and_it_lands_once_resolved(git_repo, tmp_path, project_id, monkeypatch):
    import forge.orchestration.graph as graph

    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, ["ENG-2", "ENG-3"])
    calls: list[dict] = []
    monkeypatch.setattr(graph, "run_dev", _dev_writing(
        {"ENG-2": ("README.md", "hello from ENG-2\n"), "ENG-3": ("README.md", "hello from ENG-3\n")}, calls,
    ))

    await poll_once(sm, wait=True)
    await poll_once(sm, wait=True)  # ENG-2 lands; ENG-3 conflicts with it

    assert sm.get_story("ENG-2")["status"] == "merged"
    story = sm.get_story("ENG-3")
    assert story["status"] == "dispatched" and story["merge_attempts"] == 1
    state = await _state("ENG-3")
    assert "MERGE CONFLICT WITH THE EPIC BRANCH" in state["gate_feedback"]
    assert "ENG-2 (ENG-2 work)" in state["gate_feedback"]
    assert state["integration_paths"] == ["README.md"]
    with open(os.path.join(story["worktree_path"], "README.md")) as handle:
        assert "<<<<<<<" in handle.read()  # left for dev to resolve
    epic_branch = integration_branch_name("ENG-1")
    assert _show(git_repo, epic_branch, "README.md") == "hello from ENG-2\n"  # nothing unverified landed

    # Dev resolves it (keeping both sides) and the story lands.
    monkeypatch.setattr(graph, "run_dev", _dev_writing(
        {"ENG-3": ("README.md", "hello from ENG-2\nhello from ENG-3\n")}, calls,
    ))
    await poll_once(sm, wait=True)  # resumed at dev with the conflict brief -> ready_to_merge
    assert calls[-1]["integration_paths"] == ["README.md"]
    assert "MERGE CONFLICT" in calls[-1]["gate_feedback"]
    assert sm.get_story("ENG-3")["status"] == "ready_to_merge"

    await poll_once(sm, wait=True)
    assert sm.get_story("ENG-3")["status"] == "merged"
    assert _show(git_repo, epic_branch, "README.md") == "hello from ENG-2\nhello from ENG-3\n"


async def test_the_combined_code_failing_its_checks_sends_the_story_back(git_repo, tmp_path, project_id, monkeypatch):
    import forge.daemon.main as daemon
    import forge.orchestration.graph as graph

    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, ["ENG-2", "ENG-3"])
    monkeypatch.setattr(graph, "run_dev", _dev_writing({"ENG-2": ("a.txt", "a\n"), "ENG-3": ("b.txt", "b\n")}))

    async def failing_gate(**kwargs):
        return {"passed": False, "issues": [{"reason": "java_maven: analyze failed", "details": "b.txt:1: boom"}],
                "required_fixes": ["java_maven: analyze failed"]}

    monkeypatch.setattr(daemon, "run_deterministic_gate", failing_gate)

    await poll_once(sm, wait=True)
    await poll_once(sm, wait=True)

    assert sm.get_story("ENG-2")["status"] == "merged"
    assert sm.get_story("ENG-3")["status"] == "dispatched"
    state = await _state("ENG-3")
    assert state["gate_feedback"].startswith("THE COMBINED CODE FAILS ITS CHECKS")
    assert "java_maven: analyze failed" in state["gate_feedback"]
    assert state["integration_paths"] == ["a.txt"]  # what the epic branch changed meanwhile
    assert _show(git_repo, integration_branch_name("ENG-1"), "b.txt") == ""  # not landed


async def test_a_story_that_cannot_land_fails_with_a_clean_worktree_after_the_attempt_budget(
    git_repo, tmp_path, project_id, monkeypatch
):
    import forge.orchestration.graph as graph

    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, ["ENG-2", "ENG-3"])
    monkeypatch.setattr(graph, "run_dev", _dev_writing(
        {"ENG-2": ("README.md", "from ENG-2\n"), "ENG-3": ("README.md", "from ENG-3\n")},
    ))
    await poll_once(sm, wait=True)
    await poll_once(sm, wait=True)  # ENG-3 conflicts
    for _ in range(_MAX_MERGE_ATTEMPTS):
        sm.increment_story_merge_attempts("ENG-3")  # as if every earlier send-back had failed too
    # A dev that never resolves anything: the markers stay, the merge is retried and conflicts again.
    sm.set_story_status("ENG-3", "ready_to_merge")

    await poll_once(sm, wait=True)

    story = sm.get_story("ENG-3")
    assert story["status"] == "failed" and story["failure_stage"] == "merge"
    merging = subprocess.run(
        ["git", "-C", story["worktree_path"], "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True,
    )
    assert merging.returncode != 0  # the half-done merge was aborted for a clean retry

    sm.retry_story("ENG-3")
    assert sm.get_story("ENG-3")["status"] == "ready_to_merge"
    assert sm.get_story("ENG-3")["merge_attempts"] == 0
