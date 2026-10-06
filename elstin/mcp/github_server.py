"""GitHub MCP server — mirrors elstin/mcp/jira_server.py's shape exactly.
The daemon's own framework-owned GitHub writes (opening/merging a story's
PR) call elstin/services/github_service.py directly rather than through
this server, matching the same "framework owns writes, this boundary is
for agents" split as the JIRA server. This exists for future agent use
(e.g. a PR-feedback-responding agent commenting on a PR) — not wired to
any agent yet, same status as the JIRA server's transition tool before an
agent role uses it.

Run standalone for manual verification: `python -m elstin.mcp.github_server`.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from elstin.services.github_service import GitHubService

mcp = FastMCP("elstin-github")

_service: GitHubService | None = None


def _get_service() -> GitHubService:
    global _service
    if _service is None:
        _service = GitHubService()
    return _service


def reset_service_cache() -> None:
    global _service
    _service = None


@mcp.tool()
async def github_get_pull_request(owner: str, repo: str, number: int) -> dict:
    """Fetch a pull request's current state."""
    return _get_service().get_pull_request(owner, repo, number)


@mcp.tool()
async def github_add_pr_comment(owner: str, repo: str, number: int, body: str) -> dict:
    """Post a comment on a pull request."""
    return _get_service().add_comment(owner, repo, number, body)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
