"""JIRA MCP server, built on mcp.server.fastmcp.FastMCP and wrapping
JiraService (forge/services/jira_service.py) instead of a new client. This
is the boundary agents (dev/planner) get JIRA tools through; the daemon's
own framework-owned reads/writes (status sync, comment watermarks) call
JiraService directly — the framework owns JIRA writes, not agents.

`transition_issue` doesn't need its own write-gate check here — JiraService
.transition_ticket() already refuses when jira_write_enabled is off (see
forge/services/jira_service.py), so this stays a thin pass-through.

Run standalone for manual verification: `python -m forge.mcp.jira_server`
(stdio transport, per pyproject.toml's `forge-mcp-jira` entry point).
"""
from __future__ import annotations

from dataclasses import asdict

from mcp.server.fastmcp import FastMCP

from forge.services.jira_service import JiraService, JiraTicket

mcp = FastMCP("forge-jira")

_service: JiraService | None = None


def _get_service() -> JiraService:
    """Lazily constructed and cached — JiraService.__init__ performs a
    network handshake (myself()), so it shouldn't run at import time
    (before the settings UI has even been filled in) or on every call."""
    global _service
    if _service is None:
        _service = JiraService()
    return _service


def reset_service_cache() -> None:
    """Call after JIRA settings change (a new base URL/token) so the next
    tool call re-authenticates instead of reusing a stale client."""
    global _service
    _service = None


def _ticket_dict(ticket: JiraTicket) -> dict:
    return asdict(ticket)


@mcp.tool()
async def jira_get_issue(issue_key: str) -> dict:
    """Fetch a single JIRA issue by key (e.g. ENG-42)."""
    return _ticket_dict(_get_service().get_ticket(issue_key))


@mcp.tool()
async def jira_search(jql: str, max_results: int = 50) -> list[dict]:
    """Run a JQL query, return matching issues."""
    return [_ticket_dict(t) for t in _get_service().search_tickets(jql, max_results=max_results)]


@mcp.tool()
async def jira_get_children(issue_key: str) -> list[dict]:
    """Direct children of an issue (Epic -> Story, Story -> Sub-task)."""
    return [_ticket_dict(t) for t in _get_service().get_children(issue_key)]


@mcp.tool()
async def jira_get_transitions(issue_key: str) -> list[dict]:
    """Available workflow transitions for an issue."""
    return _get_service().get_transitions(issue_key)


@mcp.tool()
async def jira_transition_issue(issue_key: str, transition_name: str) -> dict:
    """Execute a named workflow transition. Refused (returns applied=False)
    unless jira_write_enabled is turned on in settings."""
    applied = _get_service().transition_ticket(issue_key, transition_name)
    return {"issue_key": issue_key, "transition": transition_name, "applied": applied}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
