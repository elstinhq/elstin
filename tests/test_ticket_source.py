"""The ticket-source seam (forge/services/ticket_source.py): which source a
project uses, that unknown ones fail loudly, and that a synced ticket
records where it came from."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from forge.services import epic_sync, ticket_source
from forge.services.jira_service import JiraTicket
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")


class _FakeSource:
    name = "fake"

    def __init__(self, project_id: int | None = None) -> None:
        self.project_id = project_id

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[JiraTicket]:
        return [JiraTicket(key=root_key, summary="s", description="", status="To Do", issue_type="Story", priority=None)]


@pytest.fixture()
def fake_source_registered(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ticket_source, "_REGISTRY", dict(ticket_source._REGISTRY))
    ticket_source._ensure_default_sources_registered()
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
    sm.update_project(project_id, ticket_source="github")  # not written yet

    with pytest.raises(ValueError, match="'github' is not available"):
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
    from forge.app import create_app

    project_id = StateManager().create_project("Widgets")
    with TestClient(create_app()) as client:
        assert client.put(f"/api/projects/{project_id}", json={"ticket_source": "nope"}).status_code == 422
        ok = client.put(f"/api/projects/{project_id}", json={"ticket_source": "fake"})
        assert ok.status_code == 200 and ok.json()["ticket_source"] == "fake"


def test_the_migration_backfills_existing_rows_as_jira():
    """create_all bypasses Alembic, so this is the only check that the
    migration itself does what its docstring says to rows that predate it."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    command.upgrade(config, "20260919_0019")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (id, name, created_at, updated_at) VALUES (1, 'P', now(), now())"))
        conn.execute(text(
            "INSERT INTO epics (jira_key, project_id, summary, phase, plan_approved, manual_gate, created_at, updated_at) "
            "VALUES ('OLD-1', 1, '', 'planning', false, true, now(), now())"
        ))
        conn.execute(text(
            "INSERT INTO stories (jira_key, epic_key, summary, wave, status, kind, gate_approved, coverage_gap, retry_count, created_at, updated_at) "
            "VALUES ('OLD-2', 'OLD-1', '', 0, 'pending', 'dev', false, false, 0, now(), now())"
        ))

    command.upgrade(config, "20260920_0020")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT ticket_source FROM projects WHERE id = 1")).scalar() == "jira"
        for table, key in (("epics", "OLD-1"), ("stories", "OLD-2")):
            row = conn.execute(text(f"SELECT external_source, external_id, external_hash, external_synced_at FROM {table} WHERE jira_key = '{key}'")).one()
            assert tuple(row) == ("jira", key, None, None)
