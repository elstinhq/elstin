"""GET /api/stories/{key}/plan — the only way to read a story's planner
output back once the plan-approval gate has cleared, since the plan itself
is never duplicated out of the LangGraph checkpoint into Postgres (see
elstin/db/models/gameplan.py's docstring). Runs the real checkpointer
against testcontainers Postgres, same as tests/test_graph.py, so this
proves the route reads the same state a resume would.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from elstin.orchestration.graph import checkpointer_from_url, compile_graph
from elstin.orchestration.state import initial_state
from elstin.state.manager import StateManager

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("elstin_db", "fake_agents")]


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


@pytest.fixture()
def epic(project_id) -> StateManager:
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="Build the form", kind="dev")
    return sm


async def test_plan_is_null_before_the_story_has_ever_been_dispatched(epic):
    with _client() as client:
        response = client.get("/api/stories/ENG-2/plan")

    assert response.status_code == 200
    assert response.json() == {"story_key": "ENG-2", "plan": None}


async def test_plan_route_reads_the_planner_output_back_from_the_checkpoint(epic, postgres_url):
    thread_id = "ENG-2"
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=True)
        await graph.ainvoke(state, config)

    epic.set_story_thread_id("ENG-2", thread_id)

    with _client() as client:
        response = client.get("/api/stories/ENG-2/plan")

    assert response.status_code == 200
    body = response.json()
    assert body["story_key"] == "ENG-2"
    # fake_run_planner (tests/conftest.py) is the stand-in planner here —
    # asserting its exact shape proves the route round-trips whatever the
    # checkpoint holds rather than reshaping it.
    assert body["plan"] == {"summary": "Add a widget to the cart", "files_to_create": [], "files_to_modify": []}


async def test_plan_route_404s_for_an_unknown_story(epic):
    with _client() as client:
        response = client.get("/api/stories/ENG-404/plan")

    assert response.status_code == 404
