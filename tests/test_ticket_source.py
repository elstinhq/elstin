"""The ticket-source seam (elstin/services/ticket_source.py): which source a
project uses, that unknown ones fail loudly, and that a synced ticket
records where it came from."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from elstin.services import epic_sync, ticket_source
from elstin.services.jira_service import JiraTicket
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")


class _FakeSource:
    name = "fake"

    def __init__(self, project_id: int | None = None) -> None:
        self.project_id = project_id

    def normalize_key(self, raw):
        return raw

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[JiraTicket]:
        return [JiraTicket(key=root_key, summary="s", description="", status="To Do", issue_type="Story", priority=None)]


@pytest.fixture()
def fake_source_registered(monkeypatch: pytest.MonkeyPatch):
    # Defaults first: registering them after the copy below would put them in
    # the copy only, and the module's "already registered" flag would then
    # hide their absence from every later test.
    ticket_source._ensure_default_sources_registered()
    monkeypatch.setattr(ticket_source, "_REGISTRY", dict(ticket_source._REGISTRY))
    ticket_source.register_source("fake", lambda project_id: _FakeSource(project_id), lambda project_id: True)


def test_a_new_project_uses_jira():
    project_id = StateManager().create_project("Widgets")

    assert ticket_source.project_source_name(project_id) == "jira"
    assert ticket_source.project_source_name(None) == "jira"
    assert StateManager().get_project(project_id)["ticket_source"] == "jira"


def test_the_project_setting_selects_the_registered_source(fake_source_registered):
    sm = StateManager()
    project_id = sm.create_project("Widgets")
    sm.update_project(project_id, ticket_source="fake")

    source = ticket_source.get_ticket_source(project_id)

    assert isinstance(source, _FakeSource) and source.project_id == project_id
    assert ticket_source.ticket_source_configured(project_id) is True


def test_an_unregistered_source_fails_loudly_and_counts_as_not_configured():
    sm = StateManager()
    project_id = sm.create_project("Widgets")
    sm.update_project(project_id, ticket_source="linear")  # not written

    with pytest.raises(ValueError, match="'linear' is not available"):
        ticket_source.get_ticket_source(project_id)
    assert ticket_source.ticket_source_configured(project_id) is False


def test_syncing_records_which_source_each_ticket_came_from(fake_source_registered):
    sm = StateManager()
    project_id = sm.create_project("Widgets")
    sm.update_project(project_id, ticket_source="fake")

    epic_sync.sync_epic_from_jira("WID-1", project_id=project_id)

    epic = sm.get_epic("WID-1")
    story = sm.get_story("WID-1")
    assert (epic["external_source"], epic["external_id"]) == ("fake", "WID-1")
    assert (story["external_source"], story["external_id"]) == ("fake", "WID-1")


def test_a_ticket_created_natively_has_no_external_source():
    sm = StateManager()
    project_id = sm.create_project("Widgets")
    sm.create_epic("WID-9", project_id=project_id, levels=[["WID-10"]])
    sm.create_story("WID-10", epic_key="WID-9", wave=0)

    assert sm.get_epic("WID-9")["external_source"] is None
    assert sm.get_story("WID-10")["external_id"] is None


def test_the_project_route_only_accepts_a_registered_source(fake_source_registered):
    from elstin.app import create_app

    project_id = StateManager().create_project("Widgets")
    with TestClient(create_app()) as client:
        assert client.put(f"/api/projects/{project_id}", json={"ticket_source": "nope"}).status_code == 422
        ok = client.put(f"/api/projects/{project_id}", json={"ticket_source": "fake"})
        assert ok.status_code == 200 and ok.json()["ticket_source"] == "fake"
