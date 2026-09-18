"""_ci_checks_block_merge is the CI-aware merge gate added to
forge/daemon/main.py — a fake GitHubService stands in (no real GitHub
account), same pattern as tests/test_github_mcp_server.py."""
from __future__ import annotations

import pytest

import forge.daemon.main as daemon_module
from forge.config.store import set_config
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")


class _FakeGitHubService:
    check_status = "success"

    def __init__(self) -> None:
        pass

    @staticmethod
    def is_configured() -> bool:
        return True

    def get_pr_check_status(self, owner: str, repo: str, number: int) -> str:
        return _FakeGitHubService.check_status


@pytest.fixture(autouse=True)
def _fake_github(monkeypatch: pytest.MonkeyPatch, tmp_path, forge_db):
    set_config("github_token", "fake-token")
    monkeypatch.setattr(daemon_module, "GitHubService", _FakeGitHubService)
    monkeypatch.setattr(daemon_module, "parse_github_remote", lambda project_path: ("acme", "widgets"))
    yield
    _FakeGitHubService.check_status = "success"


def _setup_story_with_pr(sm: StateManager, project_id: int) -> None:
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")
    sm.set_story_status("ENG-2", "awaiting_pr_review")
    sm.set_story_gate_approved("ENG-2", True)
    sm.upsert_pull_request("ENG-2", number=42, url="https://github.com/acme/widgets/pull/42", state="open")


def test_no_pull_request_never_blocks(project_id):
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    epic = sm.get_epic("ENG-1")

    assert daemon_module._ci_checks_block_merge(sm, epic, "ENG-2") is False


def test_ci_success_does_not_block(project_id):
    sm = StateManager()
    _setup_story_with_pr(sm, project_id)
    _FakeGitHubService.check_status = "success"
    epic = sm.get_epic("ENG-1")

    assert daemon_module._ci_checks_block_merge(sm, epic, "ENG-2") is False
    # approval untouched
    assert sm.get_story("ENG-2")["gate_approved"] is True


def test_ci_pending_blocks_but_keeps_approval(project_id):
    sm = StateManager()
    _setup_story_with_pr(sm, project_id)
    _FakeGitHubService.check_status = "pending"
    epic = sm.get_epic("ENG-1")

    assert daemon_module._ci_checks_block_merge(sm, epic, "ENG-2") is True
    story = sm.get_story("ENG-2")
    assert story["gate_approved"] is True  # no re-approval needed once CI finishes
    assert story["parked_reason"] == "ci_pending"
    assert story["status"] == "awaiting_pr_review"


def test_ci_failure_blocks_and_clears_approval(project_id):
    sm = StateManager()
    _setup_story_with_pr(sm, project_id)
    _FakeGitHubService.check_status = "failure"
    epic = sm.get_epic("ENG-1")

    assert daemon_module._ci_checks_block_merge(sm, epic, "ENG-2") is True
    story = sm.get_story("ENG-2")
    assert story["gate_approved"] is False  # human must look at it and re-approve
    assert story["parked_reason"] == "ci_failed"


def test_github_not_configured_never_blocks(project_id, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_FakeGitHubService, "is_configured", staticmethod(lambda: False))
    sm = StateManager()
    _setup_story_with_pr(sm, project_id)
    epic = sm.get_epic("ENG-1")

    assert daemon_module._ci_checks_block_merge(sm, epic, "ENG-2") is False
