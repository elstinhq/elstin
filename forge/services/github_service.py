"""GitHub integration, using PyGithub, following the same shape as
forge/services/jira_service.py: a thin wrapper the daemon calls directly
for its own framework-owned writes (push branch, open PR, merge PR), and
forge/mcp/github_server.py wraps a read/comment subset as MCP tools for
future agent use.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile

from forge.config.store import get_config

logger = logging.getLogger("forge.github_service")

# The first pattern tolerates an optional userinfo prefix (e.g.
# `x-access-token@github.com`) — forge.services.repo_clone.clone_repository
# rewrites `origin` to that form for token-authenticated clones, and that
# rewrite is what a cloned repo's remote permanently looks like afterward.
_REMOTE_URL_PATTERNS = (
    re.compile(r"^https://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"),
)


def parse_github_remote(project_path: str) -> tuple[str, str] | None:
    """(owner, repo) from the local clone's `origin` remote, or None if it's
    not a GitHub remote (or has none)."""
    proc = subprocess.run(["git", "-C", project_path, "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    for pattern in _REMOTE_URL_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group("owner"), match.group("repo")
    return None


def git_auth_env() -> tuple[dict[str, str], str | None]:
    """Extra subprocess env for a git command that talks to a GitHub-hosted
    `origin` rewritten to the `x-access-token@github.com` form (see the
    module docstring above `_REMOTE_URL_PATTERNS`). That rewrite's askpass
    script only lives for the duration of the clone itself
    (repo_clone._resolve_auth deletes it right after), so any later push or
    fetch has nothing to authenticate with unless this supplies fresh
    credentials the same way.

    Returns (env, askpass_path); the caller deletes `askpass_path` when the
    command is done. `GIT_TERMINAL_PROMPT=0` is part of the env on purpose:
    without it a rejected credential makes git ask on the daemon's own
    terminal, and the command hangs there for good."""
    token = get_config("github_token")
    if not token:
        return {}, None
    fd, path = tempfile.mkstemp(prefix="forge-git-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write('#!/bin/sh\necho "$FORGE_PUSH_GIT_TOKEN"\n')
    os.chmod(path, 0o700)
    return {"GIT_ASKPASS": path, "FORGE_PUSH_GIT_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}, path


def git_auth_env_for(project_path: str) -> tuple[dict[str, str], str | None]:
    """`git_auth_env()`, but only when `project_path`'s `origin` is on
    github.com. Anything that runs against every project's clone (a fetch)
    must use this: the configured token is a GitHub token, and an askpass
    script answers whichever host asks, so handing it to a GitLab or
    self-hosted `origin` would send that host a credential it has no
    business seeing."""
    if parse_github_remote(project_path) is None:
        return {}, None
    return git_auth_env()


def push_branch(project_path: str, branch: str) -> tuple[bool, str | None]:
    """(success, error). `error` is git's own stderr — the caller persists
    it (Story.pr_open_error) so a permission/auth failure is visible in the
    UI instead of only in this process's logs."""
    extra_env, askpass_path = git_auth_env()
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        proc = subprocess.run(
            ["git", "-C", project_path, "push", "-u", "origin", branch],
            capture_output=True, text=True, check=False, env=env,
        )
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass
    if proc.returncode != 0:
        error = proc.stderr.strip() or "git push failed"
        logger.warning("git push failed for branch=%s: %s", branch, error)
        return False, error
    return True, None


class GitHubService:
    """Thin wrapper around PyGithub. Requires a GitHub token to be set via
    the settings UI (stored in Postgres, read through forge.config.store)."""

    def __init__(self) -> None:
        if not self.is_configured():
            raise ValueError("GitHub integration is not configured. Set the GitHub token at /settings.")
        from github import Auth, Github

        self._client = Github(auth=Auth.Token(get_config("github_token")))

    @staticmethod
    def is_configured() -> bool:
        return bool(get_config("github_token"))

    def create_pull_request(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str) -> dict:
        gh_repo = self._client.get_repo(f"{owner}/{repo}")
        pr = gh_repo.create_pull(base=base, head=head, title=title, body=body)
        logger.info("Opened PR #%d for %s/%s: %s <- %s", pr.number, owner, repo, base, head)
        return {"number": pr.number, "url": pr.html_url, "state": "open"}

    def find_pull_requests(self, owner: str, repo: str, head_branch: str, *, limit: int = 3) -> list[dict]:
        """Pull requests from `head_branch` (a branch of this same repo),
        newest first — what the prior-work lookup shows next to a branch
        it found (forge/services/prior_work.py)."""
        gh_repo = self._client.get_repo(f"{owner}/{repo}")
        found = []
        for pr in gh_repo.get_pulls(state="all", head=f"{owner}:{head_branch}", sort="created", direction="desc"):
            found.append({
                "number": pr.number, "url": pr.html_url,
                "state": "merged" if pr.merged else ("closed" if pr.state == "closed" else "open"),
            })
            if len(found) >= limit:
                break
        return found

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        gh_repo = self._client.get_repo(f"{owner}/{repo}")
        pr = gh_repo.get_pull(number)
        return {"number": pr.number, "url": pr.html_url, "state": "merged" if pr.merged else ("closed" if pr.state == "closed" else "open")}

    def get_pr_check_status(self, owner: str, repo: str, number: int) -> str:
        """"success" | "pending" | "failure" — combines both the legacy
        Commit Status API and the modern Check Runs API (GitHub Actions
        reports through the latter), since either or both may be in use on
        a given repo. No statuses and no check runs at all is "success" —
        nothing to block on, matching GitHub's own merge-button default."""
        gh_repo = self._client.get_repo(f"{owner}/{repo}")
        pr = gh_repo.get_pull(number)
        commit = gh_repo.get_commit(pr.head.sha)

        failure_conclusions = {"failure", "cancelled", "timed_out", "action_required"}
        for run in commit.get_check_runs():
            if run.status != "completed":
                return "pending"
            if run.conclusion in failure_conclusions:
                return "failure"

        combined = commit.get_combined_status().state
        if combined in ("failure", "error"):
            return "failure"
        if combined == "pending":
            return "pending"
        return "success"

    def merge_pull_request(self, owner: str, repo: str, number: int) -> dict:
        gh_repo = self._client.get_repo(f"{owner}/{repo}")
        pr = gh_repo.get_pull(number)
        if pr.merged:
            return {"merged": True, "sha": pr.merge_commit_sha}
        result = pr.merge(merge_method="squash")
        logger.info("Merged PR #%d for %s/%s merged=%s", number, owner, repo, result.merged)
        return {"merged": result.merged, "sha": result.sha}

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        gh_repo = self._client.get_repo(f"{owner}/{repo}")
        pr = gh_repo.get_pull(number)
        comment = pr.create_issue_comment(body)
        return {"id": comment.id, "url": comment.html_url}
