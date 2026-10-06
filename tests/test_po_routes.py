"""PO sessions over HTTP (elstin/api/po_routes.py), against a real Postgres. The
epic lookup is faked at `inspect_epic`; the runner is not involved — these tests
move sessions through their states directly, the way the daemon would."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import elstin.api.po_routes as po_routes
from elstin.services.jira_service import JiraTicket
from elstin.services.po_sessions import PoModeError, PoSourceUnavailable
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


def _story(ref="s1", **overrides):
    story = {
        "ref": ref, "summary": f"Do thing {ref}", "description": f"Why {ref} exists.",
        "acceptance_criteria": [f"{ref} behaves"], "issue_type": "Story", "kind": "dev", "labels": [],
        "depends_on": [], "subtasks": [], "evidence": [], "rationale": "", "size": "M",
    }
    story.update(overrides)
    return story


def _stored_draft(**overrides):
    draft = {
        "mode": "brainstorm",
        "epic": {
            "summary": "Retry payments", "description": "Retry failed payments.", "acceptance_criteria": ["Retries happen"],
            "existing_key": None, "suggested_description": None,
        },
        "stories": [_story("s1"), _story("s2", depends_on=["s1"])],
        "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [],
    }
    draft.update(overrides)
    return draft


def _gap_draft(**overrides):
    draft = _stored_draft(mode="gap", **overrides)
    draft["epic"] = {**draft["epic"], "existing_key": "PAY-1"}
    return draft


def _ready_session(project_id, *, mode="brainstorm", draft=None, questions=None, epic_key=None) -> dict:
    sm = StateManager()
    session = sm.create_po_session(project_id, brief="Plan retries", mode=mode, epic_key=epic_key)
    sm.claim_next_po_session()
    return sm.finish_po_turn(
        session["id"], draft=draft or _stored_draft(mode=mode), questions=questions or [], reply="Here", cost_usd=0.1,
    )


def _ticket(key, issue_type="Story", summary=None):
    return JiraTicket(key=key, summary=summary or key, description="", status="To Do", issue_type=issue_type, priority=None)


# -- starting ------------------------------------------------------------------------------------------------


def test_a_brainstorm_starts_queued_and_the_server_picked_the_mode(project_id):
    StateManager().set_project_ticket_filter(project_id, enabled=False, jira_project_key="pay")
    response = _client().post(f"/api/projects/{project_id}/po-sessions", json={"brief": "  Plan payment retries  "})
    assert response.status_code == 201
    body = response.json()
    assert (body["mode"], body["status"], body["epic_key"], body["brief"]) == ("brainstorm", "queued", None, "Plan payment retries")
    assert body["jira_project_key"] == "PAY"  # from the project's ticket filter
    assert body["draft"] is None and body["problems"] == [] and body["counts"] == {"epics": 1, "stories": 0, "subtasks": 0, "links": 0}
    assert "context" not in body


def test_the_brief_is_required_and_bounded(project_id):
    client = _client()
    assert client.post(f"/api/projects/{project_id}/po-sessions", json={"brief": "   "}).status_code == 422
    too_long = client.post(f"/api/projects/{project_id}/po-sessions", json={"brief": "x" * 20_001})
    assert too_long.status_code == 422 and "20000" in too_long.json()["detail"]
    assert client.post("/api/projects/999999/po-sessions", json={"brief": "x"}).status_code == 404


def test_an_epic_picks_split_or_gap_and_records_what_a_draft_is_checked_against(project_id, monkeypatch):
    tree = [_ticket("PAY-1", "Epic", "Retry payments"), _ticket("PAY-2", summary="Backoff")]
    monkeypatch.setattr(po_routes, "inspect_epic", lambda pid, raw: ("PAY-1", "gap", tree, "jira"))
    response = _client().post(f"/api/projects/{project_id}/po-sessions", json={"brief": "Where does it stand?", "epic_key": " PAY-1 "})
    assert response.status_code == 201
    body = response.json()
    assert (body["mode"], body["epic_key"], body["jira_project_key"]) == ("gap", "PAY-1", "PAY")
    assert body["epic_summary"] == "Retry payments" and body["existing_stories"] == [{"key": "PAY-2", "summary": "Backoff"}]


def test_a_typed_jira_project_key_wins(project_id, monkeypatch):
    response = _client().post(
        f"/api/projects/{project_id}/po-sessions", json={"brief": "x", "jira_project_key": "ops"},
    )
    assert response.json()["jira_project_key"] == "OPS"


@pytest.mark.parametrize("error,status", [(PoModeError("PAY-9 is a Story, not an Epic."), 422), (PoSourceUnavailable("JIRA is down"), 502)])
def test_an_epic_that_cannot_be_used_is_refused_with_the_reason(project_id, monkeypatch, error, status):
    def inspect(pid, raw):
        raise error

    monkeypatch.setattr(po_routes, "inspect_epic", inspect)
    response = _client().post(f"/api/projects/{project_id}/po-sessions", json={"brief": "x", "epic_key": "PAY-9"})
    assert response.status_code == status and response.json()["detail"] == str(error)
    assert StateManager().list_po_sessions(project_id) == []


def test_an_epic_that_is_on_another_projects_board_is_refused(project_id, monkeypatch):
    sm = StateManager()
    other = sm.create_project("Other")
    sm.create_epic("PAY-1", project_id=other, levels=[])
    monkeypatch.setattr(po_routes, "inspect_epic", lambda pid, raw: ("PAY-1", "split", [_ticket("PAY-1", "Epic")], "jira"))
    response = _client().post(f"/api/projects/{project_id}/po-sessions", json={"brief": "x", "epic_key": "PAY-1"})
    assert response.status_code == 409 and "different project" in response.json()["detail"]


def test_the_mode_can_be_previewed_without_starting_anything(project_id, monkeypatch):
    client = _client()
    assert client.get(f"/api/projects/{project_id}/po-mode").json() == {
        "mode": "brainstorm", "epic_key": None, "epic_summary": None, "stories": 0,
    }
    tree = [_ticket("PAY-1", "Epic", "Retry payments"), _ticket("PAY-2")]
    monkeypatch.setattr(po_routes, "inspect_epic", lambda pid, raw: ("PAY-1", "gap", tree, "jira"))
    assert client.get(f"/api/projects/{project_id}/po-mode", params={"epic_key": "PAY-1"}).json() == {
        "mode": "gap", "epic_key": "PAY-1", "epic_summary": "Retry payments", "stories": 1,
    }

    def refuse(pid, raw):
        raise PoSourceUnavailable("down")

    monkeypatch.setattr(po_routes, "inspect_epic", refuse)
    assert client.get(f"/api/projects/{project_id}/po-mode", params={"epic_key": "PAY-1"}).status_code == 502
    assert client.get("/api/projects/999999/po-mode").status_code == 404
    assert StateManager().list_po_sessions(project_id) == []


# -- reading --------------------------------------------------------------------------------------------------


def test_sessions_are_listed_as_summaries(project_id):
    session = _ready_session(project_id)
    StateManager().create_po_session(project_id, brief="b " * 200, mode="brainstorm")
    rows = _client().get(f"/api/projects/{project_id}/po-sessions").json()
    assert [r["status"] for r in rows] == ["queued", "ready"]
    ready = rows[1]
    assert (ready["id"], ready["title"], ready["stories"], ready["cost_usd"]) == (session["id"], "Retry payments", 2, 0.1)
    assert rows[0]["brief"].endswith("…") and len(rows[0]["brief"]) <= 201 and rows[0]["title"] is None
    assert "draft" not in ready

    StateManager().discard_po_session(session["id"])
    assert [r["id"] for r in _client().get(f"/api/projects/{project_id}/po-sessions").json()] == [rows[0]["id"]]
    assert len(_client().get(f"/api/projects/{project_id}/po-sessions", params={"include_discarded": True}).json()) == 2
    assert _client().get("/api/projects/999999/po-sessions").status_code == 404


def test_a_session_shows_its_draft_its_problems_and_its_conversation(project_id):
    draft = _stored_draft(stories=[_story("s1", acceptance_criteria=[]), _story("s2", summary="Validate end-to-end flow", description="Regression sign-off")])
    session = _ready_session(project_id, draft=draft)
    body = _client().get(f"/api/po-sessions/{session['id']}").json()

    assert body["status"] == "ready" and body["draft_version"] == 1 and len(body["draft"]["stories"]) == 2
    assert [p["kind"] for p in body["problems"]] == ["no_criteria"]
    assert body["problems"][0]["where"] == "stories[0].acceptance_criteria"
    assert [w["kind"] for w in body["warnings"]] == ["kind_mismatch"]  # elstin would call the second one QA
    assert body["counts"] == {"epics": 1, "stories": 2, "subtasks": 0, "links": 0}
    assert [(m["role"], m["text"]) for m in body["messages"]] == [("person", "Plan retries"), ("agent", "Here")]
    assert _client().get("/api/po-sessions/999999").status_code == 404


# -- editing ----------------------------------------------------------------------------------------------------


def test_a_persons_edit_is_saved_as_a_new_version_and_remembered(project_id):
    session = _ready_session(project_id)
    client = _client()
    edited = _stored_draft()
    edited["stories"][0]["summary"] = "A person's own wording"

    response = client.put(f"/api/po-sessions/{session['id']}/draft", json={"draft": edited, "version": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["draft_version"] == 2 and body["edited_by_person"] is True
    assert body["person_edits"] == ["stories.s1.summary"]
    assert body["draft"]["stories"][0]["summary"] == "A person's own wording"


def test_an_edit_over_a_stale_version_is_refused_and_loses_nothing(project_id):
    session = _ready_session(project_id)
    client = _client()
    first = _stored_draft()
    first["epic"]["summary"] = "First"
    assert client.put(f"/api/po-sessions/{session['id']}/draft", json={"draft": first, "version": 1}).status_code == 200

    stale = _stored_draft()
    stale["epic"]["summary"] = "Stale"
    response = client.put(f"/api/po-sessions/{session['id']}/draft", json={"draft": stale, "version": 1})
    assert response.status_code == 409 and "changed since you opened it" in response.json()["detail"]
    assert StateManager().get_po_session(session["id"])["draft"]["epic"]["summary"] == "First"


def test_saving_what_is_already_saved_changes_nothing(project_id):
    session = _ready_session(project_id)
    response = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": _stored_draft(), "version": 1})
    assert response.status_code == 200 and response.json()["draft_version"] == 1
    assert response.json()["edited_by_person"] is False


def test_a_draft_that_does_not_match_the_schema_is_refused_with_why(project_id):
    session = _ready_session(project_id)
    bad = _stored_draft()
    bad["stories"][0]["size"] = "XL"
    bad["stories"][0]["colour"] = "red"
    response = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": bad, "version": 1})
    assert response.status_code == 422
    detail = " ".join(response.json()["detail"])
    assert "size" in detail and "colour" in detail
    assert StateManager().get_po_session(session["id"])["draft_version"] == 1


def test_a_structural_problem_is_saved_and_shown_not_refused(project_id):
    """A person mid-edit has to be able to keep their work."""
    session = _ready_session(project_id)
    draft = _stored_draft()
    draft["stories"][1]["depends_on"] = ["s1", "s9"]
    draft["stories"][0]["acceptance_criteria"] = []
    body = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": draft, "version": 1}).json()
    assert body["draft_version"] == 2
    assert {p["kind"] for p in body["problems"]} == {"no_criteria", "unknown_dependency"}


def test_an_unchanged_evidence_item_keeps_its_tick_and_a_clients_own_tick_is_ignored(project_id):
    marked = _stored_draft(stories=[_story("s1", evidence=[
        {"source": "code", "ref": "a.py", "quote": "one", "verified": True},
        {"source": "code", "ref": "b.py", "quote": "two", "verified": False},
    ])])
    session = _ready_session(project_id, draft=marked)
    edited = _stored_draft(stories=[_story("s1", summary="Edited", evidence=[
        {"source": "code", "ref": "a.py", "quote": "one"},
        {"source": "code", "ref": "b.py", "quote": "two", "verified": True},  # a client cannot vouch for itself
        {"source": "code", "ref": "c.py", "quote": "new", "verified": True},
    ])])
    body = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": edited, "version": 1}).json()
    assert [e.get("verified") for e in body["draft"]["stories"][0]["evidence"]] == [True, False, None]
    # Adding an evidence item is an edit, and so is the new summary; the marks themselves are not.
    assert body["person_edits"] == ["stories.s1.evidence", "stories.s1.summary"]


def test_a_draft_cannot_be_edited_while_the_agent_works(project_id):
    session = _ready_session(project_id)
    sm = StateManager()
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    sm.claim_next_po_session()
    changed = _stored_draft()
    changed["epic"]["summary"] = "Edited while the agent works"
    response = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": changed, "version": 1})
    assert response.status_code == 409 and "while the session is running" in response.json()["detail"]
    assert _client().put("/api/po-sessions/999999/draft", json={"draft": changed, "version": 1}).status_code == 404


# -- export ----------------------------------------------------------------------------------------------------------


def test_the_draft_exports_as_markdown_or_json(project_id):
    session = _ready_session(project_id)
    client = _client()
    markdown = client.get(f"/api/po-sessions/{session['id']}/export")
    assert markdown.status_code == 200 and markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text.startswith("# Retry payments") and "### s1: Do thing s1" in markdown.text

    exported = client.get(f"/api/po-sessions/{session['id']}/export", params={"format": "json"}).json()
    assert exported["mode"] == "brainstorm" and exported["draft"]["stories"][0]["ref"] == "s1"
    assert client.get(f"/api/po-sessions/{session['id']}/export", params={"format": "docx"}).status_code == 422


def test_there_is_nothing_to_export_before_the_first_draft(project_id):
    session = StateManager().create_po_session(project_id, brief="x", mode="brainstorm")
    assert _client().get(f"/api/po-sessions/{session['id']}/export").status_code == 409
    assert _client().get("/api/po-sessions/999999/export").status_code == 404


# -- ending ----------------------------------------------------------------------------------------------------------------


def test_cancelling_discards_the_session(project_id):
    session = StateManager().create_po_session(project_id, brief="x", mode="brainstorm")
    response = _client().post(f"/api/po-sessions/{session['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "discarded"
    assert StateManager().claim_next_po_session() is None  # no longer queued


def test_a_session_being_published_or_published_cannot_be_cancelled(project_id):
    from elstin.db.engine import session_scope
    from elstin.db.models import PoSession

    session = _ready_session(project_id)
    for status in ("publishing", "published"):
        with session_scope() as db:
            db.get(PoSession, session["id"]).status = status
        response = _client().post(f"/api/po-sessions/{session['id']}/cancel")
        assert response.status_code == 409 and status in response.json()["detail"]
    assert _client().post("/api/po-sessions/999999/cancel").status_code == 409


def test_deleting_removes_the_session_and_what_it_left(project_id):
    from elstin.services.run_activity import append_activity, list_activity

    session = _ready_session(project_id)
    append_activity(f"po-{session['id']}", type="status", phase="po", title="Drafting")
    response = _client().delete(f"/api/po-sessions/{session['id']}")
    assert response.status_code == 200 and response.json()["removed"]["sessions"] == 1
    assert StateManager().get_po_session(session["id"]) is None and list_activity(f"po-{session['id']}") == []
    assert _client().delete(f"/api/po-sessions/{session['id']}").status_code == 404


def test_a_published_session_can_be_deleted_but_one_being_published_cannot(project_id):
    from elstin.db.engine import session_scope
    from elstin.db.models import PoSession

    client = _client()
    session = _ready_session(project_id)
    with session_scope() as db:
        db.get(PoSession, session["id"]).status = "publishing"
    assert client.delete(f"/api/po-sessions/{session['id']}").status_code == 409
    with session_scope() as db:
        db.get(PoSession, session["id"]).status = "published"
    assert client.delete(f"/api/po-sessions/{session['id']}").status_code == 200


def test_the_activity_stream_needs_a_session(project_id):
    assert _client().get("/api/po-sessions/999999/stream").status_code == 404


# -- talking to the agent --------------------------------------------------------------------------------------------


def test_a_message_to_a_finished_draft_queues_a_revision(project_id):
    session = _ready_session(project_id)
    response = _client().post(f"/api/po-sessions/{session['id']}/messages", json={"message": "  Split s2 in two.  "})
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["delivery"]) == ("queued", "queued")
    assert [(m["role"], m["text"], m["delivered"]) for m in StateManager().list_po_messages(session["id"])][-1] == (
        "person", "Split s2 in two.", False,
    )


def test_a_message_to_a_running_turn_is_delivered_live_and_queues_nothing(project_id):
    session = _ready_session(project_id)
    sm = StateManager()
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    sm.claim_next_po_session()
    response = _client().post(f"/api/po-sessions/{session['id']}/messages", json={"message": "Use Stripe."})
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["delivery"]) == ("running", "live")
    # The turn reads it the way the loop's hook does.
    assert [m["text"] for m in sm.take_po_messages(session["id"])] == ["Use Stripe."]


def test_a_message_to_a_queued_session_waits_for_the_turn_already_coming(project_id):
    session = StateManager().create_po_session(project_id, brief="x", mode="brainstorm")
    body = _client().post(f"/api/po-sessions/{session['id']}/messages", json={"message": "Also refunds"}).json()
    assert (body["status"], body["delivery"]) == ("queued", "queued")


def test_a_failed_session_is_retried_by_a_message(project_id):
    sm = StateManager()
    session = sm.create_po_session(project_id, brief="x", mode="brainstorm")
    sm.claim_next_po_session()
    sm.fail_po_session(session["id"], "provider down")
    body = _client().post(f"/api/po-sessions/{session['id']}/messages", json={"message": "try again"}).json()
    assert body["status"] == "queued" and body["error"] is None


def test_messages_are_checked(project_id):
    session = _ready_session(project_id)
    client = _client()
    assert client.post(f"/api/po-sessions/{session['id']}/messages", json={"message": "   "}).status_code == 422
    assert client.post(f"/api/po-sessions/{session['id']}/messages", json={"message": "x" * 4001}).status_code == 422
    assert client.post("/api/po-sessions/999999/messages", json={"message": "hi"}).status_code == 404

    for status in ("discarded", "publishing", "published"):
        _set_status(session["id"], status)
        response = client.post(f"/api/po-sessions/{session['id']}/messages", json={"message": "hi"})
        assert response.status_code == 409 and status in response.json()["detail"]


def test_a_person_cannot_pile_up_unread_messages_without_limit(project_id):
    session = StateManager().create_po_session(project_id, brief="x", mode="brainstorm")
    client = _client()
    for n in range(10):
        assert client.post(f"/api/po-sessions/{session['id']}/messages", json={"message": f"m{n}"}).status_code == 200
    response = client.post(f"/api/po-sessions/{session['id']}/messages", json={"message": "one more"})
    assert response.status_code == 409 and "already waiting" in response.json()["detail"]


def _set_status(session_id: int, status: str) -> None:
    from elstin.db.engine import session_scope
    from elstin.db.models import PoSession

    with session_scope() as db:
        db.get(PoSession, session_id).status = status


QUESTIONS = [
    {"id": "q1", "question": "Which provider?", "context": None, "options": [
        {"id": "A", "label": "Stripe", "description": "the default"}, {"id": "B", "label": "Adyen", "description": None},
    ]},
    {"id": "q2", "question": "Notify by email or SMS?", "context": None, "options": []},
]


def test_answering_the_questions_queues_a_revision_that_uses_them(project_id):
    session = _ready_session(project_id, questions=QUESTIONS)
    response = _client().post(f"/api/po-sessions/{session['id']}/answer", json={"answers": [
        {"question_id": "q1", "option_id": "A", "text": "EU accounts only"}, {"question_id": "q2", "text": "Email"},
    ]})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["answers"] == [
        {"id": "q1", "question": "Which provider?", "answer": "Stripe — the default (EU accounts only)"},
        {"id": "q2", "question": "Notify by email or SMS?", "answer": "Email"},
    ]


def test_every_question_must_be_answered_with_something_real(project_id):
    session = _ready_session(project_id, questions=QUESTIONS)
    client = _client()
    url = f"/api/po-sessions/{session['id']}/answer"

    missing = client.post(url, json={"answers": [{"question_id": "q1", "option_id": "A"}]})
    assert missing.status_code == 422 and "still open: q2" in missing.json()["detail"]
    nothing = client.post(url, json={"answers": [{"question_id": "q1", "option_id": "A"}, {"question_id": "q2", "text": "  "}]})
    assert nothing.status_code == 422 and "Choose an option or write an answer for question q2" in nothing.json()["detail"]
    bad_option = client.post(url, json={"answers": [{"question_id": "q1", "option_id": "Z"}, {"question_id": "q2", "text": "x"}]})
    assert bad_option.status_code == 422 and "not an option" in bad_option.json()["detail"]
    stranger = client.post(url, json={"answers": [{"question_id": "q9", "text": "x"}]})
    assert stranger.status_code == 422 and "q9" in stranger.json()["detail"]
    assert StateManager().get_po_session(session["id"])["status"] == "awaiting_input"


def test_there_is_nothing_to_answer_when_the_agent_asked_nothing(project_id):
    session = _ready_session(project_id)
    response = _client().post(f"/api/po-sessions/{session['id']}/answer", json={"answers": []})
    assert response.status_code == 409 and "no open questions" in response.json()["detail"]
    assert _client().post("/api/po-sessions/999999/answer", json={"answers": []}).status_code == 404


# -- publishing -------------------------------------------------------------------------------------------------------


def _jira_configured(*, create: bool = True, write: bool = False, comment: bool = False) -> None:
    from elstin.config.store import set_config

    set_config("jira_base_url", "https://acme.atlassian.net")
    set_config("jira_email", "me@acme.test")
    set_config("jira_api_token", "token")
    # The PO's two switches and the framework's are separate settings: each is set on its own.
    set_config("po_jira_create_enabled", "true" if create else "false")
    set_config("po_jira_comment_enabled", "true" if comment else "false")
    set_config("jira_write_enabled", "true" if write else "false")


def _publishable(project_id, **kwargs) -> dict:
    session = _ready_session(project_id, **kwargs)
    StateManager().set_po_jira_project_key(session["id"], "PAY")
    return StateManager().get_po_session(session["id"])


def _publish_state(session_id: int) -> dict:
    return _client().get(f"/api/po-sessions/{session_id}").json()["publish"]


def test_publishing_follows_the_po_switches_and_not_the_frameworks_write_switch(project_id):
    """`jira_write_enabled` (transitions, QA comments) can be off and a draft still publishes;
    on, with the PO's switch off, it does not. Same for the suggestion comments."""
    session = _publishable(project_id)

    _jira_configured(create=True, write=False, comment=True)
    assert _publish_state(session["id"]) == {"allowed": True, "reason": None, "jira_project_key": "PAY", "comments_allowed": True}

    _jira_configured(create=False, write=True, comment=False)
    state = _publish_state(session["id"])
    assert state["allowed"] is False and "po_jira_create_enabled" in state["reason"] and state["comments_allowed"] is False


def test_a_ready_draft_can_be_published_when_jira_creating_is_on(project_id):
    _jira_configured()
    session = _publishable(project_id)
    state = _publish_state(session["id"])
    assert state == {"allowed": True, "reason": None, "jira_project_key": "PAY", "comments_allowed": False}


@pytest.mark.parametrize("setup,expected", [
    ("create_off", "po_jira_create_enabled"),
    ("unconfigured", "JIRA is not configured"),
    ("github", "only built for JIRA"),
    ("no_key", "which JIRA project"),
    ("questions", "open questions"),
    ("running", "is running"),
    ("problem", "Fix 1 problem in the draft first"),
    ("empty", "proposes nothing to create"),
])
def test_why_a_draft_cannot_be_published_is_said_in_words(project_id, setup, expected):
    sm = StateManager()
    _jira_configured(create=setup != "create_off")
    kwargs: dict = {}
    if setup == "unconfigured":
        from elstin.config.store import set_config

        set_config("jira_api_token", "")
    if setup == "problem":
        kwargs["draft"] = _stored_draft(stories=[_story("s1", acceptance_criteria=[]), _story("s2")])
    if setup == "empty":
        # Gap mode may find nothing new to propose — and then there is nothing to publish.
        kwargs.update(mode="gap", epic_key="PAY-1", draft=_gap_draft(stories=[], existing=[]))
    if setup == "questions":
        kwargs["questions"] = QUESTIONS
    session = _publishable(project_id, **kwargs)
    if setup == "github":
        from elstin.db.engine import session_scope
        from elstin.db.models import Project

        with session_scope() as db:
            db.get(Project, project_id).ticket_source = "github"
    if setup == "no_key":
        from elstin.db.engine import session_scope
        from elstin.db.models import PoSession

        with session_scope() as db:
            db.get(PoSession, session["id"]).jira_project_key = None
    if setup == "running":
        sm.queue_po_turn(session["id"], from_statuses=("ready",))
        sm.claim_next_po_session()
    state = _publish_state(session["id"])
    assert state["allowed"] is False and expected in state["reason"], state


def test_publish_refuses_with_the_reason(project_id):
    _jira_configured(create=False)
    session = _publishable(project_id)
    response = _client().post(f"/api/po-sessions/{session['id']}/publish")
    assert response.status_code == 409 and "po_jira_create_enabled" in response.json()["detail"]
    assert StateManager().get_po_session(session["id"])["status"] == "ready"
    assert _client().post("/api/po-sessions/999999/publish").status_code == 404


def test_publish_starts_in_the_background_and_freezes_the_draft(project_id, monkeypatch):
    _jira_configured()
    started: list[int] = []

    async def fake_start(sm, session_id):
        started.append(session_id)

    monkeypatch.setattr(po_routes, "start_publish", fake_start)
    session = _publishable(project_id)
    response = _client().post(f"/api/po-sessions/{session['id']}/publish")
    assert response.status_code == 200 and response.json()["status"] == "publishing"
    assert started == [session["id"]]
    # Pressed twice: refused, started once.
    again = _client().post(f"/api/po-sessions/{session['id']}/publish")
    assert again.status_code == 409 and started == [session["id"]]
    # The draft is frozen while it publishes.
    changed = _stored_draft()
    changed["epic"]["summary"] = "Edited mid-publish"
    assert _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": changed, "version": 1}).status_code == 409


def _story_publish(session_id: int) -> dict:
    return _client().get(f"/api/po-sessions/{session_id}").json()["story_publish"]


def test_each_story_says_whether_it_can_be_published_on_its_own_and_why_not(project_id):
    _jira_configured()
    session = _publishable(project_id)  # s2 depends on s1
    states = _story_publish(session["id"])
    assert states["s1"] == {"key": None, "allowed": True, "reason": None, "counts": {"epics": 1, "stories": 1, "subtasks": 0, "links": 0}}
    assert states["s2"]["allowed"] is False and "depends on s1, which is not in JIRA yet" in states["s2"]["reason"]

    _jira_configured(create=False)
    assert "po_jira_create_enabled" in _story_publish(session["id"])["s1"]["reason"]


def test_a_stories_own_problem_holds_back_that_story_and_not_the_others(project_id):
    _jira_configured()
    session = _publishable(project_id, draft=_stored_draft(stories=[_story("s1"), _story("s2", acceptance_criteria=[])]))
    states = _story_publish(session["id"])
    assert states["s1"]["allowed"] is True  # s2's problem is s2's
    assert states["s2"]["allowed"] is False and "Fix 1 problem in s2 first" in states["s2"]["reason"]
    # …while publishing everything still waits for the whole draft to be right.
    assert "Fix 1 problem in the draft first" in _publish_state(session["id"])["reason"]


def test_publishing_one_story_starts_only_that_story_and_names_what_is_refused(project_id, monkeypatch):
    _jira_configured()
    started: list[tuple] = []

    async def fake_start(sm, session_id, refs=None):
        started.append((session_id, refs))

    monkeypatch.setattr(po_routes, "start_publish", fake_start)
    session = _publishable(project_id)

    blocked = _client().post(f"/api/po-sessions/{session['id']}/publish", json={"refs": ["s2"]})
    assert blocked.status_code == 409 and "publish s1 first" in blocked.json()["detail"]
    assert _client().post(f"/api/po-sessions/{session['id']}/publish", json={"refs": ["s9"]}).status_code == 409
    assert _client().post(f"/api/po-sessions/{session['id']}/publish", json={"refs": []}).status_code == 422
    assert started == [] and StateManager().get_po_session(session["id"])["status"] == "ready"

    response = _client().post(f"/api/po-sessions/{session['id']}/publish", json={"refs": ["s1", "s1"]})
    assert response.status_code == 200 and response.json()["status"] == "publishing"
    assert started == [(session["id"], ["s1"])]


def test_a_story_already_in_jira_cannot_be_published_again_or_edited(project_id):
    _jira_configured()
    session = _publishable(project_id)
    sm = StateManager()
    sm.record_po_publish(session["id"], created={"epic": "PAY-1", "s1": "PAY-2"}, epic_key="PAY-1")

    body = _client().get(f"/api/po-sessions/{session['id']}").json()
    assert body["story_publish"]["s1"]["key"] == "PAY-2" and body["story_publish"]["s1"]["allowed"] is False
    assert body["story_publish"]["s2"]["allowed"] is True  # its blocker is in JIRA now
    assert body["counts"]["epics"] == 0 and body["counts"]["stories"] == 1  # what is left, not the whole draft
    again = _client().post(f"/api/po-sessions/{session['id']}/publish", json={"refs": ["s1"]})
    assert again.status_code == 409 and "already in JIRA as PAY-2" in again.json()["detail"]

    edited = _stored_draft()
    edited["stories"][0]["summary"] = "Reworded after it was published"
    refused = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": edited, "version": body["draft_version"]})
    assert refused.status_code == 409 and "s1 is already in JIRA as PAY-2" in refused.json()["detail"]

    elsewhere = _stored_draft()
    elsewhere["stories"][1]["summary"] = "Reworded, and not yet published"
    saved = _client().put(f"/api/po-sessions/{session['id']}/draft", json={"draft": elsewhere, "version": body["draft_version"]})
    assert saved.status_code == 200


def test_a_problem_in_a_story_that_is_already_in_jira_holds_nothing_back(project_id):
    _jira_configured()
    # s1 has no criteria — and is in JIRA, so there is nothing left to fix in it.
    session = _publishable(project_id, draft=_stored_draft(stories=[_story("s1", acceptance_criteria=[]), _story("s2")]))
    assert "Fix 1 problem" in _publish_state(session["id"])["reason"]
    StateManager().record_po_publish(session["id"], created={"epic": "PAY-1", "s1": "PAY-2"})
    assert _publish_state(session["id"])["allowed"] is True
    assert _story_publish(session["id"])["s2"]["allowed"] is True


def test_the_jira_project_can_be_typed_at_publish_when_the_session_did_not_know_it(project_id, monkeypatch):
    _jira_configured()

    async def fake_start(sm, session_id):
        pass

    monkeypatch.setattr(po_routes, "start_publish", fake_start)
    session = _ready_session(project_id)  # no JIRA project key resolved
    assert "which JIRA project" in _publish_state(session["id"])["reason"]
    response = _client().post(f"/api/po-sessions/{session['id']}/publish", json={"jira_project_key": " ops "})
    assert response.status_code == 200 and response.json()["jira_project_key"] == "OPS"


def test_questions_can_be_dismissed_so_the_draft_can_stand_on_the_agents_guesses(project_id):
    session = _ready_session(project_id, questions=QUESTIONS)
    body = _client().post(f"/api/po-sessions/{session['id']}/questions/dismiss").json()
    assert body["status"] == "ready" and body["questions"] == []
    again = _client().post(f"/api/po-sessions/{session['id']}/questions/dismiss")
    assert again.status_code == 409
    assert _client().post("/api/po-sessions/999999/questions/dismiss").status_code == 404


def test_a_suggested_edit_is_posted_on_a_persons_say_behind_the_comment_switch(project_id, monkeypatch):
    import elstin.services.backlog_publisher as publisher

    posted: list[tuple[str, str]] = []

    class Source:
        name = "jira"

        def add_comment(self, key, body, *, guard=None):
            posted.append((key, body, guard))
            return True

    monkeypatch.setattr(publisher, "get_ticket_source", lambda pid: Source())
    draft = _gap_draft(stories=[], existing=[
        {"key": "PAY-2", "assessment": "partial", "evidence": [], "suggested_edit": "Say three attempts."},
        {"key": "PAY-3", "assessment": "done", "evidence": [], "suggested_edit": None},
    ])
    session = _ready_session(project_id, mode="gap", draft=draft, epic_key="PAY-1")
    client = _client()

    bad = client.post(f"/api/po-sessions/{session['id']}/suggestions/post", json={"key": "PAY-3"})
    assert bad.status_code == 409 and "no suggested edit" in bad.json()["detail"]

    ok = client.post(f"/api/po-sessions/{session['id']}/suggestions/post", json={"key": "PAY-2"})
    assert ok.status_code == 200 and ok.json()["publish_result"]["comments"] == ["PAY-2"]
    assert posted[0][0] == "PAY-2" and posted[0][2] == "po_jira_comment_enabled"
    again = client.post(f"/api/po-sessions/{session['id']}/suggestions/post", json={"key": "PAY-2"})
    assert again.status_code == 409 and "already posted" in again.json()["detail"] and len(posted) == 1


def test_suggestions_belong_to_gap_analysis_and_a_settled_session(project_id):
    brainstorm = _ready_session(project_id)
    response = _client().post(f"/api/po-sessions/{brainstorm['id']}/suggestions/post", json={"key": "PAY-2"})
    assert response.status_code == 409 and "gap analysis" in response.json()["detail"]
    sm = StateManager()
    sm.queue_po_turn(brainstorm["id"], from_statuses=("ready",))
    queued = _client().post(f"/api/po-sessions/{brainstorm['id']}/suggestions/post", json={"key": "PAY-2"})
    assert queued.status_code == 409 and "queued" in queued.json()["detail"]
    assert _client().post("/api/po-sessions/999999/suggestions/post", json={"key": "x"}).status_code == 404
