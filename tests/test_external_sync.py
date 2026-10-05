"""Reconciling the board with its source (forge/services/external_sync.py):
what a re-walk does to a ticket's content and to a closed or deleted one.

A fake source stands in for JIRA — no credentials, no network."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import forge.services.epic_sync as epic_sync
import forge.services.jira_filter as jira_filter
from forge.services.external_sync import CLOSED_EXTERNALLY, ON_HOLD_IN_SOURCE
from forge.services.jira_service import JiraTicket
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")


def _t(key, *, summary=None, description="", parent=None, issue_type="Story", closed=False, status="To Do") -> JiraTicket:
    return JiraTicket(
        key=key, summary=summary if summary is not None else f"{key} summary", description=description,
        status=status, issue_type=issue_type, priority=None, parent_key=parent, closed=closed,
    )


class _FakeSource:
    name = "jira"
    max_tree_tickets: int | None = None
    tree: dict[str, list[JiraTicket]] = {}
    missing: set[str] = set()
    missing_error: Exception | None = None
    search_results: list[JiraTicket] = []

    def __init__(self, project_id=None):
        self.project_id = project_id

    def normalize_key(self, raw):
        return raw

    def get_ticket_tree(self, root_key, max_tickets=None):
        return list(_FakeSource.tree[root_key])

    def get_ticket(self, key):
        return next(t for tree in _FakeSource.tree.values() for t in tree if t.key == key)

    def is_missing(self, key):
        if _FakeSource.missing_error is not None:
            raise _FakeSource.missing_error
        return key in _FakeSource.missing

    def compile_filter(self, ticket_filter):
        return jira_filter.build_jql(ticket_filter)

    def search_tickets(self, query, max_results=50):
        return _FakeSource.search_results


@pytest.fixture(autouse=True)
def _fake_source(monkeypatch: pytest.MonkeyPatch):
    _FakeSource.tree, _FakeSource.missing, _FakeSource.missing_error = {}, set(), None
    _FakeSource.max_tree_tickets, _FakeSource.search_results = None, []
    factory = lambda project_id: _FakeSource(project_id)  # noqa: E731
    monkeypatch.setattr(epic_sync, "get_ticket_source", factory)
    monkeypatch.setattr(jira_filter, "get_ticket_source", factory)


def _epic(project_id: int, *children: JiraTicket) -> None:
    _FakeSource.tree["ENG-1"] = [_t("ENG-1", issue_type="Epic"), *children]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)


def _resync(project_id: int):
    return epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)


def _edit(key: str, **fields) -> None:
    tree = _FakeSource.tree["ENG-1"]
    _FakeSource.tree["ENG-1"] = [
        JiraTicket(**{**t.__dict__, **fields}) if t.key == key else t for t in tree
    ]


def _start(sm: StateManager, key: str, status: str) -> None:
    sm.set_story_status(key, status)
    sm.set_story_thread_id(key, key)


# ---------------------------------------------------------------- content


def test_a_story_that_has_not_started_takes_the_new_text_straight_away(project_id):
    _epic(project_id, _t("ENG-2", summary="Old title", description="old body", parent="ENG-1"))
    _edit("ENG-2", summary="New title", description="new body")

    report = _resync(project_id)

    story = StateManager().get_story("ENG-2")
    assert (story["summary"], story["description"]) == ("New title", "new body")
    assert story["external_pending"] is None
    assert report.applied == ["ENG-2"] and report.flagged == []


def test_a_planned_story_holds_the_change_and_keeps_its_text(project_id):
    _epic(project_id, _t("ENG-2", summary="Old title", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "awaiting_plan_approval")
    _edit("ENG-2", summary="New title", description="added scope")

    report = _resync(project_id)

    story = sm.get_story("ENG-2")
    assert story["summary"] == "Old title"
    assert story["external_pending"]["summary"] == "New title"
    assert story["external_pending"]["description"] == "added scope"
    assert story["external_pending"]["reasons"] == ["edited in the source"]
    assert story["external_changed_at"] is not None
    assert report.flagged == ["ENG-2"] and report.applied == []


def test_a_held_change_is_not_re_flagged_on_every_sweep(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "dispatched")
    _edit("ENG-2", summary="Changed")
    _resync(project_id)
    first = sm.get_story("ENG-2")["external_changed_at"]

    _resync(project_id)

    assert sm.get_story("ENG-2")["external_changed_at"] == first


def test_the_source_going_back_to_the_resolved_text_drops_the_held_change(project_id):
    _epic(project_id, _t("ENG-2", summary="Original", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "dispatched")
    _edit("ENG-2", summary="Edited")
    _resync(project_id)
    assert sm.get_story("ENG-2")["external_pending"] is not None

    _edit("ENG-2", summary="Original")
    _resync(project_id)

    assert sm.get_story("ENG-2")["external_pending"] is None


def test_a_finished_story_and_an_epic_are_updated_in_place(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    sm.set_story_status("ENG-2", "merged")
    _edit("ENG-2", summary="Retitled after merge")
    _edit("ENG-1", summary="Epic retitled")

    report = _resync(project_id)

    assert sm.get_story("ENG-2")["summary"] == "Retitled after merge"
    assert sm.get_epic("ENG-1")["summary"] == "Epic retitled"
    assert report.flagged == []


def test_a_subtask_change_holds_a_started_parent_and_updates_the_subtask(project_id):
    _epic(
        project_id,
        _t("ENG-2", parent="ENG-1"),
        _t("ENG-3", summary="Add the file", issue_type="Sub-task", parent="ENG-2"),
    )
    sm = StateManager()
    _start(sm, "ENG-2", "dispatched")
    _edit("ENG-3", summary="Add a different file")

    report = _resync(project_id)

    assert sm.get_story("ENG-3")["summary"] == "Add a different file"
    parent = sm.get_story("ENG-2")
    assert parent["external_pending"]["reasons"] == ["sub-task changed: ENG-3"]
    assert "summary" not in parent["external_pending"]
    assert report.flagged == ["ENG-2"]
    # ...and it stays held on the next sweep, when the sub-task no longer looks changed.
    _resync(project_id)
    assert sm.get_story("ENG-2")["external_pending"] is not None


def test_an_existing_row_with_no_baseline_is_confirmed_not_flagged_when_it_matches(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "dispatched")
    from forge.db.engine import session_scope
    from forge.db.models import Story

    with session_scope() as session:
        session.get(Story, "ENG-2").external_hash = None  # as every pre-existing row is

    report = _resync(project_id)

    story = sm.get_story("ENG-2")
    assert story["external_hash"] is not None and story["external_pending"] is None
    assert report.changed() is False


# ------------------------------------------------------- resolving a held change


def _held(project_id, status="awaiting_plan_approval"):
    _epic(project_id, _t("ENG-2", summary="Old", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", status)
    _edit("ENG-2", summary="New")
    _resync(project_id)
    return sm


def test_accept_applies_the_text_and_leaves_the_story_where_it_is(project_id):
    sm = _held(project_id)

    sm.resolve_external_change("ENG-2", "accept")

    story = sm.get_story("ENG-2")
    assert (story["summary"], story["status"], story["external_pending"]) == ("New", "awaiting_plan_approval", None)


def test_replan_applies_the_text_and_sends_a_planned_story_back_to_pending(project_id):
    sm = _held(project_id)

    sm.resolve_external_change("ENG-2", "replan")

    story = sm.get_story("ENG-2")
    assert (story["summary"], story["status"], story["gate_approved"]) == ("New", "pending", False)


def test_replan_is_refused_while_work_is_in_flight(project_id):
    sm = _held(project_id, status="ready_to_merge")

    with pytest.raises(ValueError, match="can be re-planned"):
        sm.resolve_external_change("ENG-2", "replan")

    assert sm.get_story("ENG-2")["external_pending"] is not None  # nothing was lost


def test_dismiss_keeps_the_text_and_is_not_flagged_again_until_it_changes_again(project_id):
    sm = _held(project_id)

    sm.resolve_external_change("ENG-2", "dismiss")
    _resync(project_id)

    story = sm.get_story("ENG-2")
    assert (story["summary"], story["external_pending"]) == ("Old", None)

    _edit("ENG-2", summary="Newer")
    _resync(project_id)
    assert sm.get_story("ENG-2")["external_pending"]["summary"] == "Newer"


def test_the_manual_refresh_applies_whatever_the_state_and_clears_the_held_change(project_id):
    sm = _held(project_id, status="dispatched")

    epic_sync.sync_story_from_jira("ENG-2")

    story = sm.get_story("ENG-2")
    assert (story["summary"], story["external_pending"]) == ("New", None)


# ---------------------------------------------------------------- closure


def test_a_story_closed_before_it_started_is_skipped_and_unblocks_the_next_level(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    _edit("ENG-2", closed=True, status="Done")

    report = _resync(project_id)

    story = StateManager().get_story("ENG-2")
    assert (story["status"], story["parked_reason"], story["external_closed"], story["external_status"]) == (
        "skipped", CLOSED_EXTERNALLY, True, "Done",
    )
    assert report.closed == ["ENG-2"]


def test_a_story_closed_while_in_flight_is_flagged_but_left_to_finish(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "dispatched")
    _edit("ENG-2", closed=True, status="Done")

    report = _resync(project_id)

    story = sm.get_story("ENG-2")
    assert (story["status"], story["external_closed"]) == ("dispatched", True)
    assert report.closed_in_flight == ["ENG-2"] and report.closed == []


def test_reopening_restores_only_a_skip_that_sync_made(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"), _t("ENG-3", parent="ENG-1"))
    sm = StateManager()
    sm.set_story_status("ENG-3", "skipped")  # skipped by a human, for their own reasons
    _edit("ENG-2", closed=True)
    _edit("ENG-3", closed=True)
    _resync(project_id)
    assert sm.get_story("ENG-2")["status"] == "skipped"

    _edit("ENG-2", closed=False)
    _edit("ENG-3", closed=False)
    report = _resync(project_id)

    assert sm.get_story("ENG-2")["status"] == "pending"
    assert sm.get_story("ENG-3")["status"] == "skipped"
    assert sorted(report.reopened) == ["ENG-2", "ENG-3"]


# ---------------------------------------------------------------- on hold


def test_a_ticket_on_hold_when_added_is_set_aside_and_does_not_block_the_epic(project_id):
    """One story: On Hold in JIRA (filed under To Do, so not closed), and
    forge planned it anyway."""
    _epic(project_id, _t("ENG-2", parent="ENG-1", status="On Hold"), _t("ENG-3", parent="ENG-1"))

    sm = StateManager()
    story = sm.get_story("ENG-2")
    assert (story["status"], story["parked_reason"], story["external_status"]) == ("skipped", ON_HOLD_IN_SOURCE, "On Hold")
    assert story["external_closed"] is False
    assert sm.get_story("ENG-3")["status"] == "pending"


def test_a_plan_waiting_for_approval_is_set_aside_when_the_ticket_goes_on_hold(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "awaiting_plan_approval")
    _edit("ENG-2", status="on hold")  # any case

    report = _resync(project_id)

    assert (sm.get_story("ENG-2")["status"], sm.get_story("ENG-2")["parked_reason"]) == ("skipped", ON_HOLD_IN_SOURCE)
    assert report.on_hold == ["ENG-2"]


def test_a_run_under_way_when_the_ticket_goes_on_hold_finishes(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    _start(sm, "ENG-2", "dispatched")
    _edit("ENG-2", status="On Hold")

    report = _resync(project_id)

    assert sm.get_story("ENG-2")["status"] == "dispatched"
    assert report.on_hold == []


def test_off_hold_it_comes_back_and_closed_it_stays_closed(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1", status="On Hold"), _t("ENG-3", parent="ENG-1", status="On Hold"))
    sm = StateManager()

    _edit("ENG-2", status="In Progress")
    _edit("ENG-3", status="Done", closed=True)
    report = _resync(project_id)

    assert sm.get_story("ENG-2")["status"] == "pending" and report.off_hold == ["ENG-2"]
    assert (sm.get_story("ENG-3")["status"], sm.get_story("ENG-3")["parked_reason"]) == ("skipped", CLOSED_EXTERNALLY)
    assert report.closed == ["ENG-3"]


def test_which_statuses_mean_on_hold_is_a_project_setting(project_id):
    from forge.config.store import set_project_config

    set_project_config(project_id, "ticket_hold_statuses", "Waiting for PO")
    _epic(project_id, _t("ENG-2", parent="ENG-1", status="Waiting for PO"), _t("ENG-3", parent="ENG-1", status="On Hold"))

    sm = StateManager()
    assert sm.get_story("ENG-2")["parked_reason"] == ON_HOLD_IN_SOURCE
    assert sm.get_story("ENG-3")["status"] == "pending"


def test_the_epic_pull_request_says_which_ticket_is_on_hold(project_id):
    import forge.daemon.main as daemon

    _epic(project_id, _t("ENG-2", parent="ENG-1", status="On Hold"))
    sm = StateManager()

    assert "**ENG-2** ENG-2 summary — On Hold in the source; not in this pull request" in daemon._epic_pr_body(sm, sm.get_epic("ENG-1"))


def test_a_closed_epic_is_put_on_hold(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    sm.set_epic_manual_gate("ENG-1", False)
    _FakeSource.tree["ENG-1"][0] = _t("ENG-1", issue_type="Epic", closed=True, status="Done")

    _resync(project_id)

    epic = sm.get_epic("ENG-1")
    assert (epic["manual_gate"], epic["external_closed"]) == (True, True)


# ---------------------------------------------------------------- gone


def test_a_story_the_source_confirms_deleted_is_treated_as_closed(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"), _t("ENG-3", parent="ENG-1"))
    _FakeSource.tree["ENG-1"] = [t for t in _FakeSource.tree["ENG-1"] if t.key != "ENG-3"]
    _FakeSource.missing = {"ENG-3"}

    _resync(project_id)

    story = StateManager().get_story("ENG-3")
    assert (story["status"], story["external_closed"], story["external_status"]) == (
        "skipped", True, "deleted or no longer visible",
    )


def test_a_story_that_merely_moved_or_could_not_be_checked_is_left_alone(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"), _t("ENG-3", parent="ENG-1"))
    _FakeSource.tree["ENG-1"] = [t for t in _FakeSource.tree["ENG-1"] if t.key not in ("ENG-2", "ENG-3")]
    _FakeSource.missing = set()  # both still exist — the source just did not return them here
    _resync(project_id)
    assert {StateManager().get_story(k)["status"] for k in ("ENG-2", "ENG-3")} == {"pending"}

    _FakeSource.missing_error = RuntimeError("JIRA is having a moment")
    _resync(project_id)
    assert {StateManager().get_story(k)["status"] for k in ("ENG-2", "ENG-3")} == {"pending"}


def test_a_walk_that_hit_the_size_cap_never_reads_absence_as_deletion(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"), _t("ENG-3", parent="ENG-1"))
    _FakeSource.tree["ENG-1"] = [t for t in _FakeSource.tree["ENG-1"] if t.key != "ENG-3"]
    _FakeSource.missing = {"ENG-3"}
    _FakeSource.max_tree_tickets = 2  # the walk returned two — as many as it was allowed

    _resync(project_id)

    assert StateManager().get_story("ENG-3")["status"] == "pending"


def test_a_native_ticket_is_never_touched_by_a_sync(project_id):
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    sm = StateManager()
    sm.create_story("NATIVE-1", epic_key="ENG-1", wave=0, summary="Made on the board")
    _FakeSource.missing = {"NATIVE-1"}

    _resync(project_id)

    assert sm.get_story("NATIVE-1")["status"] == "pending"


# ---------------------------------------------------------------- sweep and route


def test_the_sweep_re_reads_an_epic_the_filter_no_longer_matches(project_id):
    """The filter selects on status, so an epic that finishes drops out of
    the results — exactly the epic whose closure the sweep has to see."""
    _epic(project_id, _t("ENG-2", parent="ENG-1"))
    StateManager().set_project_ticket_filter(project_id, enabled=True, jira_project_key="ENG")
    _edit("ENG-2", closed=True, status="Done")
    _FakeSource.search_results = []  # nothing matches any more

    result = jira_filter.sync_project_filter(project_id)

    assert result["rechecked"] == ["ENG-1"]
    assert result["changes"]["closed"] == ["ENG-2"]
    assert StateManager().get_story("ENG-2")["status"] == "skipped"


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


def test_the_route_resolves_a_held_change(project_id):
    sm = _held(project_id)
    with _client() as client:
        response = client.post("/api/stories/ENG-2/external-change", json={"action": "accept"})

        assert response.status_code == 200
        assert response.json()["summary"] == "New"
        assert client.post("/api/stories/ENG-2/external-change", json={"action": "accept"}).status_code == 409
    assert sm.get_story("ENG-2")["external_pending"] is None


def test_the_route_refuses_a_replan_with_work_in_flight_and_unknown_stories(project_id):
    _held(project_id, status="ready_to_merge")
    with _client() as client:
        assert client.post("/api/stories/ENG-2/external-change", json={"action": "replan"}).status_code == 409
        assert client.post("/api/stories/NOPE-1/external-change", json={"action": "accept"}).status_code == 404
        assert client.post("/api/stories/ENG-2/external-change", json={"action": "bogus"}).status_code == 422
