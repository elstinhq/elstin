"""The daemon's PO runner (elstin/daemon/po_runner.py): picking up queued turns,
what a turn is given, what is saved, and what happens when it fails, is
cancelled or the daemon dies. The agent itself is faked at `run_product_owner`
(its own behaviour is tests/test_po_loop.py's); the database is real."""
from __future__ import annotations

import asyncio

import pytest

import elstin.daemon.po_runner as po_runner
from elstin.agents.roles.po_loop import PoLoopError, PoTurnResult
from elstin.services.jira_service import JiraTicket
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")


def _story(ref="s1", **overrides):
    story = {
        "ref": ref, "summary": f"Do thing {ref}", "description": f"Why {ref} exists.",
        "acceptance_criteria": [f"{ref} behaves"], "issue_type": "Story", "kind": "dev", "labels": [],
        "depends_on": [], "subtasks": [], "evidence": [], "rationale": "", "size": "M",
    }
    story.update(overrides)
    return story


def _agent_draft(mode="brainstorm", **overrides):
    draft = {
        "mode": mode,
        "epic": {
            "summary": "Retry payments", "description": "Retry failed payments.", "acceptance_criteria": ["Retries happen"],
            "existing_key": None if mode == "brainstorm" else "PAY-1", "suggested_description": None,
        },
        "stories": [_story("s1"), _story("s2", depends_on=["s1"])],
        "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [], "questions": [], "reply": "Drafted two stories.",
    }
    draft.update(overrides)
    return draft


class FakeAgent:
    """Stands in for run_product_owner: records what a turn was given, answers
    with the next scripted draft (or raises it), and can be made to wait."""

    def __init__(self, *drafts):
        self.drafts = list(drafts)
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        item = self.drafts.pop(0) if self.drafts else _agent_draft()
        if isinstance(item, Exception):
            raise item
        return PoTurnResult(
            draft=item, reply=item.get("reply", ""), questions=item.get("questions", []), defects=[], attempts=1,
        )


@pytest.fixture()
def agent(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(po_runner, "run_product_owner", fake)
    monkeypatch.setattr(po_runner, "_tasks", {})
    return fake


@pytest.fixture()
def project(project_id, git_repo, tmp_path):
    from elstin.config.store import set_config

    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    StateManager().add_project_repo(project_id, git_repo, label="widgets")
    return project_id


class FakeSource:
    name = "jira"

    def __init__(self, tree):
        self.tree = tree

    def get_ticket_tree(self, key, max_tickets=None):
        return self.tree


def _jira(monkeypatch, tree):
    monkeypatch.setattr(po_runner, "ticket_source_configured", lambda project_id: True)
    monkeypatch.setattr(po_runner, "get_ticket_source", lambda project_id: FakeSource(tree))


def _ticket(key, *, issue_type="Story", parent_key=None, summary=None, description="", status="To Do"):
    return JiraTicket(
        key=key, summary=summary or f"{key} summary", description=description, status=status, issue_type=issue_type,
        priority=None, parent_key=parent_key,
    )


async def _run_once(sm) -> list:
    tasks = await po_runner.run_po_sessions(sm)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return tasks


# -- a turn ------------------------------------------------------------------------------------------------


async def test_a_queued_session_is_drafted_and_saved(project, agent, monkeypatch):
    from elstin.services.usage import record_phase_usage

    sm = StateManager()
    session = sm.create_po_session(project, brief="Plan payment retries", mode="brainstorm", jira_project_key="PAY")

    async def agent_that_spends(**kwargs):
        record_phase_usage(kwargs["run_id"], phase="po", model="gpt-5.1", input_tokens=1000, output_tokens=500, cost_usd=0.25)
        return await agent(**kwargs)

    monkeypatch.setattr(po_runner, "run_product_owner", agent_that_spends)
    assert len(await _run_once(sm)) == 1

    saved = sm.get_po_session(session["id"], messages=True)
    assert saved["status"] == "ready" and saved["draft_version"] == 1 and saved["cost_usd"] == 0.25
    # The conversation's parts are the conversation's, not the draft's.
    assert "reply" not in saved["draft"] and "questions" not in saved["draft"]
    assert [s["ref"] for s in saved["draft"]["stories"]] == ["s1", "s2"]
    assert [(m["role"], m["text"]) for m in saved["messages"]] == [("person", "Plan payment retries"), ("agent", "Drafted two stories.")]
    assert saved["context"] == {"epic_summary": "", "existing": [], "known_keys": []}

    call = agent.calls[0]
    assert call["session_id"] == session["id"] and call["run_id"] == f"po-{session['id']}" and call["explore"] is True
    assert call["inputs"].mode == "brainstorm" and call["inputs"].person_brief == "Plan payment retries"
    assert call["inputs"].revise is None and call["jira_project_key"] == "PAY"
    assert call["default_label"] == "widgets" and set(call["repo_roots"]) == {"widgets"}
    assert call["ctx"].mode == "brainstorm" and call["ctx"].existing == ()

    from elstin.services.run_activity import list_activity

    titles = [e["title"] for e in list_activity(f"po-{session['id']}")]
    assert "Gathered what the agent starts from" in titles and "Draft ready" in titles


async def test_questions_leave_the_session_waiting_for_the_person(project, agent):
    agent.drafts = [_agent_draft(questions=[{"id": "q1", "question": "Which provider?", "context": None, "options": []}])]
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    await _run_once(sm)
    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "awaiting_input" and saved["questions"][0]["id"] == "q1"


async def test_only_one_turn_runs_at_a_time(project, agent):
    sm = StateManager()
    first = sm.create_po_session(project, brief="a", mode="brainstorm")
    second = sm.create_po_session(project, brief="b", mode="brainstorm")
    agent.gate = asyncio.Event()

    started = await po_runner.run_po_sessions(sm)
    assert len(started) == 1
    await agent.started.wait()
    assert await po_runner.run_po_sessions(sm) == []  # busy
    assert sm.get_po_session(first["id"])["status"] == "running"
    assert sm.get_po_session(second["id"])["status"] == "queued"
    assert po_runner.live_po_session_ids() == {first["id"]}

    agent.gate.set()
    await asyncio.gather(*started)
    await asyncio.sleep(0)  # the done-callback frees the slot
    assert po_runner.live_po_session_ids() == frozenset()
    await _run_once(sm)
    assert sm.get_po_session(second["id"])["status"] == "ready"


async def test_the_poll_loop_picks_sessions_up(project, agent):
    from elstin.daemon.main import poll_once

    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    await poll_once(sm, wait=True)
    assert sm.get_po_session(session["id"])["status"] == "ready"


# -- what a turn is given ----------------------------------------------------------------------------------------


async def test_a_gap_session_is_given_the_epic_its_stories_and_elstins_board(project, agent, monkeypatch):
    tree = [
        _ticket("PAY-1", issue_type="Epic", summary="Retry payments", description="Retry failed payments."),
        _ticket("PAY-2", parent_key="PAY-1", summary="Backoff", status="To Do", description="1s, 5s, 30s"),
    ]
    _jira(monkeypatch, tree)
    sm = StateManager()
    sm.create_epic("PAY-1", project_id=project, levels=[["PAY-2"]], summary="Retry payments")
    sm.create_story("PAY-2", epic_key="PAY-1", wave=0, summary="Backoff", description="", kind="dev", kind_reason="default")
    sm.set_story_status("PAY-2", "merged")
    session = sm.create_po_session(project, brief="Where does this stand?", mode="gap", epic_key="PAY-1", jira_project_key="PAY")

    await _run_once(sm)

    call = agent.calls[0]
    inputs = call["inputs"]
    assert inputs.epic_key == "PAY-1" and "Retry failed payments." in inputs.epic_context and "PAY-2 — Backoff" in inputs.epic_context
    # elstin's view, which can differ from JIRA's.
    assert "PAY-2 [merged]" in inputs.board_context
    assert call["ctx"].existing == (("PAY-2", "Backoff"),) and call["ctx"].epic_summary == "Retry payments"
    # What the agent was shown it may cite without opening again.
    assert call["research"].knows_jira("PAY-2") and "Status: To Do" in call["research"].jira["PAY-2"]
    assert "JIRA (jira_search, jira_get_issue)" in inputs.capabilities
    assert [row["key"] for row in call["board_reader"]("PAY-1")] == ["PAY-1", "PAY-2"]
    assert call["board_reader"]("OTHER-1") == []
    assert sm.get_po_session(session["id"])["context"]["existing"] == [["PAY-2", "Backoff"]]


async def test_the_board_reader_only_shows_this_projects_epics(project, agent, monkeypatch):
    _jira(monkeypatch, [_ticket("PAY-1", issue_type="Epic")])
    sm = StateManager()
    other = sm.create_project("Other")
    sm.create_epic("OTH-1", project_id=other, levels=[], summary="Elsewhere")
    sm.create_po_session(project, brief="x", mode="split", epic_key="PAY-1")
    await _run_once(sm)
    assert agent.calls[0]["board_reader"]("OTH-1") == []


async def test_an_epic_that_cannot_be_read_fails_the_session_with_the_reason(project, agent, monkeypatch):
    class Broken(FakeSource):
        def get_ticket_tree(self, key, max_tickets=None):
            raise RuntimeError("503 from JIRA")

    monkeypatch.setattr(po_runner, "ticket_source_configured", lambda project_id: True)
    monkeypatch.setattr(po_runner, "get_ticket_source", lambda project_id: Broken([]))
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="split", epic_key="PAY-1")
    await _run_once(sm)
    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "failed" and "Could not read PAY-1" in saved["error"] and "503 from JIRA" in saved["error"]
    assert agent.calls == []


async def test_an_epic_session_without_a_ticket_source_says_so(project, agent, monkeypatch):
    monkeypatch.setattr(po_runner, "ticket_source_configured", lambda project_id: False)
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="split", epic_key="PAY-1")
    await _run_once(sm)
    assert "ticket source is not configured" in sm.get_po_session(session["id"])["error"]


async def test_a_project_with_no_folders_cannot_be_drafted_for(project_id, agent):
    sm = StateManager()
    session = sm.create_po_session(project_id, brief="x", mode="brainstorm")
    await _run_once(sm)
    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "failed" and "no folders" in saved["error"]
    assert agent.calls == []


async def test_confluence_pages_the_brief_links_to_are_read_for_the_agent(project, agent, monkeypatch):
    class Page:
        id, title, space, url, text = "222", "Retry design", "ENG", "https://wiki/222", "Backoff is 1s, 5s, 30s."

    class Wiki:
        def __init__(self, project_id):
            pass

        def get_page(self, page_id, *, allow_ids=()):
            assert page_id == "222" and "222" in allow_ids
            return Page()

    monkeypatch.setattr(po_runner, "ConfluenceService", type("W", (Wiki,), {"is_configured": staticmethod(lambda project_id: True)}))
    sm = StateManager()
    sm.create_po_session(
        project, brief="See https://acme.atlassian.net/wiki/spaces/ENG/pages/222/Retry for the design", mode="brainstorm",
    )
    await _run_once(sm)

    call = agent.calls[0]
    assert "Backoff is 1s, 5s, 30s." in call["inputs"].linked_pages
    assert call["research"].knows_page("222")
    assert call["linked_page_ids"] == frozenset({"222"}) and call["confluence"] is not None
    assert any("Confluence" in c for c in call["inputs"].capabilities)


# -- revising ------------------------------------------------------------------------------------------------------


async def test_a_revision_keeps_a_persons_edit_and_is_told_what_changed_and_what_was_asked(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="Plan payment retries", mode="brainstorm")
    await _run_once(sm)

    # The person edits the draft by hand, answers nothing, and asks for one more story.
    ready = sm.get_po_session(session["id"])
    edited = {key: value for key, value in ready["draft"].items()}
    edited["stories"] = [{**edited["stories"][0], "summary": "A person's own wording"}, *edited["stories"][1:]]
    sm.save_po_draft(session["id"], edited, expected_version=ready["draft_version"], edited_paths=["stories.s1.summary"])
    sm.add_po_message(session["id"], "Add a story for notifying the customer.")
    assert sm.queue_po_turn(session["id"], from_statuses=("ready", "awaiting_input"))["status"] == "queued"

    # The agent keeps the edit and adds the story.
    revised = _agent_draft(stories=[_story("s1", summary="A person's own wording"), _story("s2", depends_on=["s1"]), _story("s3")])
    agent.drafts = [revised]
    await _run_once(sm)

    call = agent.calls[1]
    assert call["explore"] is False  # the code is already in front of it
    revise = call["inputs"].revise
    assert revise["draft"]["stories"][0]["summary"] == "A person's own wording"
    assert revise["edited_by_person"] is True and revise["person_edits"] == ["stories.s1.summary"]
    assert revise["new_messages"] == ["Add a story for notifying the customer."]
    # The conversation before the new message, without the brief (it has its own section).
    assert [(m["role"], m["text"]) for m in revise["history"]] == [("agent", "Drafted two stories.")]
    assert revise["version"] == ready["draft_version"] + 1

    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "ready"
    assert [s["summary"] for s in saved["draft"]["stories"]][0] == "A person's own wording"
    assert len(saved["draft"]["stories"]) == 3 and saved["edited_by_person"] is True
    assert not sm.has_unread_po_messages(session["id"])


async def test_the_servers_verification_marks_are_not_shown_to_the_model(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.drafts = [_agent_draft(stories=[_story("s1", evidence=[{"source": "code", "ref": "README.md", "quote": "hello", "verified": True}])])]
    await _run_once(sm)
    assert sm.get_po_session(session["id"])["draft"]["stories"][0]["evidence"][0]["verified"] is True  # stored as it was
    sm.add_po_message(session["id"], "again")
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    await _run_once(sm)
    shown = agent.calls[1]["inputs"].revise["draft"]["stories"][0]["evidence"][0]
    assert "verified" not in shown


async def test_answers_are_given_to_the_next_turn(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.drafts = [_agent_draft(questions=[{"id": "q1", "question": "Which provider?", "context": None, "options": []}])]
    await _run_once(sm)
    sm.set_po_answers(session["id"], [{"id": "q1", "question": "Which provider?", "answer": "Stripe"}])
    await _run_once(sm)
    assert agent.calls[1]["inputs"].revise["answers"] == [{"id": "q1", "question": "Which provider?", "answer": "Stripe"}]
    assert sm.get_po_session(session["id"])["answers"] == []  # consumed


async def test_a_message_sent_while_the_agent_works_reaches_it_live_and_does_not_queue_another_turn(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.gate = asyncio.Event()
    started = await po_runner.run_po_sessions(sm)
    await agent.started.wait()

    sm.add_po_message(session["id"], "Use Stripe, not Adyen.")
    read = await asyncio.to_thread(agent.calls[0]["pending_messages"])  # the loop's hook, at the start of its next step
    assert len(read) == 1 and "Use Stripe, not Adyen." in read[0] and "you are only reading and drafting" in read[0]
    assert await asyncio.to_thread(agent.calls[0]["pending_messages"]) == []

    agent.gate.set()
    await asyncio.gather(*started)
    assert sm.get_po_session(session["id"])["status"] == "ready"  # nothing was left unread

    from elstin.services.run_activity import list_activity

    assert "The agent read 1 message from a person" in [e["title"] for e in list_activity(f"po-{session['id']}")]


async def test_a_message_the_turn_never_read_is_picked_up_by_the_next_one(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.gate = asyncio.Event()
    started = await po_runner.run_po_sessions(sm)
    await agent.started.wait()
    sm.add_po_message(session["id"], "Too late for this turn")
    agent.gate.set()
    await asyncio.gather(*started)
    assert sm.get_po_session(session["id"])["status"] == "queued"

    agent.gate = None
    await _run_once(sm)
    assert agent.calls[1]["inputs"].revise["new_messages"] == ["Too late for this turn"]


async def test_a_session_with_no_draft_yet_folds_a_later_message_into_the_brief(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="Plan retries", mode="brainstorm")
    sm.add_po_message(session["id"], "Also consider refunds")
    await _run_once(sm)
    inputs = agent.calls[0]["inputs"]
    assert inputs.revise is None
    assert "Plan retries" in inputs.person_brief and "Also consider refunds" in inputs.person_brief


# -- failure, cancellation, crashes -------------------------------------------------------------------------------


async def test_a_turn_the_agent_fails_is_the_sessions_failure_not_the_daemons(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.drafts = [PoLoopError("The draft gate rejected the draft: story s1 has no acceptance criteria")]
    await _run_once(sm)
    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "failed" and "no acceptance criteria" in saved["error"]

    from elstin.services.run_activity import list_activity

    assert "The PO turn failed" in [e["title"] for e in list_activity(f"po-{session['id']}")]


async def test_an_unexpected_error_is_recorded_and_the_next_session_still_runs(project, agent):
    sm = StateManager()
    first = sm.create_po_session(project, brief="a", mode="brainstorm")
    second = sm.create_po_session(project, brief="b", mode="brainstorm")
    agent.drafts = [ValueError("boom")]
    await _run_once(sm)
    assert sm.get_po_session(first["id"])["status"] == "failed"
    assert "Unexpected error: boom" in sm.get_po_session(first["id"])["error"]
    await _run_once(sm)
    assert sm.get_po_session(second["id"])["status"] == "ready"


async def test_a_failed_revision_keeps_the_draft(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    await _run_once(sm)
    sm.add_po_message(session["id"], "change it")
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    agent.drafts = [PoLoopError("provider down")]
    await _run_once(sm)
    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "failed" and len(saved["draft"]["stories"]) == 2


async def test_a_discarded_session_stops_its_turn_and_is_not_queued_again(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.gate = asyncio.Event()
    started = await po_runner.run_po_sessions(sm)
    await agent.started.wait()

    sm.discard_po_session(session["id"])  # first, as the API does, so the turn finds itself no longer running
    assert await po_runner.cancel_po_session_task(session["id"]) is True
    await asyncio.gather(*started, return_exceptions=True)

    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "discarded" and saved["draft"] is None
    assert await po_runner.cancel_po_session_task(session["id"]) is False


async def test_a_shutdown_puts_the_running_session_back_in_the_queue(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    agent.gate = asyncio.Event()
    await po_runner.run_po_sessions(sm)
    await agent.started.wait()

    await po_runner.cancel_po_work()
    assert sm.get_po_session(session["id"])["status"] == "queued"

    # …and the next daemon runs it.
    agent.gate = None
    await _run_once(sm)
    assert sm.get_po_session(session["id"])["status"] == "ready"


async def test_a_session_left_running_by_a_crashed_daemon_is_run_again(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    assert sm.claim_next_po_session()["id"] == session["id"]  # a daemon that then died
    assert po_runner.live_po_session_ids() == frozenset()

    await _run_once(sm)
    assert sm.get_po_session(session["id"])["status"] == "ready" and len(agent.calls) == 1


# -- publishing in the background ----------------------------------------------------------------------------------------


def _ready_for_publish(sm, project_id) -> dict:
    session = sm.create_po_session(project_id, brief="x", mode="brainstorm", jira_project_key="PAY")
    sm.claim_next_po_session()
    sm.finish_po_turn(session["id"], draft=_agent_draft(), questions=[], reply="ok", cost_usd=0)
    sm.begin_po_publish(session["id"])
    return sm.get_po_session(session["id"])


@pytest.fixture()
def publishes(monkeypatch):
    monkeypatch.setattr(po_runner, "_publish_tasks", {})


async def test_a_publish_that_completes_marks_the_session_published(project, publishes, monkeypatch):
    sm = StateManager()
    session = _ready_for_publish(sm, project)

    def fake_publish(sm_, session_id):
        sm_.record_po_publish(session_id, created={"epic": "PAY-9", "s1": "PAY-10"}, epic_key="PAY-9", synced=True)

    monkeypatch.setattr(po_runner, "publish_backlog", fake_publish)
    task = await po_runner.start_publish(sm, session["id"])
    assert po_runner.live_publish_session_ids() == {session["id"]}
    await task

    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "published" and saved["publish_result"]["created"] == {"epic": "PAY-9", "s1": "PAY-10"}

    from elstin.services.run_activity import list_activity

    titles = [e["title"] for e in list_activity(f"po-{session['id']}")]
    assert "Published" in titles
    await asyncio.sleep(0)
    assert po_runner.live_publish_session_ids() == frozenset()


async def test_a_publish_of_one_story_leaves_the_session_ready_for_the_next(project, publishes, monkeypatch):
    sm = StateManager()
    session = _ready_for_publish(sm, project)
    seen: list = []

    def fake_publish(sm_, session_id, *, refs):
        seen.append(refs)
        sm_.record_po_publish(session_id, created={"epic": "PAY-9", "s1": "PAY-10"}, epic_key="PAY-9", synced=True)

    monkeypatch.setattr(po_runner, "publish_backlog", fake_publish)
    await (await po_runner.start_publish(sm, session["id"], ["s1"]))

    saved = sm.get_po_session(session["id"])
    assert seen == [["s1"]] and saved["status"] == "ready" and saved["error"] is None  # s2 is not in JIRA yet

    from elstin.services.run_activity import list_activity

    assert "Published s1" in [e["title"] for e in list_activity(f"po-{session['id']}")]

    # …and the last story ends it.
    sm.begin_po_publish(session["id"])

    def publish_the_rest(sm_, session_id, *, refs):
        sm_.record_po_publish(session_id, created={"s2": "PAY-11"})

    monkeypatch.setattr(po_runner, "publish_backlog", publish_the_rest)
    await (await po_runner.start_publish(sm, session["id"], ["s2"]))
    assert sm.get_po_session(session["id"])["status"] == "published"


async def test_a_revision_leaves_what_is_already_in_jira_as_it_was(project, agent):
    sm = StateManager()
    session = sm.create_po_session(project, brief="x", mode="brainstorm")
    await _run_once(sm)
    first = sm.get_po_session(session["id"])
    sm.record_po_publish(session["id"], created={"epic": "PAY-1", "s1": "PAY-2"}, epic_key="PAY-1")
    original = {s["ref"]: s["summary"] for s in first["draft"]["stories"]}

    rewritten = _agent_draft(stories=[_story("s2", summary="Better s2")])
    rewritten["epic"]["summary"] = "Rewritten epic"
    agent.drafts = [rewritten]
    sm.add_po_message(session["id"], "Reshape it")
    sm.queue_po_turn(session["id"], from_statuses=("ready",))
    await _run_once(sm)

    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "ready"
    assert [(s["ref"], s["summary"]) for s in saved["draft"]["stories"]] == [("s1", original["s1"]), ("s2", "Better s2")]
    assert saved["draft"]["epic"]["summary"] == "Retry payments"
    # The agent was told, so it does not spend its turn rewriting them.
    from elstin.agents.roles.product_owner import build_po_user_text

    inputs = agent.calls[-1]["inputs"]
    assert inputs.revise["published"] == {"epic": "PAY-1", "s1": "PAY-2"}
    assert "already in JIRA" in build_po_user_text(inputs) and "s1 (PAY-2)" in build_po_user_text(inputs)


async def test_a_publish_that_stops_leaves_the_session_ready_with_the_reason(project, publishes, monkeypatch):
    from elstin.services.backlog_publisher import PublishError

    sm = StateManager()
    session = _ready_for_publish(sm, project)

    def fake_publish(sm_, session_id):
        sm_.record_po_publish(session_id, created={"epic": "PAY-9"})
        raise PublishError("Creating story s1: JIRA refused to create the Story: Epic Name is required")

    monkeypatch.setattr(po_runner, "publish_backlog", fake_publish)
    await (await po_runner.start_publish(sm, session["id"]))

    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "ready" and "Epic Name is required" in saved["error"]
    assert saved["publish_result"]["created"] == {"epic": "PAY-9"}  # kept, so pressing Publish again continues

    from elstin.services.run_activity import list_activity

    assert "Publishing stopped" in [e["title"] for e in list_activity(f"po-{session['id']}")]


async def test_an_unexpected_publish_error_is_the_sessions_not_the_daemons(project, publishes, monkeypatch):
    sm = StateManager()
    session = _ready_for_publish(sm, project)

    def fake_publish(sm_, session_id):
        raise ValueError("boom")

    monkeypatch.setattr(po_runner, "publish_backlog", fake_publish)
    await (await po_runner.start_publish(sm, session["id"]))
    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "ready" and "Unexpected error: boom" in saved["error"]


async def test_a_publish_left_running_by_a_dead_daemon_goes_back_to_ready_and_is_not_resumed_on_its_own(project, agent, publishes):
    sm = StateManager()
    session = _ready_for_publish(sm, project)
    sm.record_po_publish(session["id"], created={"epic": "PAY-9"})
    assert po_runner.live_publish_session_ids() == frozenset()

    await po_runner.run_po_sessions(sm)  # the poll that notices

    saved = sm.get_po_session(session["id"])
    assert saved["status"] == "ready" and "interrupted" in saved["error"] and "nothing is made twice" in saved["error"]
    assert saved["publish_result"]["created"] == {"epic": "PAY-9"}


async def test_a_publish_in_flight_is_not_mistaken_for_a_dead_one(project, agent, publishes, monkeypatch):
    sm = StateManager()
    session = _ready_for_publish(sm, project)
    release = asyncio.Event()

    async def slow(sm_, session_id):
        await release.wait()

    monkeypatch.setattr(po_runner, "_run_publish", slow)
    task = await po_runner.start_publish(sm, session["id"])
    await po_runner.run_po_sessions(sm)
    assert sm.get_po_session(session["id"])["status"] == "publishing"
    release.set()
    await task


async def test_a_shutdown_cancels_a_publish_without_failing_it(project, publishes, monkeypatch):
    sm = StateManager()
    session = _ready_for_publish(sm, project)
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow(sm_, session_id):
        started.set()
        await proceed.wait()

    monkeypatch.setattr(po_runner, "_run_publish", slow)
    await po_runner.start_publish(sm, session["id"])
    await started.wait()
    await po_runner.cancel_po_work()
    # Nothing marks it failed: the next daemon recovers it as interrupted.
    assert sm.get_po_session(session["id"])["status"] == "publishing"
