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


def _git_repo_at(path) -> str:
    import subprocess

    path.mkdir(parents=True)
    for args in (["init", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return str(path.resolve())


def test_scanning_a_folder_that_holds_repos_marks_the_ones_already_attached(tmp_path):
    from forge.state.manager import StateManager

    api = _git_repo_at(tmp_path / "workspace" / "api")
    web = _git_repo_at(tmp_path / "workspace" / "web")
    project_id = StateManager().create_project("Shop")
    StateManager().add_project_repo(project_id, api, label="api")

    with _client() as client:
        response = client.post("/api/projects/scan-folder", json={"path": str(tmp_path / "workspace"), "project_id": project_id})
        missing = client.post("/api/projects/scan-folder", json={"path": str(tmp_path / "nope")})

    assert response.status_code == 200
    body = response.json()
    assert body["is_repo"] is False
    assert [(r["name"], r["path"], r["attached"]) for r in body["repos"]] == [("api", api, True), ("web", web, False)]
    assert missing.status_code == 400


def test_browsing_a_folder_over_the_api(tmp_path):
    api = _git_repo_at(tmp_path / "workspace" / "api")

    with _client() as client:
        listing = client.post("/api/projects/browse-folder", json={"path": str(tmp_path / "workspace")})
        missing = client.post("/api/projects/browse-folder", json={"path": str(tmp_path / "nope")})

    assert listing.status_code == 200
    assert listing.json()["entries"] == [{"name": "api", "path": api, "is_repo": True}]
    assert missing.status_code == 400


def test_create_a_project_with_several_repos_picked_from_one_folder(tmp_path):
    api = _git_repo_at(tmp_path / "workspace" / "api")
    web = _git_repo_at(tmp_path / "workspace" / "web")

    with _client() as client:
        response = client.post("/api/projects", json={"name": "Shop", "repo_paths": [api, web, api]})
        assert response.status_code == 200
        repos = client.get(f"/api/projects/{response.json()['id']}/repos").json()

    assert [(r["label"], r["path"]) for r in repos] == [("api", api), ("web", web)]


def test_one_bad_folder_among_several_creates_nothing(tmp_path):
    api = _git_repo_at(tmp_path / "workspace" / "api")
    (tmp_path / "workspace" / "docs").mkdir()

    with _client() as client:
        before = len(client.get("/api/projects").json())
        response = client.post("/api/projects", json={"name": "Shop", "repo_paths": [api, str(tmp_path / "workspace" / "docs")]})
        assert response.status_code == 400 and "not a git repository" in response.json()["detail"]
        both = client.post("/api/projects", json={"name": "Shop", "repo_url": "https://x/y.git", "repo_paths": [api]})
        assert both.status_code == 400
        assert len(client.get("/api/projects").json()) == before


def test_attach_an_existing_folder_and_refuse_it_twice(tmp_path):
    from forge.state.manager import StateManager

    folder = _git_repo_at(tmp_path / "shop-app")
    project_id = StateManager().create_project("Shop")

    with _client() as client:
        response = client.post(f"/api/projects/{project_id}/repos", json={"path": folder})
        assert response.status_code == 200
        repo = response.json()
        assert repo["path"] == folder and repo["label"] == "shop-app" and repo["clone_url"] is None

        again = client.post(f"/api/projects/{project_id}/repos", json={"path": folder + "/"})
        assert again.status_code == 409
        assert "already attached" in again.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"url": "https://example.com/a.git", "path": "/tmp/a"}, {"url": " ", "path": ""}])
def test_add_repo_needs_exactly_one_of_url_or_path(body):
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Shop")
    with _client() as client:
        response = client.post(f"/api/projects/{project_id}/repos", json=body)
    assert response.status_code == 400
    assert "either a repository URL" in response.json()["detail"]


def test_attaching_a_folder_that_is_not_there_explains_docker(tmp_path):
    from forge.state.manager import StateManager

    project_id = StateManager().create_project("Shop")
    with _client() as client:
        response = client.post(f"/api/projects/{project_id}/repos", json={"path": str(tmp_path / "nope")})
    assert response.status_code == 400
    assert "mounted into the app container" in response.json()["detail"]


def test_create_a_project_with_an_attached_folder(tmp_path):
    folder = _git_repo_at(tmp_path / "shop-app")
    with _client() as client:
        response = client.post("/api/projects", json={"name": "Shop", "repo_path": folder})
        assert response.status_code == 200
        assert [repo["path"] for repo in response.json()["repos"]] == [folder]


def test_a_bad_folder_leaves_no_empty_project_behind(tmp_path):
    with _client() as client:
        before = len(client.get("/api/projects").json())
        response = client.post("/api/projects", json={"name": "Shop", "repo_path": str(tmp_path / "nope")})
        assert response.status_code == 400
        assert len(client.get("/api/projects").json()) == before


def test_removing_an_epics_default_folder_moves_the_epic_to_the_next_one(tmp_path):
    """Every epic is given a default folder automatically and there is no
    control to reassign it, so refusing here (as it used to) made the folder
    impossible to remove."""
    from forge.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("Widgets")
    repo_id = sm.add_project_repo(project_id, str(tmp_path / "a"))
    other_id = sm.add_project_repo(project_id, str(tmp_path / "b"))
    sm.create_epic("ENG-1", project_id=project_id)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.set_story_project_repo("ENG-2", repo_id)

    with _client() as client:
        response = client.delete(f"/api/projects/{project_id}/repos/{repo_id}")

    assert response.status_code == 200
    assert response.json() == {"repo_id": repo_id, "removed": True, "epics_moved": ["ENG-1"], "moved_to": other_id}
    assert sm.get_epic("ENG-1")["project_repo_id"] == other_id
    assert sm.get_story("ENG-2")["project_repo_id"] is None  # back to the epic's default


def test_a_folder_with_unfinished_work_in_it_is_not_removed(tmp_path):
    from forge.state.manager import StateManager

    sm = StateManager()
    project_id = sm.create_project("Widgets")
    path = str(tmp_path / "a")
    repo_id = sm.add_project_repo(project_id, path)
    sm.create_epic("ENG-1", project_id=project_id)
    sm.set_epic_project_repo("ENG-1", repo_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    sm.record_story_worktree("ENG-2", repo_label="a", repo_path=path, worktree_path=str(tmp_path / "wt"), branch="feat/ENG-2-x", is_default=True)
    sm.set_story_status("ENG-2", "ready_to_merge")

    with _client() as client:
        refused = client.delete(f"/api/projects/{project_id}/repos/{repo_id}")
        assert refused.status_code == 409 and "ENG-2" in refused.json()["detail"]
        assert sm.get_project_repo(repo_id) is not None and sm.get_epic("ENG-1")["project_repo_id"] == repo_id

        sm.set_story_status("ENG-2", "merged")  # landed: nothing of it left to land from there
        assert client.delete(f"/api/projects/{project_id}/repos/{repo_id}").status_code == 200
    assert sm.get_project_repo(repo_id) is None
    assert sm.get_epic("ENG-1")["project_repo_id"] is None  # no folder left: it waits for one


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
        response = client.put(f"/api/projects/{project_id}/config/github_token", json={"value": "ghp-x"})
        assert response.status_code == 400


# -- ticket filter ---------------------------------------------------------
# The sweep itself is covered in tests/test_jira_filter.py; these check the
# HTTP contract around it. Filters are saved disabled so the background
# sweep started by the app's lifespan never races the assertions.


def _fake_search(tickets):
    """A JiraService stand-in for the preview/sync routes — no live JIRA in
    tests, same rule as everywhere else."""

    class _Fake:
        name = "jira"

        def __init__(self, project_id=None):
            self.project_id = project_id

        def normalize_key(self, raw):
            return raw

        def compile_filter(self, ticket_filter):
            from forge.services.jira_filter import build_jql

            return build_jql(ticket_filter)

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
    fake = _fake_search([ticket])
    monkeypatch.setattr(jira_filter, "get_ticket_source", lambda project_id: fake(project_id))

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
    monkeypatch.setattr(jira_filter, "get_ticket_source", lambda project_id: fake(project_id))
    monkeypatch.setattr(epic_sync, "get_ticket_source", lambda project_id: fake(project_id))

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
