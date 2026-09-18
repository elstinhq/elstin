"""No real GitHub token involved — a fake GitHubService stands in, same
pattern as tests/test_jira_mcp_server.py."""
from __future__ import annotations

import pytest

import forge.mcp.github_server as github_server

pytestmark = pytest.mark.asyncio


class _FakeGitHubService:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        return {"number": number, "url": f"https://github.com/{owner}/{repo}/pull/{number}", "state": "open"}

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        self.comments.append((number, body))
        return {"id": 1, "url": f"https://github.com/{owner}/{repo}/pull/{number}#comment-1"}


@pytest.fixture(autouse=True)
def _reset_cache_and_fake(monkeypatch: pytest.MonkeyPatch):
    github_server.reset_service_cache()
    monkeypatch.setattr(github_server, "GitHubService", _FakeGitHubService)
    yield
    github_server.reset_service_cache()


async def test_get_pull_request():
    result = await github_server.github_get_pull_request("acme", "widgets", 42)
    assert result == {"number": 42, "url": "https://github.com/acme/widgets/pull/42", "state": "open"}


async def test_add_pr_comment():
    result = await github_server.github_add_pr_comment("acme", "widgets", 42, "LGTM")
    assert result == {"id": 1, "url": "https://github.com/acme/widgets/pull/42#comment-1"}
