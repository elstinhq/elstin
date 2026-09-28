"""The epic's pull request — the only thing forge sends to GitHub. Stories
merge into the epic branch locally; once every one has, the daemon pushes
the epic branch and opens one pull request into the default branch
(forge/daemon/main.py's _finish_epic), then follows its CI status and a
human's merge. Real Postgres and git; GitHub is a fake."""
from __future__ import annotations

import os

import pytest

from forge.config.store import set_config, set_project_config
from forge.daemon.main import check_story_action, execute_story_action, poll_once
from forge.orchestration.integration import integration_branch_name
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


class _FakeGitHub:
    configured = True
    created: list[dict] = []
    pr_state = "open"
    ci_status = "pending"

    @staticmethod
    def is_configured():
        return _FakeGitHub.configured

    def create_pull_request(self, owner, repo, *, head, base, title, body):
        _FakeGitHub.created.append({"owner": owner, "repo": repo, "head": head, "base": base, "title": title, "body": body})
        number = len(_FakeGitHub.created)
        return {"number": number, "url": f"https://github.com/{owner}/{repo}/pull/{number}", "state": "open"}

    def get_pull_request(self, owner, repo, number):
        return {"number": number, "url": "", "state": _FakeGitHub.pr_state}

    def get_pr_check_status(self, owner, repo, number):
        return _FakeGitHub.ci_status


@pytest.fixture()
def github(monkeypatch):
    import forge.daemon.main as daemon

    _FakeGitHub.configured = True
    _FakeGitHub.created = []
    _FakeGitHub.pr_state = "open"
    _FakeGitHub.ci_status = "pending"
    pushes: list[str] = []
    monkeypatch.setattr(daemon, "GitHubService", _FakeGitHub)
    monkeypatch.setattr(daemon, "parse_github_remote", lambda path: ("acme", "widgets"))
    monkeypatch.setattr(daemon, "push_branch", lambda path, branch: pushes.append(branch) or (True, None))
    return pushes


def _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2", "ENG-3")) -> StateManager:
    import forge.orchestration.graph as graph

    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "false")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[list(stories)], manual_gate=False,
                   summary="Multi-item pricing", description="Price several item types in one quote.")
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    for key in stories:
        sm.create_story(key, epic_key="ENG-1", wave=0, summary=f"{key} work", depends_on=[])

    async def dev(**kwargs):
        with open(os.path.join(kwargs["worktree_path"], f"{kwargs['run_id']}.txt"), "w") as handle:
            handle.write(kwargs["run_id"])
        return {"file_changes": [{"path": f"{kwargs['run_id']}.txt", "action": "create"}], "truncated": False,
                "messages": [], "dev_history": [], "dev_build_runner": {}}

    monkeypatch.setattr(graph, "run_dev", dev)
    return sm


async def _run_to_merged(sm: StateManager) -> None:
    await poll_once(sm, wait=True)  # run
    await poll_once(sm, wait=True)  # merge queue


async def test_one_pull_request_opens_for_the_epic_once_every_story_merged(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _run_to_merged(sm)
    assert _FakeGitHub.created == []  # nothing is proposed while stories are merging

    await poll_once(sm, wait=True)  # every story merged -> the epic proposes its branch

    assert len(_FakeGitHub.created) == 1
    pr = _FakeGitHub.created[0]
    assert pr["head"] == integration_branch_name("ENG-1") and pr["base"] == "main"
    assert pr["title"] == "ENG-1: Multi-item pricing"
    assert "- **ENG-2** ENG-2 work" in pr["body"] and "- **ENG-3** ENG-3 work" in pr["body"]
    assert github == [integration_branch_name("ENG-1")]  # only the epic branch is ever pushed
    assert sm.get_epic("ENG-1")["phase"] == "pr_open"
    [row] = sm.list_epic_pull_requests("ENG-1")
    assert row["number"] == 1 and row["state"] == "open" and row["error"] is None

    await poll_once(sm, wait=True)  # open: refreshed, never opened twice
    assert len(_FakeGitHub.created) == 1
    assert sm.list_epic_pull_requests("ENG-1")[0]["ci_status"] == "pending"


async def test_the_epic_is_merged_once_a_human_merges_its_pull_request(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _run_to_merged(sm)
    await poll_once(sm, wait=True)

    _FakeGitHub.pr_state = "merged"
    await poll_once(sm, wait=True)

    assert sm.get_epic("ENG-1")["phase"] == "merged"
    assert sm.list_epic_pull_requests("ENG-1")[0]["state"] == "merged"
    assert "ENG-1" not in sm.list_active_epic_keys()


async def test_a_closed_epic_pull_request_ends_the_epic_as_complete(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _run_to_merged(sm)
    await poll_once(sm, wait=True)

    _FakeGitHub.pr_state = "closed"
    await poll_once(sm, wait=True)

    assert sm.get_epic("ENG-1")["phase"] == "complete"


async def test_without_github_the_epic_branch_is_left_ready_locally(git_repo, tmp_path, project_id, monkeypatch, github):
    _FakeGitHub.configured = False
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _run_to_merged(sm)

    await poll_once(sm, wait=True)

    assert sm.get_epic("ENG-1")["phase"] == "complete"
    assert _FakeGitHub.created == [] and github == []


async def test_a_push_failure_is_recorded_and_retried_on_the_next_poll(git_repo, tmp_path, project_id, monkeypatch, github):
    import forge.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _run_to_merged(sm)
    monkeypatch.setattr(daemon, "push_branch", lambda path, branch: (False, "permission denied"))

    await poll_once(sm, wait=True)

    [row] = sm.list_epic_pull_requests("ENG-1")
    assert row["number"] is None and row["error"] == "git push failed: permission denied"
    assert sm.get_epic("ENG-1")["phase"] not in ("pr_open", "complete")

    monkeypatch.setattr(daemon, "push_branch", lambda path, branch: (True, None))
    await poll_once(sm, wait=True)

    [row] = sm.list_epic_pull_requests("ENG-1")
    assert row["number"] == 1 and row["error"] is None
    assert sm.get_epic("ENG-1")["phase"] == "pr_open"


async def test_the_body_names_code_forge_as_co_author_only_when_a_commit_author_is_configured(
    git_repo, tmp_path, project_id, monkeypatch, github
):
    """With no git_author_* set, commits are authored by code_forge itself,
    so a co-author line would only repeat the author and is left off."""
    import forge.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    await _run_to_merged(sm)
    assert "Co-authored-by" not in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))

    set_config("git_author_name", "Leo Elstin")
    set_config("git_author_email", "leo@example.com")
    assert daemon._epic_pr_body(sm, sm.get_epic("ENG-1")).endswith("Co-authored-by: code_forge <forge@localhost>")


async def test_a_manual_run_epic_opens_its_pull_request_after_its_last_story(
    git_repo, tmp_path, project_id, monkeypatch, github
):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    set_project_config(project_id, "auto_run_enabled", "false")

    for _ in range(2):  # dispatch, then merge
        epic = sm.get_epic("ENG-1")
        await execute_story_action(sm, epic, check_story_action(sm, epic, "ENG-2"))

    assert sm.get_story("ENG-2")["status"] == "merged"
    assert len(_FakeGitHub.created) == 1
    assert sm.get_epic("ENG-1")["phase"] == "pr_open"


async def test_an_attached_repo_without_origin_runs_merges_and_ends_ready_locally(tmp_path, project_id, monkeypatch):
    """An attached local repo that was never pushed: no `origin`, default
    branch `master`. Stories still branch (from the local master), merge on
    the epic branch, and the epic ends complete — there is no GitHub remote
    to propose it to, even with a token configured."""
    import subprocess

    import forge.daemon.main as daemon

    repo = tmp_path / "local-app"
    repo.mkdir()
    for args in (["init", "-b", "master"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("local\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    class _ConfiguredGitHub(_FakeGitHub):
        pass

    _ConfiguredGitHub.configured, _ConfiguredGitHub.created = True, []
    monkeypatch.setattr(daemon, "GitHubService", _ConfiguredGitHub)

    sm = _setup(tmp_path, project_id, str(repo), monkeypatch, stories=("ENG-2", "ENG-3"))
    await _run_to_merged(sm)
    assert {sm.get_story(k)["status"] for k in ("ENG-2", "ENG-3")} == {"merged"}

    await poll_once(sm, wait=True)

    epic_branch = integration_branch_name("ENG-1")
    files = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", epic_branch], capture_output=True, text=True)
    assert set(files.stdout.split()) == {"README.md", "ENG-2.txt", "ENG-3.txt"}
    base = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "master", epic_branch])
    assert base.returncode == 0  # branched from the local master
    assert sm.get_epic("ENG-1")["phase"] == "complete"
    assert _ConfiguredGitHub.created == []
    [row] = sm.list_epic_pull_requests("ENG-1")
    assert row["error"] == "origin is not a GitHub remote"
    # The attached folder's own checkout was never touched.
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
    assert subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True).stdout.strip() == "master"
