"""Filter-based ticket ingestion (forge/services/jira_filter.py).

JQL compilation is pure and needs no DB. The sweep tests use a fake
JiraService — same pattern as tests/test_epic_sync.py — patched on *both*
modules that construct one: jira_filter does the search, and epic_sync
does the per-root tree fetch it delegates to.
"""
from __future__ import annotations

import pytest

import forge.services.epic_sync as epic_sync
import forge.services.jira_filter as jira_filter
from forge.services.jira_service import JiraTicket
from forge.state.manager import StateManager


def _ticket(
    key: str,
    *,
    issue_type: str = "Story",
    parent_key: str | None = None,
    summary: str = "",
    status: str = "To Do",
) -> JiraTicket:
    return JiraTicket(
        key=key,
        summary=summary or f"{key} summary",
        description="",
        status=status,
        issue_type=issue_type,
        priority="Medium",
        parent_key=parent_key,
    )


# ---------------------------------------------------------------------------
# JQL compilation — pure, no DB.
# ---------------------------------------------------------------------------


def test_build_jql_combines_structured_fields():
    jql = jira_filter.build_jql(
        {
            "jira_project_key": "MMA",
            "assignee": "leo@example.com",
            "statuses": ["To Do", "In Progress"],
            "labels": ["forge"],
            "issue_types": ["Story", "Bug"],
        }
    )
    assert jql == (
        'project = "MMA" AND assignee = "leo@example.com" '
        'AND status IN ("To Do", "In Progress") '
        'AND labels IN ("forge") '
        'AND issuetype IN ("Story", "Bug") '
        "ORDER BY created DESC"
    )


def test_build_jql_passes_jql_functions_through_unquoted():
    assert jira_filter.build_jql({"assignee": "currentUser()"}).startswith("assignee = currentUser()")


def test_build_jql_escapes_quotes_in_values():
    jql = jira_filter.build_jql({"jira_project_key": 'we"ird'})
    assert jql.startswith('project = "we\\"ird"')


def test_build_jql_ignores_blank_list_entries():
    jql = jira_filter.build_jql({"jira_project_key": "MMA", "labels": ["", "  "]})
    assert "labels" not in jql


def test_build_jql_wraps_extra_jql_and_moves_its_order_by_to_the_end():
    jql = jira_filter.build_jql(
        {"jira_project_key": "MMA", "extra_jql": "sprint in openSprints() OR labels = hot ORDER BY priority DESC"}
    )
    assert jql == (
        'project = "MMA" AND (sprint in openSprints() OR labels = hot) ORDER BY priority DESC'
    )


def test_build_jql_rejects_an_empty_filter():
    with pytest.raises(ValueError, match="empty"):
        jira_filter.build_jql({"statuses": [], "labels": []})


# ---------------------------------------------------------------------------
# Sweeps — DB-backed.
# ---------------------------------------------------------------------------


class _FakeJiraService:
    search_results: list[JiraTicket] = []
    tree_by_root: dict[str, list[JiraTicket]] = {}
    last_jql: str | None = None
    search_error: Exception | None = None

    def __init__(self, project_id: int | None = None) -> None:
        self.project_id = project_id

    def search_tickets(self, jql: str, max_results: int = 50) -> list[JiraTicket]:
        _FakeJiraService.last_jql = jql
        if _FakeJiraService.search_error is not None:
            raise _FakeJiraService.search_error
        return _FakeJiraService.search_results

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[JiraTicket]:
        return _FakeJiraService.tree_by_root.get(root_key, [_ticket(root_key)])


@pytest.fixture()
def fake_jira(monkeypatch: pytest.MonkeyPatch):
    _FakeJiraService.search_results = []
    _FakeJiraService.tree_by_root = {}
    _FakeJiraService.last_jql = None
    _FakeJiraService.search_error = None
    monkeypatch.setattr(jira_filter, "JiraService", _FakeJiraService)
    monkeypatch.setattr(epic_sync, "JiraService", _FakeJiraService)
    return _FakeJiraService


def _save_filter(project_id: int, **overrides) -> dict:
    payload = {"enabled": True, "jira_project_key": "ENG"}
    payload.update(overrides)
    return StateManager().set_project_ticket_filter(project_id, **payload)


def test_sync_ingests_new_roots_as_paused_epics(project_id, fake_jira):
    fake_jira.search_results = [_ticket("ENG-2"), _ticket("ENG-1")]
    _save_filter(project_id)

    result = jira_filter.sync_project_filter(project_id)

    # Ingest order is issue-key natural order, not JIRA's arbitrary one.
    assert result["synced"] == ["ENG-1", "ENG-2"]
    sm = StateManager()
    assert sm.get_epic("ENG-1")["project_id"] == project_id
    assert sm.get_epic("ENG-1")["manual_gate"] is True
    assert sm.get_epic("ENG-1")["project_repo_id"] is None


def test_sync_skips_subtasks_and_children_of_other_matches(project_id, fake_jira):
    fake_jira.search_results = [
        _ticket("ENG-1", issue_type="Epic"),
        _ticket("ENG-2", parent_key="ENG-1"),
        _ticket("ENG-3", issue_type="Sub-task", parent_key="ENG-2"),
    ]
    fake_jira.tree_by_root["ENG-1"] = [
        _ticket("ENG-1", issue_type="Epic"),
        _ticket("ENG-2", parent_key="ENG-1"),
    ]
    _save_filter(project_id)

    result = jira_filter.sync_project_filter(project_id)

    assert result["synced"] == ["ENG-1"]
    skipped = {s["key"]: s["reason"] for s in result["skipped"]}
    assert "ENG-1" not in skipped
    assert "also matched" in skipped["ENG-2"]
    assert "sub-task" in skipped["ENG-3"]
    # ENG-2 still arrives — as a story under ENG-1, not as an epic of its own.
    assert StateManager().get_epic("ENG-2") is None
    assert StateManager().get_story("ENG-2")["epic_key"] == "ENG-1"


def test_sync_re_walks_an_already_synced_epic_and_picks_up_a_new_child(project_id, fake_jira):
    """The case background ingestion exists for: the epic was ingested last
    week, the team added a story to it since, and nobody re-typed the key
    into "Add ticket"."""
    fake_jira.search_results = [_ticket("ENG-1", issue_type="Epic")]
    fake_jira.tree_by_root["ENG-1"] = [_ticket("ENG-1", issue_type="Epic"), _ticket("ENG-2", parent_key="ENG-1")]
    _save_filter(project_id)
    jira_filter.sync_project_filter(project_id)

    sm = StateManager()
    assert [s["jira_key"] for s in sm.list_stories_for_epic("ENG-1")] == ["ENG-2"]

    # A third ticket appears under the epic in JIRA.
    fake_jira.tree_by_root["ENG-1"] = [
        _ticket("ENG-1", issue_type="Epic"),
        _ticket("ENG-2", parent_key="ENG-1"),
        _ticket("ENG-3", parent_key="ENG-1"),
    ]
    second = jira_filter.sync_project_filter(project_id)

    assert second["synced"] == []  # no *new* epic
    assert second["refreshed"] == ["ENG-1"]
    assert [s["jira_key"] for s in sm.list_stories_for_epic("ENG-1")] == ["ENG-2", "ENG-3"]


def test_re_walking_leaves_human_decisions_and_in_flight_stories_untouched(project_id, fake_jira):
    fake_jira.search_results = [_ticket("ENG-1", issue_type="Epic")]
    fake_jira.tree_by_root["ENG-1"] = [_ticket("ENG-1", issue_type="Epic"), _ticket("ENG-2", parent_key="ENG-1")]
    _save_filter(project_id)
    jira_filter.sync_project_filter(project_id)

    sm = StateManager()
    sm.set_epic_manual_gate("ENG-1", False)  # the human resumed it
    sm.set_story_status("ENG-2", "awaiting_pr_review")  # and it is mid-flight

    fake_jira.tree_by_root["ENG-1"].append(_ticket("ENG-3", parent_key="ENG-1"))
    jira_filter.sync_project_filter(project_id)

    assert sm.get_epic("ENG-1")["manual_gate"] is False
    assert sm.get_story("ENG-2")["status"] == "awaiting_pr_review"
    assert sm.get_story("ENG-3")["status"] == "pending"


def test_re_walk_that_changes_nothing_is_not_reported_as_a_change(project_id, fake_jira):
    fake_jira.search_results = [_ticket("ENG-1", issue_type="Epic")]
    fake_jira.tree_by_root["ENG-1"] = [_ticket("ENG-1", issue_type="Epic"), _ticket("ENG-2", parent_key="ENG-1")]
    _save_filter(project_id)
    jira_filter.sync_project_filter(project_id)

    steady_state = jira_filter.sync_project_filter(project_id)

    assert steady_state["synced"] == []
    assert steady_state["refreshed"] == []
    assert steady_state["skipped"] == []  # re-checked, not skipped


def test_sync_skips_a_ticket_owned_by_another_project(fake_jira, forge_db):
    sm = StateManager()
    owner = sm.create_project("Owner")
    other = sm.create_project("Other")
    fake_jira.search_results = [_ticket("ENG-1")]
    _save_filter(owner)
    jira_filter.sync_project_filter(owner)

    _save_filter(other)
    result = jira_filter.sync_project_filter(other)

    assert result["synced"] == []
    assert "different project" in result["skipped"][0]["reason"]
    assert sm.get_epic("ENG-1")["project_id"] == owner


def test_sync_continues_past_one_failing_ticket(project_id, fake_jira, monkeypatch: pytest.MonkeyPatch):
    fake_jira.search_results = [_ticket("ENG-1"), _ticket("ENG-2")]
    _save_filter(project_id)

    real_sync = jira_filter.sync_epic_from_jira

    def _flaky(jira_key: str, *, project_id: int) -> None:
        if jira_key == "ENG-1":
            raise RuntimeError("boom")
        real_sync(jira_key, project_id=project_id)

    monkeypatch.setattr(jira_filter, "sync_epic_from_jira", _flaky)

    result = jira_filter.sync_project_filter(project_id)

    assert result["synced"] == ["ENG-2"]
    assert result["errors"] == [{"key": "ENG-1", "error": "boom"}]
    assert "ENG-1: boom" in StateManager().get_project_ticket_filter(project_id)["last_error"]


def test_sync_records_bookkeeping_on_the_filter_row(project_id, fake_jira):
    fake_jira.search_results = [_ticket("ENG-1")]
    _save_filter(project_id)

    jira_filter.sync_project_filter(project_id)

    saved = StateManager().get_project_ticket_filter(project_id)
    assert saved["last_matched_count"] == 1
    assert saved["last_synced_count"] == 1
    assert saved["last_error"] is None
    assert saved["last_synced_at"] is not None


def test_search_failure_is_recorded_then_raised(project_id, fake_jira):
    fake_jira.search_error = RuntimeError("jira exploded")
    _save_filter(project_id)

    with pytest.raises(RuntimeError, match="jira exploded"):
        jira_filter.sync_project_filter(project_id)

    assert "jira exploded" in StateManager().get_project_ticket_filter(project_id)["last_error"]


def test_sync_without_a_saved_filter_raises(project_id, fake_jira):
    with pytest.raises(ValueError, match="No ticket filter"):
        jira_filter.sync_project_filter(project_id)


def test_preview_writes_nothing(project_id, fake_jira):
    fake_jira.search_results = [_ticket("ENG-1")]

    preview = jira_filter.preview_filter(project_id, {"jira_project_key": "ENG"})

    assert preview["would_sync"] == ["ENG-1"]
    assert preview["matches"][0]["status"] == "To Do"
    assert StateManager().get_epic("ENG-1") is None
    assert StateManager().get_project_ticket_filter(project_id) is None


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------


def test_filter_round_trips_and_clears(project_id):
    sm = StateManager()
    assert sm.get_project_ticket_filter(project_id) is None

    sm.set_project_ticket_filter(
        project_id, enabled=True, jira_project_key="ENG", assignee="me@example.com", statuses=["To Do"]
    )
    saved = sm.get_project_ticket_filter(project_id)
    assert (saved["jira_project_key"], saved["assignee"], saved["statuses"]) == ("ENG", "me@example.com", ["To Do"])
    assert [f["project_id"] for f in sm.list_enabled_ticket_filters()] == [project_id]

    # Replace, not patch: the dropped assignee is really gone.
    sm.set_project_ticket_filter(project_id, enabled=False, jira_project_key="ENG")
    saved = sm.get_project_ticket_filter(project_id)
    assert saved["assignee"] is None
    assert saved["statuses"] == []
    assert sm.list_enabled_ticket_filters() == []

    assert sm.clear_project_ticket_filter(project_id) is True
    assert sm.get_project_ticket_filter(project_id) is None
    assert sm.clear_project_ticket_filter(project_id) is False
