"""Deleting a project (elstin/daemon/project_removal.py, DELETE /api/projects/{id}):
every ticket as Remove ticket does, elstin's own clones, and the project's
rows — but never an attached folder, and never anything on GitHub or JIRA.
Real Postgres and git; the agents are fakes."""
from __future__ import annotations

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

import elstin.daemon.main as daemon
from elstin.config.store import get_project_config, set_config, set_project_config
from elstin.orchestration.integration import integration_branch_name
from elstin.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("elstin_db", "fake_agents"), pytest.mark.asyncio]


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path, bare_name: str, checkout: str) -> str:
    """A repo with an `origin`, checked out at `checkout`."""
    bare = tmp_path / "remotes" / f"{bare_name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(str(tmp_path), "init", "-q", "--bare", "-b", "main", str(bare))
    os.makedirs(os.path.dirname(checkout), exist_ok=True)
    _git(str(tmp_path), "clone", "-q", str(bare), checkout)
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    with open(os.path.join(checkout, "README.md"), "w") as handle:
        handle.write(f"{bare_name}\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-q", "-m", "init")
    _git(checkout, "push", "-q", "origin", "main")
    return checkout


@pytest.fixture()
def project(tmp_path):
    """A project with a folder elstin cloned and a folder that was attached,
    two tickets (one underway in both repos, one not started), a settings
    override and a ticket filter."""
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("cloned_repos_base_dir", str(tmp_path / "clones"))
    set_config("require_plan_approval", "false")
    sm = StateManager()
    pid = sm.create_project("Customer details")
    cloned = _repo(tmp_path, "api", str(tmp_path / "clones" / str(pid) / "api"))
    attached = _repo(tmp_path, "web", str(tmp_path / "my-code" / "web"))
    cloned_id = sm.add_project_repo(pid, cloned, label="api", clone_url="https://github.com/acme/api.git")
    attached_id = sm.add_project_repo(pid, attached, label="web")

    sm.create_epic("ENG-1", project_id=pid, levels=[["ENG-2", "ENG-3"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", cloned_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="API", depends_on=[])
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Tab", depends_on=[])
    sm.set_story_project_repo("ENG-3", attached_id)
    sm.create_epic("ENG-10", project_id=pid, levels=[["ENG-11"]], manual_gate=True)
    sm.set_epic_project_repo("ENG-10", cloned_id)
    sm.create_story("ENG-11", epic_key="ENG-10", wave=0, summary="Later", depends_on=[])

    set_project_config(pid, "require_plan_approval", "false")
    sm.set_project_ticket_filter(pid, enabled=True, jira_project_key="ENG")
    return {"sm": sm, "id": pid, "cloned": cloned, "attached": attached, "tmp": tmp_path}


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


async def test_deleting_a_project_removes_its_tickets_clones_and_rows_but_keeps_an_attached_folder(project):
    sm, pid = project["sm"], project["id"]
    await daemon.poll_once(sm, wait=True)  # ENG-1's stories get worktrees and branches in both repos
    assert sm.get_story("ENG-3")["worktree_path"]
    assert _git(project["attached"], "branch", "--list", integration_branch_name("ENG-1"))

    response = _client().delete(f"/api/projects/{pid}")

    assert response.status_code == 200, response.text
    report = response.json()
    assert sorted(report["removed"]["tickets"]) == ["ENG-1", "ENG-10"]
    assert report["removed"]["clones"] == [project["cloned"]]
    assert report["removed"]["folders"] == 2 and report["removed"]["ticket_filters"] == 1
    assert report["attached_folders_kept"] == [project["attached"]]
    assert report["warnings"] == []

    # Gone from the database...
    assert sm.get_project(pid) is None
    assert sm.list_project_epic_keys(pid) == [] and sm.get_story("ENG-2") is None and sm.get_story("ENG-11") is None
    assert sm.list_project_repos(pid) == []
    assert get_project_config(pid, "require_plan_approval") is None
    assert sm.get_project_ticket_filter(pid) is None
    # ...and from disk: the clone, the project's clone folder, every worktree.
    assert not os.path.exists(project["cloned"])
    assert not os.path.exists(project["tmp"] / "clones" / str(pid))
    assert os.listdir(project["tmp"] / "worktrees") == []
    # The attached folder is someone's checkout: still there, with elstin's
    # branches and worktrees gone from it and its own work untouched.
    attached = project["attached"]
    assert os.path.isfile(os.path.join(attached, "README.md"))
    assert _git(attached, "branch", "--format=%(refname:short)") == "main"
    assert len(_git(attached, "worktree", "list").splitlines()) == 1
    assert _git(attached, "status", "--porcelain") == ""


async def test_a_run_in_progress_refuses_the_deletion_before_anything_is_touched(project):
    sm, pid = project["sm"], project["id"]
    assert daemon.claim_run("ENG-2")
    try:
        response = _client().delete(f"/api/projects/{pid}")
    finally:
        daemon.release_run("ENG-2")

    assert response.status_code == 409 and "ENG-2" in response.json()["detail"]
    assert sm.get_project(pid) is not None
    assert sorted(sm.list_project_epic_keys(pid)) == ["ENG-1", "ENG-10"]
    assert sm.get_project_ticket_filter(pid)["enabled"] is True
    assert sm.get_epic("ENG-1")["manual_gate"] is False
    assert os.path.isdir(project["cloned"])


async def test_an_unknown_project_is_a_404(elstin_db):
    assert _client().delete("/api/projects/9999").status_code == 404


async def test_a_clone_path_outside_the_projects_clone_folder_is_never_deleted(project):
    """A stored path is data, not an instruction: a "cloned" folder that is not
    under cloned_repos_base_dir/<id> is reported, not deleted."""
    sm, pid = project["sm"], project["id"]
    elsewhere = _repo(project["tmp"], "stray", str(project["tmp"] / "elsewhere" / "stray"))
    sm.add_project_repo(pid, elsewhere, label="stray", clone_url="https://github.com/acme/stray.git")

    report = _client().delete(f"/api/projects/{pid}").json()

    assert os.path.isdir(elsewhere)
    assert any("not deleting the clone at" in w and "stray" in w for w in report["warnings"])
    assert sm.get_project(pid) is None
