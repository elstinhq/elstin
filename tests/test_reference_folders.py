"""Reference folders and read-only repos: a workspace folder like ~/dev/mono
read for context (its CLAUDE.md, docs) and a shared contract repo read but
never changed. forge/tools/reference_folder.py (the copy runs read),
forge/api/project_routes.py (attaching, marking read-only), and a run through
the daemon (what the planner is given, where branches are made)."""
from __future__ import annotations

import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from forge.tools import reference_folder
from forge.tools.reference_folder import plan_reference, sync_reference_copy


def _git_repo(path) -> str:
    path.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README.md").write_text(f"{path.name}\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return str(path.resolve())


def _workspace(tmp_path):
    """Like ~/dev/mono."""
    root = tmp_path / "mono"
    _git_repo(root / "mono-apiservice")
    _git_repo(root / "API-Schemas")
    (root / "CLAUDE.md").write_text("API-Schemas is off limits.\n")
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "kb.md").write_text("how item search works\n")
    (root / "docs" / "plans" / "search.md").write_text("plan\n")
    (root / "db").mkdir()
    (root / "db" / "prod.gz").write_bytes(b"\x1f\x8b" + b"x" * 100)
    (root / "Quarterly report.csv").write_text("a,b\n1,2\n")
    (root / "helper.sh").write_text("#!/bin/sh\n")
    (root / "big.log").write_text("x" * (reference_folder.MAX_FILE_BYTES + 1))
    (root / "blob.dat").write_bytes(b"\0\1\2")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "index.js").write_text("x\n")
    return root


def _files(root: str) -> list[str]:
    return sorted(
        os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs if not f.startswith(".")
    )


# --- what is read ------------------------------------------------------------


def test_the_copy_holds_the_context_and_leaves_out_repos_and_data(tmp_path):
    root = _workspace(tmp_path)

    plan = plan_reference(str(root))
    copy = sync_reference_copy(str(root), str(tmp_path / "copy"))

    assert _files(copy) == ["CLAUDE.md", "docs/kb.md", "docs/plans/search.md", "helper.sh"]
    assert sorted(plan.repos) == ["API-Schemas", "mono-apiservice"]
    summary = plan.summary()
    assert summary["readable"] == ["CLAUDE.md", "docs/", "helper.sh"]
    assert {s["name"]: s["reason"] for s in summary["skipped"]} == {
        "big.log": "over 1 MB", "blob.dat": "binary file", "db/": "data file", "Quarterly report.csv": "data file",
    }


def test_the_copy_is_reused_while_fresh_and_rebuilt_after(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    dest = str(tmp_path / "copy")
    sync_reference_copy(str(root), dest)

    (root / "docs" / "new.md").write_text("added\n")
    sync_reference_copy(str(root), dest)
    assert "docs/new.md" not in _files(dest)  # fresh: reused

    monkeypatch.setattr(reference_folder, "FRESH_SECONDS", 0)
    time.sleep(0.01)
    sync_reference_copy(str(root), dest)
    assert "docs/new.md" in _files(dest)

    # A folder that has gone leaves the last copy rather than failing a run.
    import shutil

    shutil.rmtree(root)
    assert "CLAUDE.md" in _files(sync_reference_copy(str(root), dest))


# --- attaching and marking over the API ----------------------------------------

pytestmark_db = pytest.mark.usefixtures("forge_db")


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


@pytestmark_db
def test_attach_a_reference_folder_and_a_read_only_repo(tmp_path):
    from forge.state.manager import StateManager

    root = _workspace(tmp_path)
    sm = StateManager()
    project_id = sm.create_project("MONO")

    with _client() as client:
        scan = client.post("/api/projects/scan-folder", json={"path": str(root)}).json()
        assert scan["reference"]["readable"] == ["CLAUDE.md", "docs/", "helper.sh"]

        api = client.post(f"/api/projects/{project_id}/repos", json={"path": str(root / "mono-apiservice")}).json()
        contracts = client.post(
            f"/api/projects/{project_id}/repos", json={"path": str(root / "API-Schemas"), "read_only": True},
        ).json()
        reference = client.post(f"/api/projects/{project_id}/repos", json={"path": str(root), "kind": "reference"}).json()
        a_repo_as_reference = client.post(
            f"/api/projects/{project_id}/repos", json={"path": str(root / "mono-apiservice"), "kind": "reference"},
        )
        generate = client.post(f"/api/projects/{project_id}/repos/{reference['id']}/guide/generate")
        make_reference_writable = client.put(f"/api/projects/{project_id}/repos/{reference['id']}", json={"read_only": False})

    assert (api["kind"], api["read_only"]) == ("repo", False)
    assert (contracts["kind"], contracts["read_only"]) == ("repo", True)
    assert (reference["kind"], reference["read_only"], reference["label"]) == ("reference", True, "mono")
    assert a_repo_as_reference.status_code == 400 and "mark it read-only instead" in a_repo_as_reference.json()["detail"]
    assert generate.status_code == 400
    assert make_reference_writable.status_code == 400
    # An epic defaults to the writable repo, never to the others.
    assert sm.writable_project_repo_id(project_id) == api["id"]


@pytestmark_db
def test_marking_a_repo_read_only_moves_epics_off_it_and_waits_for_unfinished_work(tmp_path):
    from forge.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("MONO")
    first = sm.add_project_repo(project_id, _git_repo(tmp_path / "a"), label="a")
    second = sm.add_project_repo(project_id, _git_repo(tmp_path / "b"), label="b")
    sm.create_epic("ENG-1", project_id=project_id)
    sm.set_epic_project_repo("ENG-1", first)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.record_story_worktree("ENG-2", repo_label="a", repo_path=str((tmp_path / "a").resolve()), worktree_path=str(tmp_path / "wt"), branch="x", is_default=True)
    sm.set_story_status("ENG-2", "dispatched")

    with _client() as client:
        refused = client.put(f"/api/projects/{project_id}/repos/{first}", json={"read_only": True})
        sm.set_story_status("ENG-2", "merged")
        marked = client.put(f"/api/projects/{project_id}/repos/{first}", json={"read_only": True})
        back = client.put(f"/api/projects/{project_id}/repos/{first}", json={"read_only": False})

    assert refused.status_code == 409 and "ENG-2" in refused.json()["detail"]
    assert marked.status_code == 200 and marked.json()["epics_moved"] == ["ENG-1"] and marked.json()["moved_to"] == second
    assert sm.get_epic("ENG-1")["project_repo_id"] == second
    assert back.json()["read_only"] is False


@pytestmark_db
def test_create_a_project_with_repos_a_read_only_one_and_the_reference_folder(tmp_path):
    root = _workspace(tmp_path)
    api, contracts = str((root / "mono-apiservice").resolve()), str((root / "API-Schemas").resolve())

    with _client() as client:
        project = client.post("/api/projects", json={
            "name": "MONO", "repo_paths": [api, contracts], "read_only_paths": [contracts], "reference_path": str(root),
        }).json()
        repos = client.get(f"/api/projects/{project['id']}/repos").json()

    assert [(r["label"], r["kind"], r["read_only"]) for r in repos] == [
        ("mono-apiservice", "repo", False), ("API-Schemas", "repo", True), ("mono", "reference", True),
    ]


# --- a run -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("forge_db", "fake_agents")
async def test_a_run_reads_the_reference_folder_and_never_branches_read_only_repos(tmp_path, monkeypatch):
    """The planner gets the workspace's CLAUDE.md first in its guide, the
    reference copy and the read-only repo as readable roots, and is told
    they are read-only; the epic branch is made only where a story can write."""
    import forge.daemon.main as daemon
    import forge.orchestration.graph as graph
    from forge.config.store import set_config
    from forge.orchestration.integration import integration_branch_name
    from forge.state.manager import StateManager
    from tests.conftest import fake_run_planner

    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "false")
    root = _workspace(tmp_path)
    # The service repo needs an origin like any real one (worktree.py assumes it).
    api = str((root / "mono-apiservice").resolve())
    (root / "mono-apiservice" / "AGENTS.md").write_text("Run mvn verify.\n")
    subprocess.run(["git", "add", "."], cwd=api, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "guide"], cwd=api, check=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "api.git")], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(tmp_path / "api.git")], cwd=api, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=api, check=True)
    contracts = str((root / "API-Schemas").resolve())

    sm = StateManager()
    project_id = sm.create_project("MONO")
    api_id = sm.add_project_repo(project_id, api, label="mono-apiservice")
    sm.add_project_repo(project_id, contracts, label="API-Schemas", read_only=True)
    sm.add_project_repo(project_id, str(root.resolve()), label="mono", kind="reference")
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", api_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="item search", depends_on=[])
    seen: dict = {}

    async def planner(**kwargs):
        seen.update(kwargs)
        return await fake_run_planner(**kwargs)

    monkeypatch.setattr(graph, "run_planner", planner)

    await daemon.poll_once(sm, wait=True)

    assert sorted(seen["read_only_labels"]) == ["api-schemas", "mono"]
    roots = seen["repo_roots"]
    assert roots["api-schemas"] == contracts
    assert _files(roots["mono"]) == ["CLAUDE.md", "docs/kb.md", "docs/plans/search.md", "helper.sh"]
    guide = seen["project_guide"]["content"]
    assert guide.startswith("### mono (CLAUDE.md)\n\nAPI-Schemas is off limits.")
    assert "### mono-apiservice (AGENTS.md)" in guide  # the repo's own, after the workspace's
    epic_branch = integration_branch_name("ENG-1")
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", epic_branch], cwd=api).returncode == 0
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", epic_branch], cwd=contracts).returncode != 0
    # The workspace folder itself was only read.
    assert sorted(os.listdir(root)) == sorted([
        ".claude", "API-Schemas", "CLAUDE.md", "Quarterly report.csv", "big.log", "blob.dat", "db",
        "docs", "helper.sh", "mono-apiservice", "node_modules",
    ])


@pytest.mark.asyncio
@pytest.mark.usefixtures("forge_db", "fake_agents")
async def test_changes_a_plan_needs_in_a_read_only_repo_are_kept_for_a_person(git_repo, tmp_path, project_id, monkeypatch):
    """One story: the plan goes ahead without the contract change, which is
    kept on the story — the board's tag, the details — and listed in the
    epic's pull request. The next plan replaces the list."""
    import forge.daemon.main as daemon
    import forge.orchestration.graph as graph
    from forge.config.store import set_config
    from forge.state.manager import StateManager
    from tests.conftest import fake_run_planner

    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "true")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False, summary="Item search")
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="search columns", depends_on=[])
    needed = [{"path": "@contracts/items.yaml", "purpose": "add the columns to the contract"}]
    plans = [needed, []]

    async def planner(**kwargs):
        result = await fake_run_planner(**kwargs)
        result["plan"]["read_only_changes"] = plans.pop(0)
        return result

    monkeypatch.setattr(graph, "run_planner", planner)

    await daemon.poll_once(sm, wait=True)

    story = sm.get_story("ENG-2")
    assert story["status"] == "awaiting_plan_approval" and story["read_only_changes"] == needed
    body = daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))
    assert "## Changes left to a person" in body
    assert "- `@contracts/items.yaml` (ENG-2): add the columns to the contract" in body

    assert sm.send_back_to_planning("ENG-2")  # as a changed ticket or newly merged work does
    await daemon.poll_once(sm, wait=True)

    assert sm.get_story("ENG-2")["read_only_changes"] == []
    assert "left to a person" not in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))
