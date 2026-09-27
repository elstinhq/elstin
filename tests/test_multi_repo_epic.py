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

from forge.config.store import set_config, set_project_config
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
    # The dev loop snapshots each file's pre-edit state under data_dir
    # before touching it; the default is an absolute path no dev machine
    # has, so an edit would fail for a reason unrelated to multi-repo.
    set_config("data_dir", str(tmp_path / "data"))
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

    await poll_once(sm, wait=True)

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


async def test_every_story_gets_the_whole_project_to_read(split_project, monkeypatch):
    """No repo is bound to a story: the planner sees the whole project and
    works out from the task which repos the change touches. Only the
    default — the repo an unprefixed path means — differs per story.

    It reads its own repo at its worktree, and every other repo at the epic
    branch's worktree there — never a clone's own checkout, which is frozen
    at clone time and knows nothing of the epic branch."""
    import forge.orchestration.graph as graph_module
    from forge.orchestration.integration import integration_branch_name
    from tests.conftest import fake_run_planner

    seen: dict[str, dict] = {}

    async def capturing_planner(**kwargs):
        seen[kwargs["default_label"]] = {"project_path": kwargs["project_path"], "repo_roots": kwargs.get("repo_roots") or {}}
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph_module, "run_planner", capturing_planner)
    sm = split_project["sm"]

    await poll_once(sm, wait=True)

    ui, api = split_project["ui"], split_project["api"]
    ui_story, api_story = sm.get_story("ENG-2")["worktree_path"], sm.get_story("ENG-3")["worktree_path"]
    assert set(seen) == {"web-ui", "api"}
    assert set(seen["web-ui"]["repo_roots"]) == set(seen["api"]["repo_roots"]) == {"web-ui", "api"}
    assert seen["web-ui"]["project_path"] == seen["web-ui"]["repo_roots"]["web-ui"] == ui_story
    assert seen["api"]["project_path"] == seen["api"]["repo_roots"]["api"] == api_story
    epic_branch = integration_branch_name("ENG-1")
    for label, other, repo in (("web-ui", "api", api), ("api", "web-ui", ui)):
        root = seen[label]["repo_roots"][other]
        assert root not in (ui, api)
        assert _owning_repo(root) == os.path.realpath(repo)
        head = subprocess.run(["git", "branch", "--show-current"], cwd=root, capture_output=True, text=True, check=True)
        assert head.stdout.strip() == epic_branch


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

    await poll_once(sm, wait=True)
    sm.set_story_gate_approved("ENG-3")
    await poll_once(sm, wait=True)

    story = sm.get_story("ENG-3")
    assert story["status"] == "ready_to_merge"

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


async def test_repo_override_route_rejects_a_repo_from_another_project(split_project, project_id):
    from fastapi.testclient import TestClient

    from forge.app import create_app

    # The app's lifespan starts the daemon poll loop, which would dispatch the
    # pending story mid-request — and a dispatched story is (correctly) refused
    # with a 409 before the repo is even looked at. Keep the poller out of it.
    set_project_config(project_id, "auto_run_enabled", "false")
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


# --- one story, several repos ------------------------------------------


async def test_a_single_story_writes_every_repo_its_plan_names(split_project, monkeypatch):
    """The model the user asked for: nothing binds a repo to the story.
    The plan names a UI file and an API file, so dev gets a worktree in
    both, writes both, and the story ends with a branch and a commit in
    each.

    The API worktree dev creates branches from the epic branch in the API
    repo, not from its main: it has what another story already landed
    there."""
    import forge.orchestration.graph as graph_module
    from forge.orchestration.integration import integration_branch_name

    api_repo = split_project["api"]
    epic_branch = integration_branch_name("ENG-1")
    _run_git(["git", "checkout", "-q", "-b", epic_branch, "main"], api_repo)
    with open(os.path.join(api_repo, "landed.py"), "w") as handle:
        handle.write("landed = True\n")
    _run_git(["git", "add", "landed.py"], api_repo)
    _run_git(["git", "commit", "-q", "-m", "ENG-9 landed on the epic branch"], api_repo)
    _run_git(["git", "checkout", "-q", "main"], api_repo)

    plan = {
        "feature_summary": "Show the cart total",
        "files_to_create": [],
        "files_to_modify": [{"path": "README.md"}, {"path": "@api/README.md"}],
    }

    async def planner_spanning_both(**kwargs):
        return {
            "questions": [],
            "context_bundle": {},
            "plan": plan,
            "acceptance_criteria": [],
            "messages": [],
        }

    async def dev_writes_both(**kwargs):
        # The real dev loop is what creates the extra worktree, so drive
        # the real one rather than faking its result.
        from forge.agents.roles.dev_loop import run_dev

        return await run_dev(**kwargs)

    written: list[str] = []

    class _FakeProvider:
        name = "openai"

        def __init__(self):
            from forge.llm.base import Capabilities

            self.capabilities = Capabilities()
            self._turns = [
                ("apply_edits_tool", {"edits": [
                    {"path": "README.md", "target_content": "web-ui", "replacement_content": "web-ui + total"},
                    {"path": "@api/README.md", "target_content": "api", "replacement_content": "api + total"},
                ]}),
                None,
            ]

        async def complete(self, request):
            from forge.llm.types import LlmResponse, TextBlock, ToolCallBlock, Usage

            step = self._turns.pop(0)
            if step is None:
                return LlmResponse(content=[TextBlock(text="done")], stop_reason="end_turn", usage=Usage(), model="m")
            name, args = step
            written.append(name)
            return LlmResponse(
                content=[ToolCallBlock(id="c1", name=name, arguments=args)],
                stop_reason="tool_calls", usage=Usage(), model="m",
            )

    import forge.agents.roles.dev_loop as dev_loop_module

    monkeypatch.setattr(graph_module, "run_planner", planner_spanning_both)
    monkeypatch.setattr(graph_module, "run_dev", dev_writes_both)
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda: _FakeProvider())

    sm = split_project["sm"]
    await poll_once(sm, wait=True)
    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm, wait=True)

    worktrees = {e["repo_label"]: e for e in sm.list_story_worktrees("ENG-2")}

    # A worktree, and a branch, in BOTH repos — from the plan alone.
    assert set(worktrees) == {"web-ui", "api"}
    assert worktrees["web-ui"]["repo_path"] == split_project["ui"]
    assert worktrees["api"]["repo_path"] == split_project["api"]

    # And the edit landed in each repo's own worktree.
    assert "total" in open(os.path.join(worktrees["web-ui"]["worktree_path"], "README.md")).read()
    assert "total" in open(os.path.join(worktrees["api"]["worktree_path"], "README.md")).read()
    assert os.path.exists(os.path.join(worktrees["api"]["worktree_path"], "landed.py"))

    # Each repo carries its own commit, because git has no cross-repo one.
    for entry in worktrees.values():
        log = subprocess.run(
            ["git", "-C", entry["worktree_path"], "log", "--oneline", "main..HEAD"],
            capture_output=True, text=True, check=True,
        )
        assert "ENG-2" in log.stdout, entry["repo_label"]
