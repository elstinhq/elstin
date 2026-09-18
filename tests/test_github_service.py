"""parse_github_remote is pure (reads git config, no network) — tested
against real git repos with different remote URL forms. GitHubService
itself needs a real GitHub token to construct, so it's not instantiated
here — see test_github_mcp_server.py for the MCP wiring, tested against a
fake service."""
from __future__ import annotations

import subprocess

from forge.services.github_service import parse_github_remote


def _init_repo_with_remote(tmp_path, remote_url: str) -> str:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=str(repo_path), check=True, capture_output=True)
    return str(repo_path)


def test_parses_https_remote(tmp_path):
    repo_path = _init_repo_with_remote(tmp_path, "https://github.com/acme/widgets.git")
    assert parse_github_remote(repo_path) == ("acme", "widgets")


def test_parses_https_remote_without_dot_git_suffix(tmp_path):
    repo_path = _init_repo_with_remote(tmp_path, "https://github.com/acme/widgets")
    assert parse_github_remote(repo_path) == ("acme", "widgets")


def test_parses_ssh_remote(tmp_path):
    repo_path = _init_repo_with_remote(tmp_path, "git@github.com:acme/widgets.git")
    assert parse_github_remote(repo_path) == ("acme", "widgets")


def test_non_github_remote_returns_none(tmp_path):
    repo_path = _init_repo_with_remote(tmp_path, "https://gitlab.com/acme/widgets.git")
    assert parse_github_remote(repo_path) is None


def test_no_remote_returns_none(tmp_path):
    repo_path = tmp_path / "bare"
    repo_path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_path), check=True, capture_output=True)
    assert parse_github_remote(str(repo_path)) is None
