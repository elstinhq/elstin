"""No real JIRA credentials involved — a fake JiraService stands in so these
tests exercise the MCP wiring itself (dict conversion, lazy/cached client
construction, write-gate passthrough). JiraService's own JIRA-API behavior
is covered elsewhere and isn't re-tested here."""
from __future__ import annotations

import pytest

import elstin.mcp.jira_server as jira_server
from elstin.services.jira_service import JiraTicket

pytestmark = pytest.mark.asyncio


class _FakeJiraService:
    instances_created = 0

    def __init__(self) -> None:
        _FakeJiraService.instances_created += 1
        self.transition_calls: list[tuple[str, str]] = []

    def get_ticket(self, issue_key: str) -> JiraTicket:
        return JiraTicket(key=issue_key, summary="A summary", description="desc", status="To Do", issue_type="Story", priority="Medium")

    def search_tickets(self, jql: str, max_results: int = 50) -> list[JiraTicket]:
        return [self.get_ticket("ENG-1"), self.get_ticket("ENG-2")]

    def get_children(self, issue_key: str) -> list[JiraTicket]:
        return [self.get_ticket("ENG-2")]

    def get_transitions(self, issue_key: str) -> list[dict]:
        return [{"id": "31", "name": "Done", "to_status": "Done"}]

    def transition_ticket(self, issue_key: str, transition_name: str) -> bool:
        self.transition_calls.append((issue_key, transition_name))
        return transition_name.lower() == "done"


@pytest.fixture(autouse=True)
def _reset_cache_and_fake(monkeypatch: pytest.MonkeyPatch):
    jira_server.reset_service_cache()
    _FakeJiraService.instances_created = 0
    monkeypatch.setattr(jira_server, "JiraService", _FakeJiraService)
    yield
    jira_server.reset_service_cache()


async def test_get_issue_returns_plain_dict():
    result = await jira_server.jira_get_issue("ENG-1")
    assert result["key"] == "ENG-1"
    assert result["summary"] == "A summary"
    assert isinstance(result, dict)


async def test_search_returns_list_of_dicts():
    results = await jira_server.jira_search("project = ENG")
    assert [r["key"] for r in results] == ["ENG-1", "ENG-2"]


async def test_get_children():
    results = await jira_server.jira_get_children("ENG-1")
    assert [r["key"] for r in results] == ["ENG-2"]


async def test_get_transitions_passthrough():
    results = await jira_server.jira_get_transitions("ENG-1")
    assert results == [{"id": "31", "name": "Done", "to_status": "Done"}]


async def test_transition_issue_reports_applied():
    result = await jira_server.jira_transition_issue("ENG-1", "Done")
    assert result == {"issue_key": "ENG-1", "transition": "Done", "applied": True}

    result = await jira_server.jira_transition_issue("ENG-1", "Blocked")
    assert result["applied"] is False


async def test_service_is_constructed_once_and_cached():
    await jira_server.jira_get_issue("ENG-1")
    await jira_server.jira_get_issue("ENG-2")
    await jira_server.jira_search("project = ENG")
    assert _FakeJiraService.instances_created == 1


async def test_reset_service_cache_forces_reconstruction():
    await jira_server.jira_get_issue("ENG-1")
    jira_server.reset_service_cache()
    await jira_server.jira_get_issue("ENG-1")
    assert _FakeJiraService.instances_created == 2
