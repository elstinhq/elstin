"""The PO publisher (elstin/services/backlog_publisher.py) against a real Postgres
and a fake ticket source: what it creates and in what order, the checkpoint that
makes a stopped publish resumable, and that a retry never makes a duplicate.
No live JIRA — the source is faked at the TicketSource boundary."""
from __future__ import annotations

import pytest

import elstin.services.backlog_publisher as publisher
from elstin.services.backlog_publisher import PublishError, marker_label, post_suggestion, publish_backlog
from elstin.services.ticket_source import TicketWriteError
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


def _draft(mode="brainstorm", stories=None, **overrides):
    draft = {
        "mode": mode,
        "epic": {
            "summary": "Retry payments", "description": "Retry failed payments.", "acceptance_criteria": ["Retries happen"],
            "existing_key": None if mode == "brainstorm" else "PAY-1", "suggested_description": None,
        },
        "stories": stories if stories is not None else [_story("s1"), _story("s2", depends_on=["s1"])],
        "existing": [], "gaps": [], "assumptions": [], "out_of_scope": [],
    }
    draft.update(overrides)
    return draft


class FakeSource:
    """Stands in for JiraService: numbers what it creates, remembers it, and can be
    made to fail on the Nth create."""

    name = "jira"

    def __init__(self, *, fail_on_create: int | None = None, orphan: dict | None = None):
        self.created: list[dict] = []
        self.links: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.searches: list[str] = []
        self.fail_on_create = fail_on_create
        self.comments_enabled = True
        # Which setting each write named as its guard — the publisher's to choose, the source's to check.
        self.guards: dict[str, set[str | None]] = {"create": set(), "link": set(), "comment": set()}
        # {label: key} — tickets that exist in "JIRA" without elstin having recorded them.
        self.existing_by_label: dict[str, str] = dict(orphan or {})
        self._counter = 100

    def create_ticket(self, project_key, issue_type, summary, description, parent_key=None, labels=(), *, guard):
        self.guards["create"].add(guard)
        if self.fail_on_create is not None and len(self.created) + 1 == self.fail_on_create:
            self.fail_on_create = None  # fails once
            raise TicketWriteError(f"JIRA refused to create the {issue_type} {summary!r}: Epic Name is required")
        self._counter += 1
        key = f"{project_key}-{self._counter}"
        self.created.append({
            "key": key, "project": project_key, "type": issue_type, "summary": summary, "description": description,
            "parent": parent_key, "labels": list(labels),
        })
        return key

    def link_tickets(self, blocker, blocked, *, guard):
        self.guards["link"].add(guard)
        self.links.append((blocker, blocked))

    def search_tickets(self, query, max_results=50):
        self.searches.append(query)
        label = query.split('"')[1]
        if label in self.existing_by_label:
            return [type("T", (), {"key": self.existing_by_label[label]})()]
        for item in self.created:
            if label in item["labels"]:
                return [type("T", (), {"key": item["key"]})()]
        return []

    def add_comment(self, key, body, *, guard=None):
        self.guards["comment"].add(guard)
        if not self.comments_enabled:
            return False
        self.comments.append((key, body))
        return True


@pytest.fixture()
def project(project_id):
    return project_id


def _synced(calls):
    def sync(epic_key, *, project_id):
        calls.append((epic_key, project_id))
    return sync


def _session(sm, project_id, draft, *, mode=None, epic_key=None, jira_project_key="PAY") -> dict:
    mode = mode or draft["mode"]
    session = sm.create_po_session(project_id, brief="x", mode=mode, epic_key=epic_key, jira_project_key=jira_project_key)
    sm.claim_next_po_session()
    sm.finish_po_turn(session["id"], draft=draft, questions=[], reply="ok", cost_usd=0)
    sm.begin_po_publish(session["id"])
    return sm.get_po_session(session["id"])


def _publish(sm, session_id, source, **kwargs):
    synced: list = []
    return publish_backlog(sm, session_id, settle_seconds=0, sync=kwargs.pop("sync", _synced(synced)), **kwargs), synced


@pytest.fixture()
def source(monkeypatch):
    fake = FakeSource()
    monkeypatch.setattr(publisher, "get_ticket_source", lambda project_id: fake)
    return fake


# -- what is created ----------------------------------------------------------------------------------------


def test_a_brainstorm_creates_the_epic_then_the_stories_then_sub_tasks_then_links(project, source):
    sm = StateManager()
    draft = _draft(stories=[
        _story("s1", subtasks=[{"summary": "Write the schedule", "description": "1s, 5s, 30s"}, {"summary": "  ", "description": ""}]),
        _story("s2", depends_on=["s1"], kind="qa", labels=["payments"], issue_type="Task", subtasks=[{"summary": "Run it", "description": ""}]),
    ])
    session = _session(sm, project, draft)

    result, synced = _publish(sm, session["id"], source)

    assert [(c["type"], c["summary"]) for c in source.created] == [
        ("Epic", "Retry payments"), ("Story", "Do thing s1"), ("Task", "Do thing s2"),
        ("Sub-task", "Write the schedule"), ("Sub-task", "Run it"),
    ]
    epic, s1, s2, s1_1, s2_1 = source.created
    assert epic["parent"] is None and s1["parent"] == epic["key"] and s2["parent"] == epic["key"]
    assert s1_1["parent"] == s1["key"] and s2_1["parent"] == s2["key"]
    assert all(c["project"] == "PAY" for c in source.created)
    # s2 depends on s1: s1 blocks s2.
    assert source.links == [(s1["key"], s2["key"])]
    assert result["created"] == {"epic": epic["key"], "s1": s1["key"], "s2": s2["key"], "s1.1": s1_1["key"], "s2.1": s2_1["key"]}
    assert result["links"] == [f"{s1['key']}>{s2['key']}"] and result["epic_key"] == epic["key"]
    # (this fake sync puts nothing on the board, so it is tried again before it is reported as it stands)
    assert set(synced) == {(epic["key"], project)}
    # Every create and every link names the PO's own switch — not the framework's `jira_write_enabled`.
    assert source.guards["create"] == {"po_jira_create_enabled"} and source.guards["link"] == {"po_jira_create_enabled"}


def test_descriptions_end_with_their_acceptance_criteria_and_the_epic_too(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1", acceptance_criteria=["It works", "It is fast"])]))
    _publish(sm, session["id"], source)
    epic, story = source.created
    assert story["description"] == "Why s1 exists.\n\n## Acceptance Criteria\n- It works\n- It is fast"
    assert epic["description"] == "Retry failed payments.\n\n## Acceptance Criteria\n- Retries happen"


def test_a_qa_story_is_labelled_qa_and_every_ticket_carries_the_label_that_ties_it_to_its_draft(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1"), _story("s2", kind="qa", labels=["QA", "x"]), _story("s3", kind="qa")]))
    _publish(sm, session["id"], source)
    labels = {c["summary"]: c["labels"] for c in source.created}
    sid = session["id"]
    assert labels["Retry payments"] == [f"elstin-po-{sid}-epic"]
    assert labels["Do thing s1"] == [f"elstin-po-{sid}-s1"]  # a dev story gets nothing extra
    assert labels["Do thing s2"] == ["QA", "x", f"elstin-po-{sid}-s2"]  # already labelled, not twice
    assert labels["Do thing s3"] == ["qa", f"elstin-po-{sid}-s3"]
    assert marker_label(7, "s1.2") == "elstin-po-7-s1-2"


def test_a_story_is_created_after_the_stories_it_depends_on_whatever_order_the_draft_lists_them(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[
        _story("s1", depends_on=["s2", "s3"]), _story("s2", depends_on=["s3"]), _story("s3"),
    ]))
    _publish(sm, session["id"], source)
    assert [c["summary"] for c in source.created][1:] == ["Do thing s3", "Do thing s2", "Do thing s1"]
    keys = {c["summary"]: c["key"] for c in source.created}
    assert set(source.links) == {
        (keys["Do thing s3"], keys["Do thing s2"]), (keys["Do thing s2"], keys["Do thing s1"]), (keys["Do thing s3"], keys["Do thing s1"]),
    }


def test_split_mode_puts_the_stories_under_the_existing_epic_and_creates_no_epic(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft("split"), epic_key="PAY-1")
    result, synced = _publish(sm, session["id"], source)
    assert [c["type"] for c in source.created] == ["Story", "Story"]
    assert all(c["parent"] == "PAY-1" for c in source.created)
    assert "epic" not in result["created"] and result["epic_key"] == "PAY-1" and set(synced) == {("PAY-1", project)}


def test_a_dependency_on_an_existing_ticket_is_linked_to_that_ticket(project, source):
    sm = StateManager()
    draft = _draft("gap", stories=[_story("s1", depends_on=["PAY-2"])], existing=[
        {"key": "PAY-2", "assessment": "partial", "evidence": [], "suggested_edit": None},
    ])
    session = _session(sm, project, draft, epic_key="PAY-1")
    result, _ = _publish(sm, session["id"], source)
    assert source.links == [("PAY-2", result["created"]["s1"])]


# -- one story at a time ---------------------------------------------------------------------------------------------


def test_publishing_one_story_makes_the_epic_and_that_story_only(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[
        _story("s1", subtasks=[{"summary": "Write the schedule", "description": ""}]), _story("s2", depends_on=["s1"]),
    ]))

    result, synced = _publish(sm, session["id"], source, refs=["s1"])

    assert [(c["type"], c["summary"]) for c in source.created] == [
        ("Epic", "Retry payments"), ("Story", "Do thing s1"), ("Sub-task", "Write the schedule"),
    ]
    assert set(result["created"]) == {"epic", "s1", "s1.1"} and source.links == []
    assert set(synced) == {(result["epic_key"], project)}


def test_the_next_story_joins_the_epic_already_made_and_is_linked_to_the_story_it_depends_on(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1"), _story("s2", depends_on=["s1"]), _story("s3")]))
    first, _ = _publish(sm, session["id"], source, refs=["s1"])
    sm.fail_po_publish(session["id"], "pretend it stopped here")  # back to ready, as finishing a scoped publish does
    sm.begin_po_publish(session["id"])

    second, _ = _publish(sm, session["id"], source, refs=["s2"])

    epics = [c for c in source.created if c["type"] == "Epic"]
    assert len(epics) == 1 and second["epic_key"] == first["epic_key"]  # not made twice
    s2 = next(c for c in source.created if c["summary"] == "Do thing s2")
    assert s2["parent"] == first["epic_key"]
    assert source.links == [(first["created"]["s1"], second["created"]["s2"])]
    assert "s3" not in second["created"]


def test_a_scoped_publish_never_makes_a_story_twice(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1"), _story("s2")]))
    _publish(sm, session["id"], source, refs=["s1"])
    sm.fail_po_publish(session["id"], "again please")
    sm.begin_po_publish(session["id"])

    _publish(sm, session["id"], source, refs=["s1", "s2"])  # s1 is already there

    assert [c["summary"] for c in source.created].count("Do thing s1") == 1
    assert sorted(c["summary"] for c in source.created) == ["Do thing s1", "Do thing s2", "Retry payments"]


def test_a_scoped_publish_of_a_story_that_is_not_in_the_draft_is_refused(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft())
    with pytest.raises(PublishError, match="no story s9"):
        _publish(sm, session["id"], source, refs=["s9"])
    assert source.created == []


# -- onto the board -------------------------------------------------------------------------------------------------


def test_the_new_stories_are_brought_onto_the_board_and_the_result_says_so(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1")]))

    def sync(epic_key, *, project_id):
        sm.create_epic(epic_key, project_id=project_id, levels=[])
        for item in source.created:
            if item["type"] != "Epic":
                sm.create_story(item["key"], epic_key=epic_key, wave=0, summary=item["summary"], kind="dev", kind_reason="x")

    result, _ = _publish(sm, session["id"], source, sync=sync)
    assert result["synced"] is True and result["synced_stories"] == 1 and result["sync_error"] is None
    assert sm.get_epic(result["epic_key"])["manual_gate"] is True  # a new epic lands paused


def test_a_sync_that_trails_the_create_is_tried_again_before_it_is_reported(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1")]))
    attempts: list[int] = []

    def sync(epic_key, *, project_id):
        attempts.append(1)
        if len(attempts) == 3:  # the search index has caught up
            sm.create_epic(epic_key, project_id=project_id, levels=[])
            sm.create_story(source.created[1]["key"], epic_key=epic_key, wave=0, summary="s", kind="dev", kind_reason="x")

    result, _ = _publish(sm, session["id"], source, sync=sync)
    assert len(attempts) == 3 and result["synced"] is True


def test_a_sync_that_fails_does_not_undo_the_publish(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1")]))

    def broken(epic_key, *, project_id):
        raise RuntimeError("JIRA search is down")

    result, _ = _publish(sm, session["id"], source, sync=broken)
    assert result["synced"] is False and "JIRA search is down" in result["sync_error"]
    assert len(source.created) == 2 and result["created"]["s1"]  # the tickets exist; "Add ticket" can bring them in


# -- resumable, and never twice ---------------------------------------------------------------------------------------


def test_a_publish_that_stops_keeps_what_it_made_and_a_retry_makes_no_duplicates(project, source):
    sm = StateManager()
    draft = _draft(stories=[
        _story("s1", subtasks=[{"summary": "Sub", "description": ""}]), _story("s2", depends_on=["s1"]), _story("s3", depends_on=["s2"]),
    ])
    session = _session(sm, project, draft)
    source.fail_on_create = 3  # the epic and s1 are made; s2 is refused

    with pytest.raises(PublishError, match="Creating story s2 .*Epic Name is required"):
        _publish(sm, session["id"], source)
    assert [c["summary"] for c in source.created] == ["Retry payments", "Do thing s1"]
    stopped = sm.get_po_session(session["id"])["publish_result"]
    assert set(stopped["created"]) == {"epic", "s1"}
    assert stopped["failed"]["ref"] == "s2" and "Epic Name is required" in stopped["failed"]["error"]

    # Back to ready, as the daemon does; the person presses Publish again.
    sm.fail_po_publish(session["id"], "stopped")
    sm.begin_po_publish(session["id"])
    result, _ = _publish(sm, session["id"], source)

    summaries = [c["summary"] for c in source.created]
    assert sorted(summaries) == sorted(["Retry payments", "Do thing s1", "Do thing s2", "Do thing s3", "Sub"])
    assert len(summaries) == len(set(summaries))  # nothing made twice
    assert result["failed"] is None and set(result["created"]) == {"epic", "s1", "s2", "s3", "s1.1"}
    assert len(source.links) == 2 and len(set(source.links)) == 2


def test_a_create_whose_checkpoint_never_landed_is_found_by_its_label_not_made_again(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1")]))
    sid = session["id"]
    # The epic was created in JIRA and the process died before it was recorded;
    # a first record shows the publish had begun.
    source.existing_by_label[f"elstin-po-{sid}-epic"] = "PAY-77"
    sm.record_po_publish(sid, failed={"step": "x", "ref": "epic", "error": "died"})

    result, _ = _publish(sm, sid, source)
    assert [c["type"] for c in source.created] == ["Story"]  # no second epic
    assert result["created"]["epic"] == "PAY-77" and source.created[0]["parent"] == "PAY-77"


def test_a_first_publish_does_not_search_for_orphans(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft(stories=[_story("s1")]))
    _publish(sm, session["id"], source)
    assert source.searches == []


def test_a_failed_link_stops_the_publish_with_the_stories_kept(project, source):
    sm = StateManager()
    session = _session(sm, project, _draft())

    def refuse(blocker, blocked, *, guard):
        raise TicketWriteError("JIRA refused to link")

    source.link_tickets = refuse
    with pytest.raises(PublishError, match="Linking"):
        _publish(sm, session["id"], source)
    assert len(source.created) == 3
    sm.fail_po_publish(session["id"], "x")
    sm.begin_po_publish(session["id"])
    source.link_tickets = lambda blocker, blocked, *, guard: source.links.append((blocker, blocked))
    result, _ = _publish(sm, session["id"], source)
    assert len(source.created) == 3 and len(source.links) == 1 and len(result["links"]) == 1


# -- what it refuses to do ------------------------------------------------------------------------------------------------


def test_it_needs_a_draft_a_jira_project_and_a_jira_source(project, source, monkeypatch):
    sm = StateManager()
    session = _session(sm, project, _draft(), jira_project_key=None)
    with pytest.raises(PublishError, match="which JIRA project"):
        _publish(sm, session["id"], source)
    empty = sm.create_po_session(project, brief="x", mode="brainstorm")
    with pytest.raises(PublishError, match="no draft"):
        publish_backlog(sm, empty["id"])
    sm.discard_po_session(empty["id"])  # out of the queue, so the next session is the one claimed

    ok = _session(sm, project, _draft())
    source.name = "github"
    with pytest.raises(PublishError, match="only built for JIRA"):
        _publish(sm, ok["id"], source)
    assert source.created == []


# -- suggestions, one ticket at a time -------------------------------------------------------------------------------------


def _gap_session(sm, project_id) -> dict:
    draft = _draft("gap", stories=[], existing=[
        {"key": "PAY-2", "assessment": "partial", "evidence": [], "suggested_edit": "Say three attempts, not two."},
        {"key": "PAY-3", "assessment": "done", "evidence": [], "suggested_edit": None},
    ])
    session = sm.create_po_session(project_id, brief="x", mode="gap", epic_key="PAY-1")
    sm.claim_next_po_session()
    sm.finish_po_turn(session["id"], draft=draft, questions=[], reply="ok", cost_usd=0)
    return sm.get_po_session(session["id"])


def test_a_suggested_edit_is_posted_as_a_comment_and_never_twice(project, source):
    sm = StateManager()
    session = _gap_session(sm, project)
    post_suggestion(sm, session, "PAY-2")
    assert len(source.comments) == 1
    key, body = source.comments[0]
    assert key == "PAY-2" and "Say three attempts, not two." in body and "this ticket was not changed" in body
    assert sm.get_po_session(session["id"])["publish_result"]["comments"] == ["PAY-2"]
    assert source.guards["comment"] == {"po_jira_comment_enabled"}  # the PO's own comment switch, not `jira_write_enabled`

    with pytest.raises(PublishError, match="already posted"):
        post_suggestion(sm, sm.get_po_session(session["id"]), "PAY-2")
    assert len(source.comments) == 1


def test_a_suggestion_needs_a_ticket_with_one_and_the_comment_switch(project, source):
    sm = StateManager()
    session = _gap_session(sm, project)
    with pytest.raises(PublishError, match="no suggested edit for PAY-3"):
        post_suggestion(sm, session, "PAY-3")
    with pytest.raises(PublishError, match="not one of this draft's existing stories"):
        post_suggestion(sm, session, "PAY-9")
    source.comments_enabled = False
    with pytest.raises(PublishError, match="po_jira_comment_enabled"):
        post_suggestion(sm, session, "PAY-2")
    assert sm.get_po_session(session["id"])["publish_result"] is None  # nothing recorded for a post that did not happen
