"""Round-trips forge/services/run_activity.py's append_activity/list_activity
against Postgres — confirms the DB-backed rewrite behaves exactly like the
in-memory ring buffer it replaced: per-run isolation, insertion order, an
empty list for a run with no events, and a working `limit`."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("forge_db")


def _seed_story(project_id: int) -> str:
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    return "ENG-2"


def test_append_then_list_round_trips_in_order(project_id):
    from forge.services.run_activity import append_activity, list_activity

    run_id = _seed_story(project_id)
    append_activity(run_id, type="status", phase="planner", title="Planning started")
    append_activity(run_id, type="thinking", phase="planner", title="Planner reasoning", detail="considering the approach")
    append_activity(run_id, type="tool", phase="dev", title="Called tool: read_file", files=["a.py"], meta={"tool": "read_file"})

    events = list_activity(run_id)

    assert [e["title"] for e in events] == ["Planning started", "Planner reasoning", "Called tool: read_file"]
    assert events[0]["type"] == "status"
    assert events[0]["phase"] == "planner"
    assert events[1]["detail"] == "considering the approach"
    assert events[2]["files"] == ["a.py"]
    assert events[2]["meta"] == {"tool": "read_file"}
    assert all(e["ts"] for e in events)  # a real timestamp was stamped


def test_unknown_run_id_returns_empty_list(project_id):
    from forge.services.run_activity import list_activity

    assert list_activity("no-such-run") == []


def test_events_are_isolated_per_run(project_id):
    from forge.services.run_activity import append_activity, list_activity

    run_id = _seed_story(project_id)
    append_activity(run_id, type="status", phase="dev", title="For ENG-2")
    append_activity("ENG-999", type="status", phase="dev", title="For a different run")

    events = list_activity(run_id)

    assert len(events) == 1
    assert events[0]["title"] == "For ENG-2"


def test_limit_keeps_the_most_recent_events_in_chronological_order(project_id):
    from forge.services.run_activity import append_activity, list_activity

    run_id = _seed_story(project_id)
    for i in range(5):
        append_activity(run_id, type="status", phase="dev", title=f"event {i}")

    events = list_activity(run_id, limit=2)

    assert [e["title"] for e in events] == ["event 3", "event 4"]


def test_empty_run_id_is_a_noop():
    from forge.services.run_activity import append_activity

    assert append_activity("", type="status", phase="dev", title="ignored") == {}
