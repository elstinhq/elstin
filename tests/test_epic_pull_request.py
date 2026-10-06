"""The epic's pull request — the only thing elstin sends to GitHub. Stories
merge into the epic branch locally; once every one has, the daemon pushes
the epic branch and opens one pull request into the default branch
(elstin/daemon/main.py's _finish_epic), then follows its CI status and a
human's merge. Real Postgres and git; GitHub is a fake."""
from __future__ import annotations

import os

import pytest

from elstin.config.store import set_config, set_project_config
from elstin.daemon.main import check_story_action, execute_story_action, poll_once
from elstin.orchestration.integration import integration_branch_name
from elstin.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("elstin_db", "fake_agents"), pytest.mark.asyncio]


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
    import elstin.daemon.main as daemon

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
    import elstin.orchestration.graph as graph

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
    import elstin.daemon.main as daemon

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


async def test_the_body_names_elstin_as_co_author_only_when_a_commit_author_is_configured(
    git_repo, tmp_path, project_id, monkeypatch, github
):
    """With no git_author_* set, commits are authored by elstin itself,
    so a co-author line would only repeat the author and is left off."""
    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    await _run_to_merged(sm)
    assert "Co-authored-by" not in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))

    set_config("git_author_name", "Leo Elstin")
    set_config("git_author_email", "leo@example.com")
    assert daemon._epic_pr_body(sm, sm.get_epic("ENG-1")).endswith("Co-authored-by: elstin <elstin@localhost>")


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

    import elstin.daemon.main as daemon

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


async def test_the_body_carries_this_epics_accepted_lessons(git_repo, tmp_path, project_id, monkeypatch, github):
    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    await _run_to_merged(sm)
    accepted = sm.add_lesson_candidate(project_id, "Build with JDK 21; 26 crashes javac.", story_key="ENG-2")
    sm.set_lesson_status(accepted["id"], "accepted")
    sm.add_lesson_candidate(project_id, "Not yet reviewed by anyone.", story_key="ENG-2")
    other = sm.add_lesson_candidate(project_id, "Learned on some other epic.", story_key="OTHER-1")
    sm.set_lesson_status(other["id"], "accepted")

    body = daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))

    assert "## Lessons from building this" in body
    assert "- Build with JDK 21; 26 crashes javac." in body
    assert "Learned on some other epic." not in body and "Not yet reviewed by anyone." not in body
    assert "1 more from this epic is still waiting for review in elstin." in body
    assert body.index("## Lessons from building this") < body.index("## How this was built")


async def test_a_body_without_lessons_is_unchanged(git_repo, tmp_path, project_id, monkeypatch, github):
    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    await _run_to_merged(sm)

    assert "Lessons" not in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))


async def test_a_lessons_read_failure_never_costs_the_pull_request(git_repo, tmp_path, project_id, monkeypatch, github):
    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    await _run_to_merged(sm)

    def boom(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(sm, "list_project_lessons", boom)

    body = daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))

    assert "## How this was built" in body and "Lessons" not in body


# --- the PR gate (`require_pr_approval`) ----------------------------------------------
#
# The epic branch is the only thing elstin pushes to GitHub, and a pull request is
# visible to other people. With the gate on, an epic whose stories have all merged
# parks as "awaiting" and nothing leaves the machine until a person decides.


def _client():
    from fastapi.testclient import TestClient

    from elstin.app import create_app

    return TestClient(create_app())


def _decide(epic_key: str, action: str):
    with _client() as client:
        return client.post(f"/api/epics/{epic_key}/pr-approval", json={"action": action})


async def _merged_and_held(sm: StateManager) -> None:
    set_config("require_pr_approval", "true")
    await _run_to_merged(sm)
    await poll_once(sm, wait=True)  # every story merged -> the gate holds it


def _titles(epic_key: str) -> list[str]:
    from elstin.services.run_activity import list_activity

    return [e["title"] for e in list_activity(epic_key)]


async def test_with_the_gate_on_nothing_is_pushed_and_the_epic_waits_for_a_person(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)

    await _merged_and_held(sm)

    assert _FakeGitHub.created == [] and github == []
    epic = sm.get_epic("ENG-1")
    assert epic["pr_approval"] == "awaiting" and epic["phase"] not in ("pr_open", "complete")
    assert "ENG-1" in sm.list_active_epic_keys()
    waiting = "All stories merged — waiting for approval to open the pull request"
    assert _titles("ENG-1").count(waiting) == 1

    await poll_once(sm, wait=True)
    await poll_once(sm, wait=True)  # still waiting: asked once, not every poll
    assert _titles("ENG-1").count(waiting) == 1 and _FakeGitHub.created == [] and github == []


async def test_the_gate_is_off_by_default_and_the_pull_request_opens_as_before(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _run_to_merged(sm)
    await poll_once(sm, wait=True)
    assert len(_FakeGitHub.created) == 1 and sm.get_epic("ENG-1")["pr_approval"] is None


async def test_approving_opens_the_pull_request(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _merged_and_held(sm)

    response = _decide("ENG-1", "open")

    assert response.status_code == 200 and response.json()["pr_approval"] == "approved"
    assert len(_FakeGitHub.created) == 1 and github == [integration_branch_name("ENG-1")]
    assert sm.get_epic("ENG-1")["phase"] == "pr_open"
    [row] = sm.list_epic_pull_requests("ENG-1")
    assert row["number"] == 1 and row["error"] is None


async def test_declining_completes_the_epic_with_its_branch_kept_local(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _merged_and_held(sm)

    response = _decide("ENG-1", "skip")

    assert response.status_code == 200 and response.json()["pr_approval"] == "skipped"
    assert _FakeGitHub.created == [] and github == []
    assert sm.get_epic("ENG-1")["phase"] == "complete"
    assert "ENG-1" not in sm.list_active_epic_keys()
    assert f"Epic complete: {integration_branch_name('ENG-1')} kept locally" in _titles("ENG-1")
    assert sm.list_epic_pull_requests("ENG-1") == []
    # the branch is still there, locally
    import subprocess

    branches = subprocess.run(["git", "-C", git_repo, "branch", "--list"], capture_output=True, text=True).stdout
    assert integration_branch_name("ENG-1") in branches


async def test_a_decline_stands_even_if_the_gate_is_turned_off_afterwards(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _merged_and_held(sm)
    sm.set_epic_pr_approval("ENG-1", "skipped")
    set_config("require_pr_approval", "false")

    await poll_once(sm, wait=True)

    assert _FakeGitHub.created == [] and sm.get_epic("ENG-1")["phase"] == "complete"


async def test_turning_the_gate_off_while_an_epic_waits_lets_it_open(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _merged_and_held(sm)

    set_config("require_pr_approval", "false")
    await poll_once(sm, wait=True)

    assert len(_FakeGitHub.created) == 1 and sm.get_epic("ENG-1")["phase"] == "pr_open"


async def test_a_project_can_hold_its_pull_requests_while_others_do_not(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    set_project_config(project_id, "require_pr_approval", "true")  # the global setting stays off
    await _run_to_merged(sm)

    await poll_once(sm, wait=True)

    assert _FakeGitHub.created == [] and sm.get_epic("ENG-1")["pr_approval"] == "awaiting"


async def test_a_project_can_opt_out_when_the_global_setting_holds_everything(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    set_config("require_pr_approval", "true")
    set_project_config(project_id, "require_pr_approval", "false")
    await _run_to_merged(sm)

    await poll_once(sm, wait=True)

    assert len(_FakeGitHub.created) == 1


async def test_with_no_github_there_is_nothing_to_approve(git_repo, tmp_path, project_id, monkeypatch, github):
    """Nowhere to propose the branch: the epic ends ready locally as it always did,
    without asking for permission to do nothing."""
    _FakeGitHub.configured = False
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)

    await _merged_and_held(sm)

    epic = sm.get_epic("ENG-1")
    assert epic["phase"] == "complete" and epic["pr_approval"] is None


async def test_with_nothing_to_propose_there_is_nothing_to_approve(git_repo, tmp_path, project_id, monkeypatch, github):
    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    monkeypatch.setattr(daemon, "_repos_worth_proposing", lambda sm_, epic_: [])

    await _merged_and_held(sm)

    epic = sm.get_epic("ENG-1")
    assert epic["phase"] == "complete" and epic["pr_approval"] is None
    assert "Epic complete: no changes to propose" in _titles("ENG-1")


async def test_an_approved_epic_whose_push_fails_stays_approved_and_is_retried(git_repo, tmp_path, project_id, monkeypatch, github):
    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _merged_and_held(sm)
    monkeypatch.setattr(daemon, "push_branch", lambda path, branch: (False, "permission denied"))

    _decide("ENG-1", "open")

    [row] = sm.list_epic_pull_requests("ENG-1")
    assert row["error"] == "git push failed: permission denied"
    assert sm.get_epic("ENG-1")["pr_approval"] == "approved"  # not asked again

    monkeypatch.setattr(daemon, "push_branch", lambda path, branch: (True, None))
    await poll_once(sm, wait=True)

    assert len(_FakeGitHub.created) == 1 and sm.get_epic("ENG-1")["phase"] == "pr_open"
    assert _titles("ENG-1").count("All stories merged — waiting for approval to open the pull request") == 1


async def test_the_poll_and_an_approval_arriving_together_open_one_pull_request(git_repo, tmp_path, project_id, monkeypatch, github):
    import asyncio

    import elstin.daemon.main as daemon

    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)
    await _merged_and_held(sm)
    sm.set_epic_pr_approval("ENG-1", "approved")

    await asyncio.gather(*(daemon._finish_epic(sm, sm.get_epic("ENG-1")) for _ in range(4)))

    assert len(_FakeGitHub.created) == 1 and len(github) == 1


async def test_a_manual_run_project_is_held_too_and_approval_opens_it(git_repo, tmp_path, project_id, monkeypatch, github):
    """No poll visits a manual project's epics, so the approval itself has to do the work."""
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch, stories=("ENG-2",))
    set_project_config(project_id, "auto_run_enabled", "false")
    set_config("require_pr_approval", "true")

    for _ in range(2):  # dispatch, then merge
        epic = sm.get_epic("ENG-1")
        await execute_story_action(sm, epic, check_story_action(sm, epic, "ENG-2"))

    assert sm.get_story("ENG-2")["status"] == "merged"
    assert _FakeGitHub.created == [] and sm.get_epic("ENG-1")["pr_approval"] == "awaiting"

    assert _decide("ENG-1", "open").status_code == 200

    assert len(_FakeGitHub.created) == 1 and sm.get_epic("ENG-1")["phase"] == "pr_open"


async def test_there_is_nothing_to_decide_unless_the_epic_is_waiting(git_repo, tmp_path, project_id, monkeypatch, github):
    sm = _setup(tmp_path, project_id, git_repo, monkeypatch)

    assert _decide("ENG-1", "open").status_code == 409  # still implementing
    assert _decide("NOPE-1", "open").status_code == 404
    await _merged_and_held(sm)
    assert _decide("ENG-1", "maybe").status_code == 422
    assert _decide("ENG-1", "open").status_code == 200
    assert _decide("ENG-1", "skip").status_code == 409  # already decided
    assert len(_FakeGitHub.created) == 1  # the second answer changed nothing


async def test_the_setting_is_a_workflow_setting_a_project_may_override():
    from elstin.config.store import CONFIG_CATEGORIES, PROJECT_OVERRIDABLE_KEYS, get_config

    assert get_config("require_pr_approval") == "false"
    assert "require_pr_approval" in PROJECT_OVERRIDABLE_KEYS
    assert "require_pr_approval" in next(c for c in CONFIG_CATEGORIES if c.id == "workflow").keys
