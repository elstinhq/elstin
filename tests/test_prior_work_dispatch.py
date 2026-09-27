"""Work that already exists for a ticket decides where forge's branches start
(forge/daemon/prior_work_lookup.py, forge/daemon/main.py's first dispatch).
Real Postgres and git — branches pushed the way a teammate would push them;
the agents are fakes."""
from __future__ import annotations

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import forge.daemon.main as daemon
from forge.config.store import set_config, set_project_config
from forge.daemon.prior_work_lookup import look_up_prior_work
from forge.orchestration.integration import integration_branch_name
from forge.orchestration.reconciler import Blocked
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=repo).returncode == 0


def _push_branch(repo: str, branch: str, files: dict[str, str]) -> str:
    """A teammate's branch: committed, pushed, and — as in forge's own clone —
    present only as `origin/<branch>`."""
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


def _setup(tmp_path, project_id, repo, deps: dict[str, list[str]]) -> StateManager:
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "false")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[list(deps)], manual_gate=False, summary="Search")
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, repo))
    for key, depends_on in deps.items():
        sm.create_story(key, epic_key="ENG-1", wave=0, summary=f"{key} work", depends_on=depends_on)
    return sm


def _capture_planner(monkeypatch) -> dict[str, dict]:
    import forge.orchestration.graph as graph
    from tests.conftest import fake_run_planner

    seen: dict[str, dict] = {}

    async def planner(**kwargs):
        seen[kwargs["run_id"]] = kwargs
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph, "run_planner", planner)
    return seen


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


async def test_a_story_continues_from_the_branch_its_ticket_already_has(git_repo, tmp_path, project_id, monkeypatch):
    tip = _push_branch(git_repo, "feature/ENG-2-search-api", {"search.py": "def search(): ...\n"})
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": [], "ENG-3": []})
    seen = _capture_planner(monkeypatch)

    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert story["prior_work"]["repos"]["widgets"]["verdict"] == "continue"
    worktree = story["worktree_path"]
    assert os.path.exists(os.path.join(worktree, "search.py"))
    assert story["branch"].startswith("feat/ENG-2-")  # forge's own branch...
    assert _is_ancestor(worktree, tip, "HEAD")  # ...starting from the teammate's work
    # The planner read that worktree and was told what it already holds.
    assert seen["ENG-2"]["project_path"] == worktree
    [candidate] = seen["ENG-2"]["prior_work"]["candidates"]
    assert candidate["branch"] == "feature/ENG-2-search-api" and candidate["selected"] is True
    # The other story found nothing and did not get it.
    other = sm.get_story("ENG-3")
    assert other["prior_work"]["repos"] == {} and not (seen["ENG-3"]["prior_work"] or {}).get("found")
    assert not os.path.exists(os.path.join(other["worktree_path"], "search.py"))
    # The teammate's branch was only read: never moved, never checked out locally.
    assert _git(git_repo, "rev-parse", "origin/feature/ENG-2-search-api") == tip
    assert _git(git_repo, "branch", "--list", "feature/ENG-2-search-api") == ""


async def test_continued_work_lands_even_when_the_story_needs_no_new_change(git_repo, tmp_path, project_id, monkeypatch):
    """Dev finds the ticket already done by the branch it continued: nothing
    new to write, but the branch's commits still have to reach the epic."""
    import forge.orchestration.graph as graph

    _push_branch(git_repo, "feature/ENG-2-search-api", {"search.py": "def search(): ...\n"})
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": []})

    async def dev(**kwargs):
        return {"file_changes": [], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {},
                "declared_unchanged": {"search.py": "the continued branch already implements the search endpoint"}}

    monkeypatch.setattr(graph, "run_dev", dev)

    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["status"] == "ready_to_merge"  # not "done": its commits have not landed

    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["status"] == "merged"
    epic_branch = integration_branch_name("ENG-1")
    assert _git(git_repo, "show", f"{epic_branch}:search.py") == "def search(): ..."
    assert "continues `feature/ENG-2-search-api` (widgets)" in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))


async def test_work_already_in_main_is_not_continued_but_the_planner_is_told(git_repo, tmp_path, project_id, monkeypatch):
    tip = _push_branch(git_repo, "feature/ENG-2-done", {"done.py": "ok = True\n"})
    with open(os.path.join(git_repo, "done.py"), "w") as handle:  # squash-merged
        handle.write("ok = True\n")
    _git(git_repo, "add", "done.py")
    _git(git_repo, "commit", "-q", "-m", "Finish ENG-2 (#3)")
    _git(git_repo, "push", "-q", "origin", "main")
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": []})
    seen = _capture_planner(monkeypatch)

    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert story["prior_work"]["repos"]["widgets"]["verdict"] == "in_default"
    assert not _is_ancestor(story["worktree_path"], tip, "HEAD")  # started from the epic branch as usual
    prior = seen["ENG-2"]["prior_work"]
    assert prior["found"] is True
    assert [(c["branch"], c["selected"], c["status"]) for c in prior["candidates"]] == [("feature/ENG-2-done", False, "already in main")]
    assert [c.split(" ", 1)[1] for c in prior["merged_commits"]["widgets (default repo)"]] == ["Finish ENG-2 (#3)"]


async def test_diverged_branches_hold_only_that_story_until_someone_picks(git_repo, tmp_path, project_id):
    _push_branch(git_repo, "feature/ENG-2-api", {"a.txt": "a\n"})
    _push_branch(git_repo, "ENG-2-spike", {"b.txt": "b\n"})
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": [], "ENG-3": [], "ENG-4": ["ENG-2"]})

    await daemon.poll_once(sm, wait=True)

    held = sm.get_story("ENG-2")
    assert held["status"] == "pending" and held["branch"] is None
    assert sm.get_story("ENG-3")["status"] == "ready_to_merge"  # independent work carried on
    assert sm.get_story("ENG-4")["status"] == "pending"  # waits on ENG-2, as for any dependency
    epic = sm.get_epic("ENG-1")
    assert daemon.check_story_action(sm, epic, "ENG-2") == Blocked("ENG-2", "choose_prior_branch")

    client = _client()
    assert client.post("/api/stories/ENG-2/prior-work", json={"repo_label": "widgets", "branch": "nope"}).status_code == 422
    assert client.post("/api/stories/ENG-2/prior-work", json={"repo_label": "other", "branch": None}).status_code == 404
    picked = client.post("/api/stories/ENG-2/prior-work", json={"repo_label": "widgets", "branch": "ENG-2-spike"})
    assert picked.status_code == 200 and picked.json()["prior_work"]["picks"] == {"widgets": "ENG-2-spike"}

    await daemon.poll_once(sm, wait=True)

    worktree = sm.get_story("ENG-2")["worktree_path"]
    assert os.path.exists(os.path.join(worktree, "b.txt")) and not os.path.exists(os.path.join(worktree, "a.txt"))
    assert client.post("/api/stories/ENG-2/prior-work", json={"repo_label": "widgets", "branch": None}).status_code == 409


async def test_a_branch_named_after_the_epic_is_where_the_epic_branch_starts(git_repo, tmp_path, project_id):
    tip = _push_branch(git_repo, "feature/ENG-1-shared-models", {"models.py": "class Customer: ...\n"})
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": []})

    await daemon.poll_once(sm, wait=True)

    assert sm.get_epic("ENG-1")["prior_work"]["repos"]["widgets"]["chosen"] == "feature/ENG-1-shared-models"
    assert _is_ancestor(git_repo, tip, integration_branch_name("ENG-1"))
    assert os.path.exists(os.path.join(sm.get_story("ENG-2")["worktree_path"], "models.py"))


async def test_look_again_finds_a_branch_pushed_after_the_epic_was_added(git_repo, tmp_path, project_id):
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": []})
    set_project_config(project_id, "auto_run_enabled", "false")  # looked up on the poll, not started

    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["prior_work"]["repos"] == {}

    _push_branch(git_repo, "ENG-2-late", {"late.txt": "x\n"})
    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["prior_work"]["repos"] == {}  # looked up once, not on every poll

    assert _client().post("/api/epics/ENG-1/prior-work/refresh").status_code == 200
    assert sm.get_story("ENG-2")["prior_work"] == {"stale": True, "picks": {}}
    await daemon.poll_once(sm, wait=True)
    assert sm.get_story("ENG-2")["prior_work"]["repos"]["widgets"]["chosen"] == "ENG-2-late"


async def test_look_again_keeps_a_pick_that_still_applies(git_repo, tmp_path, project_id):
    _push_branch(git_repo, "feature/ENG-2-api", {"a.txt": "a\n"})
    _push_branch(git_repo, "ENG-2-spike", {"b.txt": "b\n"})
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": []})
    set_project_config(project_id, "auto_run_enabled", "false")
    await daemon.poll_once(sm, wait=True)
    client = _client()
    client.post("/api/stories/ENG-2/prior-work", json={"repo_label": "widgets", "branch": "ENG-2-spike"})

    _push_branch(git_repo, "ENG-2-third", {"c.txt": "c\n"})  # a new branch: worth looking again
    client.post("/api/epics/ENG-1/prior-work/refresh")
    await daemon.poll_once(sm, wait=True)

    prior = sm.get_story("ENG-2")["prior_work"]
    assert sorted(c["branch"] for c in prior["repos"]["widgets"]["candidates"]) == ["ENG-2-spike", "ENG-2-third", "feature/ENG-2-api"]
    assert prior["picks"] == {"widgets": "ENG-2-spike"}


async def test_a_story_that_has_started_is_never_looked_up(git_repo, tmp_path, project_id):
    sm = _setup(tmp_path, project_id, git_repo, {"ENG-2": []})
    sm.set_story_worktree("ENG-2", worktree_path=str(tmp_path / "wt"), branch="feat/ENG-2-x")

    look_up_prior_work(sm, sm.get_epic("ENG-1"))

    assert sm.get_story("ENG-2")["prior_work"] is None


async def test_the_epic_branch_is_made_in_every_repo_and_removed_from_every_repo(tmp_path, project_id):
    """A repo no story wrote still gets the epic branch — the planner reads it
    there — so removing the ticket has to clean it up there too."""
    from tests.test_multi_repo_epic import _make_repo

    ui, api = _make_repo(tmp_path, "web-ui"), _make_repo(tmp_path, "api")
    sm = _setup(tmp_path, project_id, ui, {"ENG-2": []})
    sm.add_project_repo(project_id, api, label="api")

    await daemon.poll_once(sm, wait=True)

    epic_branch = integration_branch_name("ENG-1")
    assert _git(api, "branch", "--list", epic_branch)
    from forge.daemon.ticket_removal import remove_epic

    await remove_epic(sm, "ENG-1")
    assert sm.get_epic("ENG-1") is None
    assert _git(ui, "branch", "--list", epic_branch) == "" and _git(api, "branch", "--list", epic_branch) == ""
    assert not any(name.startswith("_integration-") for name in os.listdir(tmp_path / "worktrees"))

