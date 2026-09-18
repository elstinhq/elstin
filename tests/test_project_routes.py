"""/api/projects — CRUD, repo attach (which clones — see
forge/services/repo_clone.py) and detach, and project-scoped config
overrides. clone_repository is monkeypatched throughout: these are route
tests, not clone tests (see tests/test_repo_clone.py for those), and a real
clone needs a real git source."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import forge.api.project_routes as project_routes
from forge.services.repo_clone import RepoCloneError

pytestmark = pytest.mark.usefixtures("forge_db")


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


def _fake_clone_into(tmp_path):
    """Stands in for a real `git clone` — ignores the (config-driven,
    normally /data/cloned-repos/...) dest_base_dir the route computes and
    creates the fake checkout under pytest's tmp_path instead, so this
    doesn't require a writable /data on the test machine."""

    def _clone(url, *, dest_base_dir, timeout=300):
        from pathlib import Path

        dest = tmp_path / "cloned" / Path(dest_base_dir).name / "widgets"
        (dest / ".git").mkdir(parents=True)
        return str(dest)

    return _clone


def test_create_project_without_folder():
    with _client() as client:
        response = client.post("/api/projects", json={"name": "Widgets", "description": "Widget project"})
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Widgets"
        assert body["description"] == "Widget project"
        assert body["repos"] == []


def test_create_project_with_repo_url(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(project_routes, "clone_repository", _fake_clone_into(tmp_path))

    with _client() as client:
        response = client.post("/api/projects", json={"name": "Widgets", "repo_url": "https://example.com/widgets.git"})
        assert response.status_code == 200
        body = response.json()
        assert len(body["repos"]) == 1
        assert body["repos"][0]["clone_url"] == "https://example.com/widgets.git"
        assert body["repos"][0]["path"].endswith("/widgets")


def test_create_project_clone_failure_returns_400(monkeypatch: pytest.MonkeyPatch):
    def _fail_clone(url, *, dest_base_dir, timeout=300):
        raise RepoCloneError("repository not found")

    monkeypatch.setattr(project_routes, "clone_repository", _fail_clone)

    with _client() as client:
        response = client.post("/api/projects", json={"name": "Widgets", "repo_url": "https://example.com/nope.git"})
        assert response.status_code == 400
        assert response.json()["detail"] == "repository not found"


def test_list_projects_shows_counts():
    from forge.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("Widgets")
    sm.create_epic("ENG-1", project_id=project_id)

    with _client() as client:
        projects = client.get("/api/projects").json()
        assert len(projects) == 1
        assert projects[0]["epic_count"] == 1


def test_get_project_404_for_unknown():
    with _client() as client:
        response = client.get("/api/projects/999")
        assert response.status_code == 404


def test_update_project():
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        response = client.put(f"/api/projects/{project_id}", json={"name": "Widgets v2"})
        assert response.status_code == 200
        assert response.json()["name"] == "Widgets v2"


def test_add_and_remove_project_repo(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from forge.state.manager import StateManager

    monkeypatch.setattr(project_routes, "clone_repository", _fake_clone_into(tmp_path))
    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        add_response = client.post(f"/api/projects/{project_id}/repos", json={"url": "https://example.com/widgets.git"})
        assert add_response.status_code == 200
        repo_id = add_response.json()["id"]

        remove_response = client.delete(f"/api/projects/{project_id}/repos/{repo_id}")
        assert remove_response.status_code == 200
        assert client.get(f"/api/projects/{project_id}/repos").json() == []


def test_add_project_repo_clone_failure_returns_400(monkeypatch: pytest.MonkeyPatch):
    from forge.state.manager import StateManager

    def _fail_clone(url, *, dest_base_dir, timeout=300):
        raise RepoCloneError("auth failed")

    monkeypatch.setattr(project_routes, "clone_repository", _fail_clone)
    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        response = client.post(f"/api/projects/{project_id}/repos", json={"url": "https://example.com/private.git"})
        assert response.status_code == 400
        assert response.json()["detail"] == "auth failed"


def test_remove_project_repo_blocked_while_referenced_by_epic(tmp_path):
    from forge.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("Widgets")
    repo_id = sm.add_project_repo(project_id, str(tmp_path))
    sm.create_epic("ENG-1", project_id=project_id)
    sm.set_epic_project_repo("ENG-1", repo_id)

    with _client() as client:
        response = client.delete(f"/api/projects/{project_id}/repos/{repo_id}")
        assert response.status_code == 400


def test_project_config_set_get_clear_with_fallback():
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        fields = {f["key"]: f for f in client.get(f"/api/projects/{project_id}/config").json()}
        assert fields["jira_base_url"]["overridden"] is False

        set_response = client.put(f"/api/projects/{project_id}/config/jira_base_url", json={"value": "https://acme.atlassian.net"})
        assert set_response.status_code == 200

        fields = {f["key"]: f for f in client.get(f"/api/projects/{project_id}/config").json()}
        assert fields["jira_base_url"]["overridden"] is True
        assert fields["jira_base_url"]["value"] == "https://acme.atlassian.net"

        clear_response = client.delete(f"/api/projects/{project_id}/config/jira_base_url")
        assert clear_response.status_code == 200

        fields = {f["key"]: f for f in client.get(f"/api/projects/{project_id}/config").json()}
        assert fields["jira_base_url"]["overridden"] is False


def test_project_config_rejects_non_overridable_key():
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        response = client.put(f"/api/projects/{project_id}/config/openai_api_key", json={"value": "sk-openai-x"})
        assert response.status_code == 400


# -- ticket filter ---------------------------------------------------------
# The sweep itself is covered in tests/test_jira_filter.py; these check the
# HTTP contract around it. Filters are saved disabled so the background
# sweep started by the app's lifespan never races the assertions.


def _fake_search(tickets):
    """A JiraService stand-in for the preview/sync routes — no live JIRA in
    tests, same rule as everywhere else."""

    class _Fake:
        def __init__(self, project_id=None):
            self.project_id = project_id

        def search_tickets(self, jql, max_results=50):
            return tickets

        def get_ticket_tree(self, root_key, max_tickets=None):
            return [t for t in tickets if t.key == root_key]

    return _Fake


def test_ticket_filter_round_trips_over_http():
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        assert client.get(f"/api/projects/{project_id}/ticket-filter").json()["filter"] is None

        saved = client.put(
            f"/api/projects/{project_id}/ticket-filter",
            json={"enabled": False, "jira_project_key": "ENG", "assignee": "me@example.com", "statuses": ["To Do"]},
        )
        assert saved.status_code == 200
        body = saved.json()["filter"]
        assert body["jql"] == 'project = "ENG" AND assignee = "me@example.com" AND status IN ("To Do") ORDER BY created DESC'

        assert client.get(f"/api/projects/{project_id}/ticket-filter").json()["filter"]["jira_project_key"] == "ENG"

        assert client.delete(f"/api/projects/{project_id}/ticket-filter").json()["cleared"] is True
        assert client.get(f"/api/projects/{project_id}/ticket-filter").json()["filter"] is None


def test_ticket_filter_rejects_an_empty_query():
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        response = client.put(f"/api/projects/{project_id}/ticket-filter", json={"enabled": True})
        assert response.status_code == 400


def test_ticket_filter_preview_uses_the_unsaved_filter_and_writes_nothing(monkeypatch: pytest.MonkeyPatch):
    import forge.services.jira_filter as jira_filter
    from forge.services.jira_service import JiraTicket
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")
    ticket = JiraTicket(key="ENG-1", summary="s", description="", status="To Do", issue_type="Story", priority=None)
    monkeypatch.setattr(jira_filter, "JiraService", _fake_search([ticket]))

    with _client() as client:
        response = client.post(
            f"/api/projects/{project_id}/ticket-filter/preview",
            json={"filter": {"enabled": False, "jira_project_key": "ENG"}},
        )
        assert response.status_code == 200
        assert response.json()["would_sync"] == ["ENG-1"]

    assert StateManager().get_epic("ENG-1") is None
    assert StateManager().get_project_ticket_filter(project_id) is None


def test_ticket_filter_sync_runs_a_disabled_filter_on_demand(monkeypatch: pytest.MonkeyPatch):
    import forge.services.epic_sync as epic_sync
    import forge.services.jira_filter as jira_filter
    from forge.services.jira_service import JiraTicket
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")
    ticket = JiraTicket(key="ENG-1", summary="s", description="", status="To Do", issue_type="Story", priority=None)
    fake = _fake_search([ticket])
    monkeypatch.setattr(jira_filter, "JiraService", fake)
    monkeypatch.setattr(epic_sync, "JiraService", fake)

    with _client() as client:
        client.put(f"/api/projects/{project_id}/ticket-filter", json={"enabled": False, "jira_project_key": "ENG"})
        response = client.post(f"/api/projects/{project_id}/ticket-filter/sync")
        assert response.status_code == 200
        assert response.json()["synced"] == ["ENG-1"]

    assert StateManager().get_epic("ENG-1")["project_id"] == project_id


def test_ticket_filter_routes_404_on_unknown_project():
    with _client() as client:
        assert client.get("/api/projects/999/ticket-filter").status_code == 404
        assert client.post("/api/projects/999/ticket-filter/sync").status_code == 404


def test_ticket_filter_sync_without_a_saved_filter_is_a_400():
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Widgets")

    with _client() as client:
        assert client.post(f"/api/projects/{project_id}/ticket-filter/sync").status_code == 400
