"""GET /api/epics, GET /api/epics/{key}, POST /api/epics/sync, and
PUT /api/epics/{key}/repo — the dashboard's data source, JIRA-sync entry
point, and (deliberately separate) folder-assignment entry point."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jira import JIRAError

import forge.api.epic_routes as epic_routes

pytestmark = pytest.mark.usefixtures("forge_db", "fake_agents")


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


def test_list_epics_empty():
    with _client() as client:
        response = client.get("/api/epics")
        assert response.status_code == 200
        assert response.json() == []


def test_list_epics_includes_stories_and_pr_state(project_id):
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Add a widget")
    sm.set_story_status("ENG-2", "awaiting_pr_review")
    sm.upsert_pull_request("ENG-2", number=7, url="https://github.com/acme/widgets/pull/7", state="open")

    with _client() as client:
        epics = client.get("/api/epics").json()
        assert len(epics) == 1
        assert epics[0]["jira_key"] == "ENG-1"
        assert len(epics[0]["stories"]) == 1
        story = epics[0]["stories"][0]
        assert story["jira_key"] == "ENG-2"
        assert story["status"] == "awaiting_pr_review"
        # repo_label is "" for a story that wrote only its default repo.
        assert story["pull_request"] == {
            "story_key": "ENG-2", "repo_label": "", "number": 7,
            "url": "https://github.com/acme/widgets/pull/7", "state": "open",
        }


def test_get_epic_by_key(project_id):
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)

    with _client() as client:
        response = client.get("/api/epics/ENG-1")
        assert response.status_code == 200
        assert response.json()["jira_key"] == "ENG-1"
        assert len(response.json()["stories"]) == 1


def test_get_epic_404_for_unknown_key():
    with _client() as client:
        response = client.get("/api/epics/NOPE-1")
        assert response.status_code == 404


def test_sync_epic_returns_epic_shape_with_no_repo(project_id, monkeypatch: pytest.MonkeyPatch):
    from forge.state.manager import StateManager

    def fake_sync(jira_key: str, *, project_id: int) -> None:
        StateManager().create_epic(jira_key, project_id=project_id, levels=[["ENG-2"]])
        StateManager().create_story("ENG-2", epic_key=jira_key, wave=0, summary="Add a widget")

    monkeypatch.setattr(epic_routes, "sync_epic_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/epics/sync", json={"jira_key": "ENG-1", "project_id": project_id})
        assert response.status_code == 200
        body = response.json()
        assert body["jira_key"] == "ENG-1"
        assert body["repo"] is None
        assert [s["jira_key"] for s in body["stories"]] == ["ENG-2"]


def test_sync_epic_404_for_unknown_project():
    with _client() as client:
        response = client.post("/api/epics/sync", json={"jira_key": "ENG-1", "project_id": 999})
        assert response.status_code == 404


def test_sync_epic_400_when_jira_not_configured(project_id, monkeypatch: pytest.MonkeyPatch):
    def fake_sync(jira_key: str, *, project_id: int) -> None:
        raise ValueError("JIRA integration is not configured.")

    monkeypatch.setattr(epic_routes, "sync_epic_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/epics/sync", json={"jira_key": "ENG-1", "project_id": project_id})
        assert response.status_code == 400


def test_sync_epic_409_when_epic_belongs_to_different_project(project_id, monkeypatch: pytest.MonkeyPatch):
    def fake_sync(jira_key: str, *, project_id: int) -> None:
        raise ValueError(f"{jira_key} already belongs to a different project (project_id=1)")

    monkeypatch.setattr(epic_routes, "sync_epic_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/epics/sync", json={"jira_key": "ENG-1", "project_id": project_id})
        assert response.status_code == 409


def test_sync_epic_404_when_jira_ticket_not_found(project_id, monkeypatch: pytest.MonkeyPatch):
    def fake_sync(jira_key: str, *, project_id: int) -> None:
        raise JIRAError("no such issue", status_code=404)

    monkeypatch.setattr(epic_routes, "sync_epic_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/epics/sync", json={"jira_key": "NOPE-1", "project_id": project_id})
        assert response.status_code == 404


def test_sync_epic_fields_refreshes_prose(project_id, monkeypatch: pytest.MonkeyPatch):
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic(
        "ENG-1", project_id=project_id, levels=[["ENG-2"]],
        summary="Stale epic", description="Stale body",
    )
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.set_epic_phase("ENG-1", "building")

    def fake_sync(epic_key: str) -> dict:
        sm.update_epic_jira_fields(epic_key, summary="Fresh epic", description="Fresh body")
        return sm.get_epic(epic_key)

    monkeypatch.setattr(epic_routes, "sync_epic_fields_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/epics/ENG-1/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "Fresh epic"
    assert body["description"] == "Fresh body"
    assert body["phase"] == "building"
    assert [s["jira_key"] for s in body["stories"]] == ["ENG-2"]


def test_sync_epic_fields_404_for_unknown_key(monkeypatch: pytest.MonkeyPatch):
    def fake_sync(epic_key: str) -> dict:
        raise ValueError(f"No such epic: {epic_key}")

    monkeypatch.setattr(epic_routes, "sync_epic_fields_from_jira", fake_sync)

    with _client() as client:
        response = client.post("/api/epics/NOPE-1/sync")
    assert response.status_code == 404


def test_set_epic_repo(project_id):
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    repo_id = sm.add_project_repo(project_id, "/repo/widgets")

    with _client() as client:
        response = client.put("/api/epics/ENG-1/repo", json={"project_repo_id": repo_id})
        assert response.status_code == 200
        assert response.json()["repo"] == "/repo/widgets"


def test_set_epic_repo_400_for_repo_from_another_project(project_id):
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    other_project_id = sm.create_project("Other")
    other_repo_id = sm.add_project_repo(other_project_id, "/repo/other")

    with _client() as client:
        response = client.put("/api/epics/ENG-1/repo", json={"project_repo_id": other_repo_id})
        assert response.status_code == 400


def test_set_epic_repo_404_for_unknown_epic():
    with _client() as client:
        response = client.put("/api/epics/NOPE-1/repo", json={"project_repo_id": 1})
        assert response.status_code == 404


def test_set_epic_gate_persists_and_toggles(project_id):
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    assert sm.get_epic("ENG-1")["manual_gate"] is True

    with _client() as client:
        response = client.put("/api/epics/ENG-1/gate", json={"manual_gate": True})
        assert response.status_code == 200
        assert response.json()["manual_gate"] is True
        assert sm.get_epic("ENG-1")["manual_gate"] is True

        response = client.put("/api/epics/ENG-1/gate", json={"manual_gate": False})
        assert response.status_code == 200
        assert response.json()["manual_gate"] is False


def test_set_epic_gate_404_for_unknown_epic():
    with _client() as client:
        response = client.put("/api/epics/NOPE-1/gate", json={"manual_gate": True})
        assert response.status_code == 404


def test_projects_page_serves():
    with _client() as client:
        response = client.get("/projects")
        assert response.status_code == 200
        assert "code_forge" in response.text

        root = client.get("/")
        assert root.status_code == 200


def test_project_detail_page_serves(project_id):
    with _client() as client:
        response = client.get(f"/projects/{project_id}")
        assert response.status_code == 200
        assert "code_forge" in response.text
