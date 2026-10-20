"""What is waiting on a person (elstin/services/inbox.py, elstin/api/inbox_routes.py),
against a real SQLite database: one item per gate, the per-project counts, and what
never shows up — sub-tasks, a story elstin will retry itself, an approved plan."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from elstin.services.inbox import build_inbox
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


def _board(sm: StateManager, project_id: int, epic_key: str = "PROJ-1") -> None:
    sm.create_epic(epic_key, project_id=project_id, summary="Saved payment methods")
    for key, status in [("PROJ-2", "merged"), ("PROJ-3", "awaiting_plan_approval"), ("PROJ-4", "pending")]:
        sm.create_story(key, epic_key=epic_key, wave=1, summary=f"Story {key}")
        sm.set_story_status(key, status)


_DRAFT = {
    "mode": "brainstorm",
    "epic": {"summary": "Webhook retries", "description": "", "acceptance_criteria": [], "existing_key": None, "suggested_description": None},
    "stories": [], "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [],
}


def _kinds(inbox: dict) -> list[str]:
    return sorted(item["kind"] for item in inbox["items"])


def test_a_plan_waiting_for_approval_is_an_item():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    _board(sm, pid)

    inbox = build_inbox(sm)

    [item] = inbox["items"]
    assert item["kind"] == "plan_approval"
    assert item["story_key"] == "PROJ-3"
    assert item["epic_key"] == "PROJ-1"
    assert item["title"] == "Story PROJ-3"
    assert inbox["projects"][0]["counts"]["needs_you"] == 1


def test_an_approved_plan_is_not_waiting_on_anyone():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    _board(sm, pid)
    sm.set_story_gate_approved("PROJ-3")

    assert build_inbox(sm)["items"] == []


def test_questions_replace_plan_approval():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    _board(sm, pid)
    sm.set_story_clarification_questions("PROJ-3", [{"id": "q1", "question": "Which?", "options": []}])

    [item] = build_inbox(sm)["items"]
    assert item["kind"] == "questions"
    assert item["detail"] == "1 question"


def test_every_gate_kind_is_listed():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    _board(sm, pid)
    sm.set_story_status("PROJ-4", "failed", failure_stage="run")
    sm.set_story_external_pending("PROJ-2", {"summary": "New", "hash": "x", "reasons": ["summary changed"]})
    sm.set_epic_pr_approval("PROJ-1", "awaiting")
    sm.add_lesson_candidate(pid, "Run the Flyway migration check before the integration tests.")

    inbox = build_inbox(sm)

    assert _kinds(inbox) == ["external_change", "failed", "lesson", "plan_approval", "pr_approval"]
    pr = next(i for i in inbox["items"] if i["kind"] == "pr_approval")
    assert pr["detail"] == "1/3 merged"
    lesson = next(i for i in inbox["items"] if i["kind"] == "lesson")
    assert isinstance(lesson["lesson_id"], int)
    counts = inbox["projects"][0]["counts"]
    assert counts == {"needs_you": 5, "epics": 1, "repos": 0, "lessons_proposed": 1, "drafts": 0}


def test_a_failed_story_elstin_will_retry_itself_is_not_listed():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    _board(sm, pid)
    sm.set_story_gate_approved("PROJ-3")
    sm.set_story_status(
        "PROJ-4", "failed", parked_reason="usage_limit", retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert build_inbox(sm)["items"] == []


def test_sub_tasks_are_never_listed():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    _board(sm, pid)
    sm.set_story_gate_approved("PROJ-3")
    sm.create_story("PROJ-9", epic_key="PROJ-1", wave=1, parent_key="PROJ-3", status="skipped")
    sm.set_story_status("PROJ-9", "failed")

    assert build_inbox(sm)["items"] == []


def test_a_draft_needing_answers_is_listed():
    sm = StateManager()
    pid = sm.create_project("Checkout")
    session = sm.create_po_session(pid, brief="Plan retries for failed webhooks", mode="brainstorm")
    sm.claim_next_po_session()
    sm.finish_po_turn(
        session["id"], draft=_DRAFT, questions=[{"id": "q1", "question": "Which?", "options": []}], reply="?", cost_usd=0.0,
    )

    inbox = build_inbox(sm)

    drafts = [i for i in inbox["items"] if i["kind"] == "po_draft"]
    assert len(drafts) == 1
    assert drafts[0]["session_id"] == session["id"]
    assert inbox["projects"][0]["counts"]["drafts"] == 1


def test_project_route_is_scoped_and_404s_for_an_unknown_project():
    sm = StateManager()
    one = sm.create_project("Checkout")
    two = sm.create_project("Mobile")
    _board(sm, one)
    sm.add_lesson_candidate(two, "Pin the JDK to 21 for the mobile backend build.")
    client = _client()

    scoped = client.get(f"/api/projects/{one}/inbox").json()
    assert [p["id"] for p in scoped["projects"]] == [one]
    assert _kinds(scoped) == ["plan_approval"]

    everything = client.get("/api/inbox").json()
    assert {p["id"] for p in everything["projects"]} == {one, two}
    assert _kinds(everything) == ["lesson", "plan_approval"]

    assert client.get("/api/projects/999999/inbox").status_code == 404
