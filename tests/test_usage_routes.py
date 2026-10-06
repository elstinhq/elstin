"""GET /api/epics/{key}/usage and GET /api/stories/{key}/usage — the token/
cost/time rollups the epic detail page and story detail dialog fetch on
demand (elstin/services/usage.py does the actual aggregation; these tests
just confirm the routes wire up and 404 correctly)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("elstin_db", "fake_agents")


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


def test_epic_usage_404_for_unknown_epic():
    with _client() as client:
        response = client.get("/api/epics/NO-SUCH-EPIC/usage")
    assert response.status_code == 404


def test_story_usage_404_for_unknown_story():
    with _client() as client:
        response = client.get("/api/stories/NO-SUCH-STORY/usage")
    assert response.status_code == 404


def test_epic_usage_groups_stories_by_wave(project_id):
    from elstin.services.usage import record_phase_usage
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, kind="dev")
    record_phase_usage("ENG-2", phase="dev", model="gpt-5.1", input_tokens=1000, output_tokens=500, cost_usd=0.1, duration_ms=100)

    with _client() as client:
        response = client.get("/api/epics/ENG-1/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["epic_key"] == "ENG-1"
    assert len(body["levels"]) == 1
    assert body["levels"][0]["wave"] == 0
    assert body["totals"]["cost_usd"] == pytest.approx(0.1)


def test_story_usage_breaks_down_by_phase(project_id):
    from elstin.services.usage import record_phase_usage
    from elstin.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, kind="dev")
    record_phase_usage("ENG-2", phase="planner", model="gpt-5.1", input_tokens=100, cost_usd=0.01, duration_ms=50)
    record_phase_usage("ENG-2", phase="dev", model="gpt-5.1", input_tokens=500, output_tokens=200, cost_usd=0.05, duration_ms=200)

    with _client() as client:
        response = client.get("/api/stories/ENG-2/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["story_key"] == "ENG-2"
    phases = {p["phase"] for p in body["phases"]}
    assert phases == {"planner", "dev"}
    assert body["totals"]["cost_usd"] == pytest.approx(0.06)
