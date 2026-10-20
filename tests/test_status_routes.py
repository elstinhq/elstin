"""GET /api/status — what this process is working on, for the Mac app's window."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from elstin.app import create_app
from elstin.daemon.main import claim_run, release_run

pytestmark = pytest.mark.usefixtures("elstin_db")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_an_idle_process_says_so(client: TestClient):
    assert client.get("/api/status").json() == {"busy": False, "stories": [], "po_sessions": 0, "publishing": 0}


def test_a_story_in_flight_is_listed_until_its_run_ends(client: TestClient):
    assert claim_run("ENG-2") and claim_run("ENG-1")
    try:
        status = client.get("/api/status").json()
        assert status["busy"] is True and status["stories"] == ["ENG-1", "ENG-2"]
    finally:
        release_run("ENG-1")
        release_run("ENG-2")

    assert client.get("/api/status").json()["busy"] is False


def test_a_po_session_or_a_publish_counts_as_work(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    import elstin.api.status_routes as routes

    monkeypatch.setattr(routes, "live_po_session_ids", lambda: frozenset({7}))
    assert client.get("/api/status").json() == {"busy": True, "stories": [], "po_sessions": 1, "publishing": 0}

    monkeypatch.setattr(routes, "live_po_session_ids", lambda: frozenset())
    monkeypatch.setattr(routes, "live_publish_session_ids", lambda: frozenset({3, 4}))
    assert client.get("/api/status").json() == {"busy": True, "stories": [], "po_sessions": 0, "publishing": 2}


def test_it_is_behind_the_access_token_like_everything_else(elstin_db):
    from elstin.api.access_guard import Access

    guarded = TestClient(create_app(Access.for_port(5000, "t" * 43)), base_url="http://127.0.0.1:5000")

    assert guarded.get("/api/status").status_code == 401
    assert guarded.get("/api/status", headers={"authorization": f"Bearer {'t' * 43}"}).status_code == 200
