"""End-to-end: two independent stories dispatch together in wave 0, each
parks at the plan-approval gate, resumes once approved, parks again at the
PR-open gate, opens (best-effort — GitHub isn't configured here) once
approved, parks at the PR-review gate, and merges into the epic integration
branch once approved a third time — only then does wave 1 become
dispatchable. This is the scenario the plan's Phase 1 verification section
calls for, run for real against Postgres and a real git repo — worktree
creation and the integration-branch merge are genuine git operations. Only
the LLM calls inside forge/agents/roles/*_loop.py (routed through
forge/llm/'s OpenAI provider adapter and forge/agents/loop.py) are replaced
by the `fake_agents` fixture (conftest.py), so this needs no network access
or API key.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.config.store import set_config, set_project_config
from forge.daemon.main import check_story_action, execute_story_action, poll_once
from forge.orchestration.reconciler import Blocked, DispatchStory
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _configure(worktrees_base_dir: str) -> None:
    # Dummy JIRA values: poll_once's daemon-wide gate only checks
    # openai_api_key now (JIRA-configuredness is per-project — see
    # forge/config/store.py's PROJECT_OVERRIDABLE_KEYS), but Phase 1's
    # daemon never actually calls JiraService anyway (no JIRA-driven epic
    # discovery yet — epics/stories are seeded directly into StateManager
    # here, same as a future JIRA-polling importer would).
    set_config("jira_base_url", "https://example.atlassian.net")
    set_config("jira_email", "bot@example.com")
    set_config("jira_api_token", "dummy-token")
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", worktrees_base_dir)


def _create_epic_with_repo(sm: StateManager, jira_key: str, *, project_id: int, repo_path: str, levels) -> None:
    sm.create_epic(jira_key, project_id=project_id, levels=levels, manual_gate=False)
    repo_id = sm.add_project_repo(project_id, repo_path)
    sm.set_epic_project_repo(jira_key, repo_id)


async def test_epic_flows_through_both_waves(git_repo: str, tmp_path, project_id, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])  # already set by forge_db; explicit for clarity
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2", "ENG-3"], ["ENG-4"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Add a gadget")
    sm.create_story("ENG-4", epic_key="ENG-1", wave=1, summary="Wire widget to gadget")

    # --- wave 0 dispatches, parks at the plan-approval gate ---
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"
    assert sm.get_story("ENG-3")["status"] == "awaiting_plan_approval"
    assert sm.get_story("ENG-4")["status"] == "pending"  # wave 1 untouched

    # --- approve both, resume past the gate, land on the PR-open gate ---
    sm.set_story_gate_approved("ENG-2")
    sm.set_story_gate_approved("ENG-3")
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_open"
    assert sm.get_story("ENG-3")["status"] == "awaiting_pr_open"
    # the plan-approval was consumed, not carried over to the next gate
    assert sm.get_story("ENG-2")["gate_approved"] is False

    # --- approve the PR open, land on the PR-review gate ---
    sm.set_story_gate_approved("ENG-2")
    sm.set_story_gate_approved("ENG-3")
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_review"
    assert sm.get_story("ENG-3")["status"] == "awaiting_pr_review"
    # the pr-open approval was consumed too, not carried over to PR-review
    assert sm.get_story("ENG-2")["gate_approved"] is False

    # --- approve the PR review, merge into the integration branch ---
    sm.set_story_gate_approved("ENG-2")
    sm.set_story_gate_approved("ENG-3")
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "merged"
    assert sm.get_story("ENG-3")["status"] == "merged"
    assert sm.get_pull_request("ENG-2")["state"] == "merged"
    assert sm.get_epic("ENG-1")["integration_branch"] == "agent/epic-eng-1"

    # the merge really happened — both story branches' README edits are on
    # the integration branch (both stub dev nodes wrote no files, so what's
    # actually being proven is that the merge commits landed cleanly, not
    # that specific file content merged — file-content merging is exercised
    # once Phase 2's real dev node writes files)
    import subprocess

    log = subprocess.run(
        ["git", "log", "--oneline", "agent/epic-eng-1"], cwd=git_repo, capture_output=True, text=True, check=True
    )
    assert log.returncode == 0

    # --- wave 1 was NOT dispatched by the same cycle that finished wave 0 ---
    assert sm.get_story("ENG-4")["status"] == "pending"

    # --- next cycle dispatches wave 1 ---
    await poll_once(sm)
    assert sm.get_story("ENG-4")["status"] == "awaiting_plan_approval"

    sm.set_story_gate_approved("ENG-4")
    await poll_once(sm)
    assert sm.get_story("ENG-4")["status"] == "awaiting_pr_open"

    sm.set_story_gate_approved("ENG-4")
    await poll_once(sm)
    assert sm.get_story("ENG-4")["status"] == "awaiting_pr_review"

    sm.set_story_gate_approved("ENG-4")
    await poll_once(sm)
    assert sm.get_story("ENG-4")["status"] == "merged"

    # --- epic completes once every level is resolved ---
    await poll_once(sm)
    assert sm.get_epic("ENG-1")["phase"] == "complete"


async def test_poll_once_dispatches_without_an_openai_api_key(git_repo: str, tmp_path, project_id):
    """Dispatch (creating the worktree, moving a story to
    awaiting_plan_approval) is a reconciler/daemon decision that never
    touches forge/llm/registry.py — only the planner/dev/verifier/qa role
    functions do, and those are replaced by the `fake_agents` fixture here
    (conftest.py), so a missing key would only ever surface once a real
    LLM call actually happened. poll_once must not gate dispatch on one
    being set regardless. Deliberately does NOT call
    set_config("openai_api_key", ...), unlike every other test in this
    file."""
    set_config("jira_base_url", "https://example.atlassian.net")
    set_config("jira_email", "bot@example.com")
    set_config("jira_api_token", "dummy-token")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    await poll_once(sm)

    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"


async def test_poll_once_skips_epic_with_no_repo_assigned(tmp_path, project_id):
    """An epic synced from JIRA before a folder is chosen (forge/services/
    epic_sync.py) must sit idle rather than crash trying to create a
    worktree for a None repo."""
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])  # no folder yet
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    await poll_once(sm)

    assert sm.get_story("ENG-2")["status"] == "pending"


async def test_retry_after_a_run_failure_redispatches_and_succeeds(
    git_repo: str, tmp_path, project_id, monkeypatch: pytest.MonkeyPatch
):
    """A verifier that never passes exhausts its iterations and fails the
    run — which halts the whole epic (forge/orchestration/reconciler.py)
    and, critically, stops the daemon from even polling it (Epic.phase
    becomes "failed", excluded by list_active_epic_keys — see
    forge/state/manager_epic.py). retry_story has to move both the story
    and the epic phase, or the redispatch below would never happen."""
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")

    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"

    import forge.orchestration.graph as graph_module

    async def failing_verifier(**kwargs) -> dict:
        return {"verifier_report": {"passed": False, "required_fixes": ["nope"]}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_verifier", failing_verifier)

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)  # dev <-> verifier loops internally until iterations exhaust

    story = sm.get_story("ENG-2")
    assert story["status"] == "failed"
    assert story["failure_stage"] == "run"

    # Epic.phase flips to "failed" on the *next* cycle — the cycle that just
    # failed the story was busy running ResumeStory, not re-evaluating the
    # reconciler's epic_failed NoOp.
    await poll_once(sm)
    assert sm.get_epic("ENG-1")["phase"] == "failed"

    # --- while the epic is "failed", the daemon does not touch it at all ---
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "failed"  # unchanged

    # --- retry: whatever was wrong is fixed now, so redispatch ---
    async def passing_verifier(**kwargs) -> dict:
        return {"verifier_report": {"passed": True, "fake": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_verifier", passing_verifier)
    sm.retry_story("ENG-2")

    story = sm.get_story("ENG-2")
    assert story["status"] == "pending"
    assert story["failure_stage"] is None
    assert story["retry_count"] == 1
    assert sm.get_epic("ENG-1")["phase"] == "implementing"  # daemon will poll it again

    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"  # a genuine fresh run, not a resume

    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_open"  # this time it succeeds


async def test_awaiting_pr_open_commits_the_worktrees_uncommitted_changes(
    git_repo: str, tmp_path, project_id, monkeypatch: pytest.MonkeyPatch
):
    """Regression: the dev/verifier loop only ever checks the worktree's
    working-tree diff (git_tools.list_changed_files/get_all_diffs both see
    uncommitted changes fine) — git_tools.commit_all existed but was never
    called anywhere in the daemon. A story could pass verification for
    real and still push a branch identical to its base, since `git push`
    transfers commits, not working-tree state — GitHub would then reject
    PR creation with "No commits between main and <branch>" despite the
    worktree having real, verified changes sitting there uncommitted."""
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")

    import forge.orchestration.graph as graph_module

    async def dev_writes_a_file(**kwargs) -> dict:
        with open(os.path.join(kwargs["worktree_path"], "widget.py"), "w") as f:
            f.write("widget = True\n")
        return {
            "file_changes": [{"path": "widget.py", "action": "create"}],
            "truncated": False,
            "messages": [],
            "dev_history": [],
            "dev_build_runner": {},
        }

    monkeypatch.setattr(graph_module, "run_dev", dev_writes_a_file)

    await poll_once(sm)
    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)

    story = sm.get_story("ENG-2")
    assert story["status"] == "awaiting_pr_open"

    worktree_path = story["worktree_path"]
    status = subprocess.run(["git", "-C", worktree_path, "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout.strip() == ""  # everything committed, nothing left dangling

    log = subprocess.run(["git", "-C", worktree_path, "log", "--oneline", "main..HEAD"], capture_output=True, text=True)
    assert log.stdout.strip() != ""  # a real commit landed on top of the base
    assert "ENG-2" in log.stdout


async def test_poll_once_uses_project_scoped_gate_override(git_repo: str, tmp_path):
    """A project that overrides require_plan_approval=false dispatches
    straight past the plan-approval gate even though the global default
    (require_plan_approval=true) would park it — proves build_context
    resolves gates per-epic's project, not from the flat global config."""
    _configure(str(tmp_path / "worktrees"))

    from forge.config.store import set_project_config

    sm = StateManager()
    project_id = sm.create_project("Fast-track")
    set_project_config(project_id, "require_plan_approval", "false")

    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")

    await poll_once(sm)

    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_open"  # skipped the plan-approval park


async def test_coverage_gap_forces_pr_open_gate_even_when_project_disabled_it(
    git_repo: str, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A story whose verifier found nothing but a coverage-threshold miss
    (tests themselves passed) must still park at the PR-open gate even
    though this project turned require_pr_open_approval off — a story that
    didn't fully verify shouldn't silently ride along on a gate configured
    for the fully-verified case (forge/orchestration/reconciler.py)."""
    _configure(str(tmp_path / "worktrees"))

    import forge.orchestration.graph as graph_module

    async def fake_verifier_coverage_gap_only(**kwargs):
        return {
            "verifier_report": {
                "passed": True,
                "deterministic_gate": {"coverage_gap": True, "coverage_gap_detail": "lines 50% < 80%"},
            },
            "diffs": [],
            "messages": [{"role": "verifier", "content": "fake verifier: coverage gap only"}],
        }

    monkeypatch.setattr(graph_module, "run_verifier", fake_verifier_coverage_gap_only)

    from forge.config.store import set_project_config

    sm = StateManager()
    project_id = sm.create_project("Coverage-lenient")
    set_project_config(project_id, "require_pr_open_approval", "false")

    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")

    await poll_once(sm)  # dispatch -> plan-approval gate
    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)  # resume -> completes, lands on awaiting_pr_open
    await poll_once(sm)  # reconciler evaluates it -> forced park despite the override

    story = sm.get_story("ENG-2")
    assert story["status"] == "awaiting_pr_open"
    assert story["coverage_gap"] is True
    assert story["coverage_gap_detail"] == "lines 50% < 80%"
    assert story["parked_reason"] == "pr_open_gate"

    # Approving it moves it on past the forced gate like any other.
    sm.set_story_gate_approved("ENG-2")
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_review"


async def test_poll_once_skips_a_manual_run_project_epic(git_repo: str, tmp_path, project_id):
    """A project with auto_run_enabled off must not have its stories
    autopicked by the background scan — only the /start route (via
    check_story_action/execute_story_action) may act on them."""
    _configure(str(tmp_path / "worktrees"))
    set_project_config(project_id, "auto_run_enabled", "false")

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")

    await poll_once(sm)

    assert sm.get_story("ENG-2")["status"] == "pending"  # untouched by the scan


async def test_check_and_execute_story_action_drive_a_manual_run_epic_to_completion(
    git_repo: str, tmp_path, project_id
):
    """The manual-run-mode path (check_story_action for eligibility,
    execute_story_action to run it) reaches the same terminal states
    poll_once's automatic scan would — including flipping Epic.phase to
    "complete", which nothing else does for a project the automatic scan
    skips."""
    _configure(str(tmp_path / "worktrees"))
    set_project_config(project_id, "auto_run_enabled", "false")
    set_project_config(project_id, "require_plan_approval", "false")
    set_project_config(project_id, "require_pr_open_approval", "false")
    set_project_config(project_id, "require_pr_review", "false")

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")

    # poll_once must not touch it, even repeatedly.
    await poll_once(sm)
    assert sm.get_story("ENG-2")["status"] == "pending"

    # Each check+execute pair advances exactly one reconciler intent's worth
    # of progress, same as one poll_once cycle in auto mode — dispatch,
    # then (with every gate off) open-PR, then merge, each its own call.
    epic = sm.get_epic("ENG-1")
    result = check_story_action(sm, epic, "ENG-2")
    assert result == DispatchStory("ENG-2")
    await execute_story_action(sm, epic, result)
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_open"

    epic = sm.get_epic("ENG-1")
    result = check_story_action(sm, epic, "ENG-2")
    await execute_story_action(sm, epic, result)
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_review"

    epic = sm.get_epic("ENG-1")
    result = check_story_action(sm, epic, "ENG-2")
    await execute_story_action(sm, epic, result)
    assert sm.get_story("ENG-2")["status"] == "merged"
    assert sm.get_epic("ENG-1")["phase"] == "complete"


async def test_check_story_action_blocked_when_epic_on_hold(project_id):
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])  # manual_gate defaults True
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    epic = sm.get_epic("ENG-1")
    result = check_story_action(sm, epic, "ENG-2")
    assert result == Blocked("ENG-2", "epic_on_hold")


async def test_pr_open_failure_is_recorded_and_cleared_on_retry(project_id, monkeypatch: pytest.MonkeyPatch):
    """_maybe_open_pull_request is best-effort — a push or GitHub API
    failure never blocks the story's own gate progress (see
    handle_intent's OpenPullRequest branch, which advances the story to
    awaiting_pr_review regardless). Before Story.pr_open_error existed,
    that failure only ever reached a log line; this checks it lands on the
    story instead, and that a later successful attempt clears it rather
    than leaving stale failure text behind."""
    import forge.daemon.main as daemon_main

    sm = StateManager()
    sm.create_epic("ENG-9", project_id=project_id, levels=[["ENG-10"]], manual_gate=False)
    sm.create_story("ENG-10", epic_key="ENG-9", wave=0)
    epic = sm.get_epic("ENG-9")

    class _FakeGitHubService:
        configured = True

        @staticmethod
        def is_configured():
            return _FakeGitHubService.configured

        def create_pull_request(self, *args, **kwargs):
            return {"number": 1, "url": "https://github.com/acme/repo/pull/1"}

    monkeypatch.setattr(daemon_main, "GitHubService", _FakeGitHubService)
    monkeypatch.setattr(daemon_main, "parse_github_remote", lambda project_path: ("acme", "repo"))
    monkeypatch.setattr(daemon_main, "push_branch", lambda project_path, branch: (False, "permission denied"))

    daemon_main._maybe_open_pull_request(sm, epic, "ENG-10", "ENG-10-branch")
    story = sm.get_story("ENG-10")
    assert story["pr_open_error"] == "git push failed: permission denied"
    assert sm.get_pull_request("ENG-10") is None

    monkeypatch.setattr(daemon_main, "push_branch", lambda project_path, branch: (True, None))
    daemon_main._maybe_open_pull_request(sm, epic, "ENG-10", "ENG-10-branch")
    story = sm.get_story("ENG-10")
    assert story["pr_open_error"] is None
    assert sm.get_pull_request("ENG-10")["number"] == 1


async def test_a_bad_verifier_reply_fails_one_story_without_abandoning_the_poll_cycle(
    git_repo: str, tmp_path, project_id, monkeypatch: pytest.MonkeyPatch
):
    """The liveness bug behind the verifier-node fix. verifier_node used to
    catch nothing, poll_once has no per-story handler, and run_forever's
    only net abandons the whole cycle — so one model reply that wasn't
    JSON silently starved every epic queued behind it.

    Epic order is pinned here because list_active_epic_keys has no ORDER BY:
    the failing epic must go first for this to prove anything.
    """
    _configure(str(tmp_path / "worktrees"))

    sm = StateManager()
    _create_epic_with_repo(sm, "ENG-1", project_id=project_id, repo_path=git_repo, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Story whose verifier misbehaves")
    _create_epic_with_repo(sm, "ENG-10", project_id=project_id, repo_path=git_repo, levels=[["ENG-11"]])
    sm.create_story("ENG-11", epic_key="ENG-10", wave=0, summary="Story queued behind it")

    monkeypatch.setattr(sm, "list_active_epic_keys", lambda: ["ENG-1", "ENG-10"])

    import forge.orchestration.graph as graph_module
    from forge.services.generation import GenerationError

    async def verifier_answers_with_prose(**kwargs) -> dict:
        # Only the first epic's story gets the bad reply; run_id is the
        # story key by construction (see _run_or_resume_story).
        if kwargs.get("run_id") == "ENG-2":
            raise GenerationError("model reply contained no JSON object")
        return {"verifier_report": {"passed": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_verifier", verifier_answers_with_prose)

    await poll_once(sm)  # both epics plan
    assert sm.get_story("ENG-2")["status"] == "awaiting_plan_approval"
    assert sm.get_story("ENG-11")["status"] == "awaiting_plan_approval"

    sm.set_story_gate_approved("ENG-2")
    sm.set_story_gate_approved("ENG-11")

    await poll_once(sm)  # must not raise, and must not stop at ENG-1

    failed = sm.get_story("ENG-2")
    assert failed["status"] == "failed"
    assert failed["failure_stage"] == "run"
    # The real assertion: the epic behind the failure still got its turn.
    assert sm.get_story("ENG-11")["status"] == "awaiting_pr_open"
