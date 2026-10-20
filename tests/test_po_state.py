"""PO sessions' storage and lifecycle (elstin/state/manager_po.py), against a real
SQLite database: every transition is guarded by the status the row is in, because the
API, the runner and the publisher all move a session at once."""
from __future__ import annotations

import pytest

from elstin.db.models.po_session import po_run_id
from elstin.state.manager import StateManager
from elstin.state.manager_po import PoSessionConflict

pytestmark = pytest.mark.usefixtures("elstin_db")

DRAFT = {"mode": "split", "epic": {"summary": "E"}, "stories": []}


@pytest.fixture()
def sm() -> StateManager:
    return StateManager()


def _running(sm, project_id, **kwargs) -> dict:
    session = sm.create_po_session(project_id, brief="Plan it", mode=kwargs.pop("mode", "brainstorm"), **kwargs)
    claimed = sm.claim_next_po_session()
    assert claimed["id"] == session["id"]
    return claimed


def _finished(sm, project_id, **kwargs) -> dict:
    session = _running(sm, project_id, **kwargs)
    return sm.finish_po_turn(session["id"], draft=DRAFT, questions=[], reply="Here you go", cost_usd=0.5)


def test_a_new_session_is_queued_with_its_brief_as_the_first_line_of_the_conversation(sm, project_id):
    session = sm.create_po_session(project_id, brief="Plan it", mode="brainstorm", jira_project_key="PAY", context={"epic_summary": ""})
    assert (session["status"], session["draft"], session["draft_version"], session["mode"]) == ("queued", None, 0, "brainstorm")
    assert (session["jira_project_key"], session["context"], session["cost_usd"]) == ("PAY", {"epic_summary": ""}, 0.0)

    messages = sm.list_po_messages(session["id"])
    assert [(m["role"], m["text"], m["delivered"]) for m in messages] == [("person", "Plan it", True)]
    assert sm.get_po_session(session["id"], messages=True)["messages"] == messages
    assert sm.get_po_session(999_999) is None


def test_sessions_are_listed_newest_first_and_discarded_ones_hidden(sm, project_id):
    first = sm.create_po_session(project_id, brief="a", mode="brainstorm")
    second = sm.create_po_session(project_id, brief="b", mode="brainstorm")
    other = sm.create_project("Other")
    sm.create_po_session(other, brief="c", mode="brainstorm")
    sm.discard_po_session(first["id"])

    assert [s["id"] for s in sm.list_po_sessions(project_id)] == [second["id"]]
    assert [s["id"] for s in sm.list_po_sessions(project_id, include_discarded=True)] == [second["id"], first["id"]]


def test_the_oldest_queued_session_is_claimed_once(sm, project_id):
    first = sm.create_po_session(project_id, brief="a", mode="brainstorm")
    second = sm.create_po_session(project_id, brief="b", mode="brainstorm")

    claimed = sm.claim_next_po_session()
    assert (claimed["id"], claimed["status"]) == (first["id"], "running")
    assert sm.claim_next_po_session()["id"] == second["id"]
    assert sm.claim_next_po_session() is None
    assert sm.count_running_po_sessions(project_id) == 2


def test_a_finished_turn_saves_the_draft_and_the_agents_reply(sm, project_id):
    session = _running(sm, project_id)
    saved = sm.finish_po_turn(session["id"], draft=DRAFT, questions=[], reply="Here you go", cost_usd=0.5, context={"epic_summary": "E"})
    assert (saved["status"], saved["draft"], saved["draft_version"], saved["cost_usd"]) == ("ready", DRAFT, 1, 0.5)
    assert saved["context"] == {"epic_summary": "E"}
    assert [(m["role"], m["text"], m["draft_version"]) for m in sm.list_po_messages(session["id"])] == [
        ("person", "Plan it", None), ("agent", "Here you go", 1),
    ]


def test_questions_leave_the_session_awaiting_input(sm, project_id):
    session = _running(sm, project_id)
    questions = [{"id": "q1", "question": "Which?", "options": []}]
    saved = sm.finish_po_turn(session["id"], draft=DRAFT, questions=questions, reply="Two things", cost_usd=0)
    assert (saved["status"], saved["questions"]) == ("awaiting_input", questions)


def test_a_message_the_turn_never_read_queues_the_next_turn(sm, project_id):
    session = _running(sm, project_id)
    sm.add_po_message(session["id"], "One more thing")
    assert sm.has_unread_po_messages(session["id"])
    saved = sm.finish_po_turn(session["id"], draft=DRAFT, questions=[], reply="Done", cost_usd=0)
    assert saved["status"] == "queued"
    sm.discard_po_session(session["id"])  # out of the queue, so the next claim is the one below

    # …and a message the turn did read does not.
    other = _running(sm, project_id)
    sm.add_po_message(other["id"], "Read live")
    assert [m["text"] for m in sm.take_po_messages(other["id"])] == ["Read live"]
    assert sm.finish_po_turn(other["id"], draft=DRAFT, questions=[], reply="Done", cost_usd=0)["status"] == "ready"


def test_a_result_for_a_session_that_is_no_longer_running_is_dropped(sm, project_id):
    session = _running(sm, project_id)
    sm.discard_po_session(session["id"])
    assert sm.finish_po_turn(session["id"], draft=DRAFT, questions=[], reply="x", cost_usd=1) is None
    assert sm.fail_po_session(session["id"], "late") is None
    assert sm.get_po_session(session["id"])["draft"] is None
    assert sm.finish_po_turn(999_999, draft=DRAFT, questions=[], reply="x", cost_usd=1) is None


def test_a_failed_turn_keeps_the_draft_a_person_could_already_see(sm, project_id):
    session = _finished(sm, project_id)
    sm.add_po_message(session["id"], "Change it")
    sm.queue_po_turn(session["id"], from_statuses=("ready", "awaiting_input"))
    sm.claim_next_po_session()
    failed = sm.fail_po_session(session["id"], "provider down " * 500, cost_usd=0.75)
    assert (failed["status"], failed["draft"], failed["cost_usd"]) == ("failed", DRAFT, 0.75)
    assert len(failed["error"]) == 4000


def test_a_message_is_delivered_once(sm, project_id):
    session = sm.create_po_session(project_id, brief="Plan it", mode="brainstorm")
    sm.add_po_message(session["id"], "first")
    sm.add_po_message(session["id"], "second")
    assert [m["text"] for m in sm.take_po_messages(session["id"])] == ["first", "second"]
    assert sm.take_po_messages(session["id"]) == []
    assert not sm.has_unread_po_messages(session["id"])
    assert sm.add_po_message(999_999, "nobody home") is None


def test_queueing_a_turn_only_happens_from_the_allowed_statuses(sm, project_id):
    session = _finished(sm, project_id)
    assert sm.queue_po_turn(session["id"], from_statuses=("ready",))["status"] == "queued"
    sm.claim_next_po_session()
    # Running: not queued again by a message.
    assert sm.queue_po_turn(session["id"], from_statuses=("ready", "awaiting_input"))["status"] == "running"
    assert sm.queue_po_turn(999_999, from_statuses=("ready",)) is None


def test_a_person_edit_needs_the_version_it_was_made_over(sm, project_id):
    session = _finished(sm, project_id)
    edited = {**DRAFT, "epic": {"summary": "Edited"}}

    saved = sm.save_po_draft(session["id"], edited, expected_version=1, edited_paths=["epic.summary", "epic.summary"])
    assert (saved["draft"], saved["draft_version"], saved["edited_by_person"], saved["person_edits"]) == (edited, 2, True, ["epic.summary"])

    with pytest.raises(PoSessionConflict, match="changed since you opened it"):
        sm.save_po_draft(session["id"], DRAFT, expected_version=1, edited_paths=["x"])
    assert sm.get_po_session(session["id"])["draft"] == edited

    again = sm.save_po_draft(session["id"], {**edited, "gaps": ["g"]}, expected_version=2, edited_paths=["gaps", "epic.summary"])
    assert again["person_edits"] == ["epic.summary", "gaps"]


def test_a_draft_cannot_be_edited_while_the_agent_works_or_before_there_is_one(sm, project_id):
    fresh = sm.create_po_session(project_id, brief="x", mode="brainstorm")
    with pytest.raises(PoSessionConflict, match="no draft"):
        sm.save_po_draft(fresh["id"], DRAFT, expected_version=0, edited_paths=[])
    sm.discard_po_session(fresh["id"])

    session = _finished(sm, project_id)
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    sm.claim_next_po_session()
    with pytest.raises(PoSessionConflict, match="while the session is running"):
        sm.save_po_draft(session["id"], DRAFT, expected_version=1, edited_paths=[])
    with pytest.raises(PoSessionConflict, match="No such session"):
        sm.save_po_draft(999_999, DRAFT, expected_version=0, edited_paths=[])


def test_a_session_left_running_by_a_dead_daemon_is_queued_again_with_its_unread_messages(sm, project_id):
    session = _finished(sm, project_id)
    sm.add_po_message(session["id"], "Please split s1")
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    claimed = sm.claim_next_po_session()
    assert [m["text"] for m in sm.take_po_messages(claimed["id"])] == ["Please split s1"]  # the lost turn read it

    # Nobody in this process is working on it.
    assert sm.requeue_orphaned_po_sessions(frozenset()) == [session["id"]]
    again = sm.get_po_session(session["id"])
    assert again["status"] == "queued"
    # The new turn reads the message again; the opening brief is not re-delivered as news.
    assert [m["text"] for m in sm.take_po_messages(session["id"])] == ["Please split s1"]


def test_a_running_session_with_a_live_task_is_left_alone(sm, project_id):
    session = _running(sm, project_id)
    assert sm.requeue_orphaned_po_sessions(frozenset({session["id"]})) == []
    assert sm.get_po_session(session["id"])["status"] == "running"


def test_the_first_turn_that_dies_does_not_make_the_brief_news(sm, project_id):
    session = _running(sm, project_id)
    sm.requeue_orphaned_po_sessions(frozenset())
    assert sm.take_po_messages(session["id"]) == []


def test_answers_are_taken_only_while_the_agent_waits_on_them(sm, project_id):
    session = _running(sm, project_id)
    questions = [{"id": "q1", "question": "Which?", "options": []}]
    sm.finish_po_turn(session["id"], draft=DRAFT, questions=questions, reply="?", cost_usd=0)

    answers = [{"id": "q1", "question": "Which?", "answer": "Stripe"}]
    saved = sm.set_po_answers(session["id"], answers)
    assert (saved["status"], saved["answers"]) == ("queued", answers)
    with pytest.raises(PoSessionConflict, match="not waiting for answers"):
        sm.set_po_answers(session["id"], answers)

    # The next turn that finishes consumes them.
    sm.claim_next_po_session()
    done = sm.finish_po_turn(session["id"], draft=DRAFT, questions=[], reply="ok", cost_usd=0)
    assert done["answers"] == [] and done["questions"] == []

    with pytest.raises(PoSessionConflict, match="no open questions"):
        sm.set_po_answers(session["id"], answers)
    with pytest.raises(PoSessionConflict, match="No such session"):
        sm.set_po_answers(999_999, answers)


def _set_status(session_id: int, status: str) -> None:
    from elstin.db.engine import session_scope
    from elstin.db.models import PoSession

    with session_scope() as db:
        db.get(PoSession, session_id).status = status


def test_discarding_is_refused_once_publishing_has_begun(sm, project_id):
    session = _finished(sm, project_id)
    assert sm.discard_po_session(session["id"])["status"] == "discarded"

    _set_status(session["id"], "publishing")
    with pytest.raises(PoSessionConflict, match="publishing"):
        sm.discard_po_session(session["id"])
    # Deleting it would leave tickets half made with nothing recording which.
    with pytest.raises(PoSessionConflict, match="publishing"):
        sm.delete_po_session(session["id"])

    # A published session is a record of what was created: it cannot be discarded, but it can be deleted.
    _set_status(session["id"], "published")
    with pytest.raises(PoSessionConflict, match="published"):
        sm.discard_po_session(session["id"])
    assert sm.delete_po_session(session["id"])["sessions"] == 1


def test_deleting_a_session_removes_its_conversation_and_its_telemetry(sm, project_id):
    from elstin.services.run_activity import append_activity
    from elstin.services.usage import get_run_outcome, record_phase_usage, record_run_outcome

    session = _finished(sm, project_id)
    other = sm.create_po_session(project_id, brief="keep me", mode="brainstorm")
    run_id = po_run_id(session["id"])
    append_activity(run_id, type="status", phase="po", title="Drafting")
    append_activity(po_run_id(other["id"]), type="status", phase="po", title="Other")
    record_phase_usage(run_id, phase="po", model="gpt-5.1", input_tokens=10, output_tokens=5)
    record_run_outcome(run_id, explorer_count=2)

    removed = sm.delete_po_session(session["id"])
    assert removed["sessions"] == 1 and removed["messages"] == 2
    assert removed["activity_events"] == 1 and removed["usage_rows"] == 1 and removed["run_outcomes"] == 1
    assert sm.get_po_session(session["id"]) is None and sm.list_po_messages(session["id"]) == []
    assert get_run_outcome(run_id) is None
    assert sm.get_po_session(other["id"]) is not None and sm.delete_po_session(999_999) is None

    from elstin.services.run_activity import list_activity

    assert [e["title"] for e in list_activity(po_run_id(other["id"]))] == ["Other"]


def test_deleting_a_project_removes_its_sessions(sm):
    project = sm.create_project("Doomed")
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    counts = sm.delete_project_records(project)
    assert counts["po_sessions"] == 1
    assert sm.get_po_session(session["id"]) is None


def test_cost_can_be_set_on_its_own(sm, project_id):
    session = sm.create_po_session(project_id, brief="x", mode="brainstorm")
    sm.set_po_cost(session["id"], 1.25)
    sm.set_po_context(session["id"], {"epic_summary": "later"})
    saved = sm.get_po_session(session["id"])
    assert (saved["cost_usd"], saved["context"]) == (1.25, {"epic_summary": "later"})


# -- publishing ----------------------------------------------------------------------------------------------------------


def test_publishing_begins_once_from_a_ready_draft(sm, project_id):
    session = _finished(sm, project_id)
    begun = sm.begin_po_publish(session["id"], jira_project_key="PAY")
    assert begun["status"] == "publishing" and begun["jira_project_key"] == "PAY"
    assert begun["publish_result"] == {"started": True, "failed": None}
    with pytest.raises(PoSessionConflict, match="Only a draft that is ready"):
        sm.begin_po_publish(session["id"])
    with pytest.raises(PoSessionConflict, match="No such session"):
        sm.begin_po_publish(999_999)

    queued = sm.create_po_session(project_id, brief="x", mode="brainstorm")
    with pytest.raises(PoSessionConflict, match="the session is queued"):
        sm.begin_po_publish(queued["id"])


def test_the_checkpoint_is_merged_never_replaced(sm, project_id):
    session = _finished(sm, project_id)
    sm.begin_po_publish(session["id"])
    sm.record_po_publish(session["id"], created={"epic": "PAY-1"})
    sm.record_po_publish(session["id"], created={"s1": "PAY-2"}, link="PAY-1>PAY-2", epic_key="PAY-1")
    sm.record_po_publish(session["id"], link="PAY-1>PAY-2", comment="PAY-7")
    sm.record_po_publish(session["id"], comment="PAY-7", synced=True)
    result = sm.get_po_session(session["id"])["publish_result"]
    assert result["created"] == {"epic": "PAY-1", "s1": "PAY-2"}
    assert result["links"] == ["PAY-1>PAY-2"] and result["comments"] == ["PAY-7"]
    assert (result["epic_key"], result["synced"], result["started"]) == ("PAY-1", True, True)
    assert sm.record_po_publish(999_999, created={"x": "y"}) is None


def test_a_publish_scoped_to_some_stories_leaves_the_session_ready_until_every_story_is_in_jira(sm, project_id):
    draft = {"mode": "brainstorm", "epic": {"summary": "E"}, "stories": [{"ref": "s1"}, {"ref": "s2"}]}
    session = _running(sm, project_id)
    sm.finish_po_turn(session["id"], draft=draft, questions=[], reply="ok", cost_usd=0)

    sm.begin_po_publish(session["id"])
    sm.record_po_publish(session["id"], created={"epic": "PAY-1", "s1": "PAY-2"})
    assert sm.finish_po_publish(session["id"], scoped=True)["status"] == "ready"  # s2 is still to do

    sm.begin_po_publish(session["id"])
    sm.record_po_publish(session["id"], created={"s2": "PAY-3"})
    assert sm.finish_po_publish(session["id"], scoped=True)["status"] == "published"


def test_a_scoped_brainstorm_publish_is_not_finished_while_the_epic_is_missing(sm, project_id):
    draft = {"mode": "brainstorm", "epic": {"summary": "E"}, "stories": [{"ref": "s1"}]}
    session = _running(sm, project_id)
    sm.finish_po_turn(session["id"], draft=draft, questions=[], reply="ok", cost_usd=0)
    sm.begin_po_publish(session["id"])
    sm.record_po_publish(session["id"], created={"s1": "PAY-2"})
    assert sm.finish_po_publish(session["id"], scoped=True)["status"] == "ready"


def test_a_publish_ends_published_or_back_to_ready_with_its_progress_kept(sm, project_id):
    done = _finished(sm, project_id)
    sm.begin_po_publish(done["id"])
    assert sm.finish_po_publish(done["id"])["status"] == "published"
    assert sm.finish_po_publish(done["id"]) is None  # only from publishing

    stopped = _finished(sm, project_id)
    sm.begin_po_publish(stopped["id"])
    sm.record_po_publish(stopped["id"], created={"epic": "PAY-1"})
    failed = sm.fail_po_publish(stopped["id"], "JIRA refused " * 500)
    assert failed["status"] == "ready" and len(failed["error"]) == 4000
    assert failed["publish_result"]["created"] == {"epic": "PAY-1"}
    assert sm.fail_po_publish(stopped["id"], "again") is None

    # And it can be pressed again, which clears the error.
    again = sm.begin_po_publish(stopped["id"])
    assert again["status"] == "publishing" and again["error"] is None and again["publish_result"]["created"] == {"epic": "PAY-1"}


def test_an_interrupted_publish_is_recovered_unless_it_is_alive(sm, project_id):
    dead = _finished(sm, project_id)
    alive = _finished(sm, project_id)
    for session in (dead, alive):
        sm.begin_po_publish(session["id"])
    assert sm.recover_interrupted_publishes(frozenset({alive["id"]})) == [dead["id"]]
    assert sm.get_po_session(dead["id"])["status"] == "ready" and "interrupted" in sm.get_po_session(dead["id"])["error"]
    assert sm.get_po_session(alive["id"])["status"] == "publishing"


def test_a_draft_the_agent_asked_about_can_stand_on_its_guesses(sm, project_id):
    session = _running(sm, project_id)
    sm.finish_po_turn(session["id"], draft=DRAFT, questions=[{"id": "q1", "question": "?", "options": []}], reply="?", cost_usd=0)
    dismissed = sm.dismiss_po_questions(session["id"])
    assert dismissed["status"] == "ready" and dismissed["questions"] == []
    with pytest.raises(PoSessionConflict, match="not waiting for answers"):
        sm.dismiss_po_questions(session["id"])
    with pytest.raises(PoSessionConflict, match="No such session"):
        sm.dismiss_po_questions(999_999)
