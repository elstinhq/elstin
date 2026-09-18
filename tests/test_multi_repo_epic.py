"""One epic whose stories write to different repos.

This is the case a project with its UI and API in separate repos actually
has: a single epic carrying an API story and a UI story. Pinning the
writable repo to the epic could not express it, so the writable root
belongs to the story, with the epic's remaining the default. Reads still
span the project (tests/test_workspace.py covers that half).

Real git throughout — two `git_repo`-style checkouts and genuine worktree
creation — because the whole point is where the branch and worktree land.
Only the LLM calls are faked, via the `fake_agents` fixture.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.config.store import set_config
from forge.daemon.main import poll_once
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _run_git(cmd: list[str], cwd: str) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(tmp_path, name: str) -> str:
    """A repo with an `origin`, matching conftest's git_repo fixture —
    worktree creation assumes `origin/<default>` exists."""
    bare = tmp_path / f"{name}.git"
    _run_git(["git", "init", "--bare", "-b", "main", str(bare)], str(tmp_path))
    repo = tmp_path / name
    _run_git(["git", "clone", str(bare), str(repo)], str(tmp_path))
    _run_git(["git", "config", "user.email", "test@example.com"], str(repo))
    _run_git(["git", "config", "user.name", "Test"], str(repo))
    (repo / "README.md").write_text(f"{name}\n")
    _run_git(["git", "add", "README.md"], str(repo))
    _run_git(["git", "commit", "-m", "initial"], str(repo))
    _run_git(["git", "push", "origin", "main"], str(repo))
    return str(repo)


@pytest.fixture()
def split_project(tmp_path, project_id):
    """One project, two repos, one epic with a UI story and an API story.
    The epic defaults to the UI repo; the API story overrides."""
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("openai_api_key", "sk-test")

    ui = _make_repo(tmp_path, "web-ui")
    api = _make_repo(tmp_path, "api")

    sm = StateManager()
    ui_repo_id = sm.add_project_repo(project_id, ui, label="web-ui")
    api_repo_id = sm.add_project_repo(project_id, api, label="api")

    sm.create_epic("ENG-1", project_id=project_id, manual_gate=False, levels=[["ENG-2", "ENG-3"]])
    sm.set_epic_project_repo("ENG-1", ui_repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Show the cart total")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Expose the cart total endpoint")
    sm.set_story_project_repo("ENG-3", api_repo_id)

    return {"sm": sm, "ui": ui, "api": api, "ui_repo_id": ui_repo_id, "api_repo_id": api_repo_id}


# --- resolution --------------------------------------------------------


async def test_a_story_inherits_the_epics_repo_unless_it_overrides(split_project):
    sm = split_project["sm"]

    inherited = sm.get_story("ENG-2")
    overridden = sm.get_story("ENG-3")

    assert inherited["project_repo_id"] is None  # no override of its own
    assert inherited["repo"] == split_project["ui"]
    assert overridden["project_repo_id"] == split_project["api_repo_id"]
    assert overridden["repo"] == split_project["api"]


async def test_clearing_the_override_returns_the_story_to_the_epics_repo(split_project):
    sm = split_project["sm"]

    sm.set_story_project_repo("ENG-3", None)

    assert sm.get_story("ENG-3")["repo"] == split_project["ui"]


# --- dispatch ----------------------------------------------------------


async def test_each_story_gets_its_worktree_in_its_own_repo(split_project):
    """The heart of it: one epic, one poll, two worktrees in two different
    repos. Before the writable root moved to the story, both landed in the
    epic's default repo and the API story wrote UI code."""
    sm = split_project["sm"]

    await poll_once(sm)

    ui_worktree = sm.get_story("ENG-2")["worktree_path"]
    api_worktree = sm.get_story("ENG-3")["worktree_path"]

    assert ui_worktree and api_worktree
    # A worktree's git dir points back at the repo that owns it, which is
    # the claim that matters — not merely that the paths differ.
    assert _owning_repo(ui_worktree) == os.path.realpath(split_project["ui"])
    assert _owning_repo(api_worktree) == os.path.realpath(split_project["api"])


def _owning_repo(worktree_path: str) -> str:
    """The checkout a linked worktree belongs to."""
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=worktree_path, capture_output=True, text=True, check=True,
    )
    return os.path.realpath(os.path.dirname(result.stdout.strip()))


async def test_each_story_reads_the_other_repo_and_writes_only_its_own(split_project, monkeypatch):
    """The two halves together: the API story's agents get the UI repo as a
    read-only reference, and vice versa."""
    import forge.orchestration.graph as graph_module
    from tests.conftest import fake_run_planner

    seen: dict[str, dict] = {}

    async def capturing_planner(**kwargs):
        seen[kwargs["project_path"]] = kwargs.get("reference_roots") or {}
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph_module, "run_planner", capturing_planner)

    await poll_once(split_project["sm"])

    ui, api = split_project["ui"], split_project["api"]
    # Each story's writable repo is its project_path, and the OTHER repo is
    # what it can read. Neither lists itself as a reference.
    assert seen[ui] == {"api": api}
    assert seen[api] == {"web-ui": ui}


async def test_the_branch_and_commit_land_in_the_storys_own_repo(split_project, monkeypatch):
    """A story that writes a file must commit it in its own repo, or the
    push and PR would target the wrong one."""
    import forge.orchestration.graph as graph_module

    async def dev_writes_a_file(**kwargs) -> dict:
        with open(os.path.join(kwargs["worktree_path"], "endpoint.py"), "w") as handle:
            handle.write("total = 0\n")
        return {
            "file_changes": [{"path": "endpoint.py", "action": "create"}],
            "truncated": False, "messages": [], "dev_history": [],
            "dev_progress": [], "dev_build_runner": {},
        }

    monkeypatch.setattr(graph_module, "run_dev", dev_writes_a_file)
    sm = split_project["sm"]

    await poll_once(sm)
    sm.set_story_gate_approved("ENG-3")
    await poll_once(sm)

    story = sm.get_story("ENG-3")
    assert story["status"] == "awaiting_pr_open"

    # The commit exists on the API repo's branch, and the UI repo has no
    # such branch at all.
    log = subprocess.run(
        ["git", "-C", story["worktree_path"], "log", "--oneline", "main..HEAD"],
        capture_output=True, text=True, check=True,
    )
    assert "ENG-3" in log.stdout
    branches = subprocess.run(
        ["git", "-C", split_project["ui"], "branch", "--list", story["branch"]],
        capture_output=True, text=True, check=True,
    )
    assert branches.stdout.strip() == ""


# --- the override API --------------------------------------------------


async def test_repo_override_route_rejects_a_repo_from_another_project(split_project):
    from fastapi.testclient import TestClient

    from forge.app import create_app

    other_project = split_project["sm"].create_project("other")
    foreign_repo_id = split_project["sm"].add_project_repo(other_project, "/srv/elsewhere")

    with TestClient(create_app()) as client:
        response = client.put("/api/stories/ENG-2/repo", json={"project_repo_id": foreign_repo_id})

    assert response.status_code == 400


async def test_repo_override_route_refuses_a_story_already_in_flight(split_project):
    """The worktree, branch and any PR already live in the old repo, so
    moving the target underneath them would orphan all three."""
    from fastapi.testclient import TestClient

    from forge.app import create_app

    split_project["sm"].set_story_status("ENG-2", "dispatched")

    with TestClient(create_app()) as client:
        response = client.put(
            "/api/stories/ENG-2/repo", json={"project_repo_id": split_project["api_repo_id"]}
        )

    assert response.status_code == 409
