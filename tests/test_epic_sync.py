"""No real JIRA credentials involved — a fake JiraService stands in, same
pattern as test_jira_mcp_server.py. Exercises epic_sync's own logic: level
computation from issue links, and idempotent/merging writes into
StateManager."""
from __future__ import annotations

import pytest
from jira import JIRAError

import forge.services.epic_sync as epic_sync
from forge.services.jira_service import JiraTicket
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")


def _ticket(
    key: str,
    *,
    links: list[dict] | None = None,
    summary: str = "",
    description: str = "",
    issue_type: str = "Story",
) -> JiraTicket:
    return JiraTicket(
        key=key,
        summary=summary or f"{key} summary",
        description=description,
        status="To Do",
        issue_type=issue_type,
        priority="Medium",
        linked_issues=links or [],
    )


class _FakeJiraService:
    tree_by_root: dict[str, list[JiraTicket]] = {}
    ticket_by_key: dict[str, JiraTicket] = {}
    raise_error: Exception | None = None

    def __init__(self, project_id: int | None = None) -> None:
        self.project_id = project_id

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[JiraTicket]:
        if _FakeJiraService.raise_error is not None:
            raise _FakeJiraService.raise_error
        return _FakeJiraService.tree_by_root[root_key]

    def get_ticket(self, issue_key: str) -> JiraTicket:
        if _FakeJiraService.raise_error is not None:
            raise _FakeJiraService.raise_error
        return _FakeJiraService.ticket_by_key[issue_key]


@pytest.fixture(autouse=True)
def _patch_jira(monkeypatch: pytest.MonkeyPatch):
    _FakeJiraService.tree_by_root = {}
    _FakeJiraService.ticket_by_key = {}
    _FakeJiraService.raise_error = None
    monkeypatch.setattr(epic_sync, "JiraService", _FakeJiraService)


def test_sync_creates_epic_paused_by_default(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2")]

    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    assert StateManager().get_epic("ENG-1")["manual_gate"] is True


def test_resync_does_not_reset_manual_gate(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    sm.set_epic_manual_gate("ENG-1", False)  # user resumed

    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2"), _ticket("ENG-4")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    assert sm.get_epic("ENG-1")["manual_gate"] is False


def test_sync_creates_epic_and_stories_flat_when_no_links(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1", summary="The epic", description="Epic description"),
        _ticket("ENG-2", description="Story description"),
        _ticket("ENG-3"),
    ]

    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    epic = sm.get_epic("ENG-1")
    assert epic["project_id"] == project_id
    assert epic["repo"] is None  # syncing tickets and picking a folder are independent steps
    assert epic["levels"] == [["ENG-2", "ENG-3"]]
    assert epic["summary"] == "The epic"
    assert epic["description"] == "Epic description"

    story = sm.get_story("ENG-2")
    assert story["epic_key"] == "ENG-1"
    assert story["wave"] == 0
    assert story["summary"] == "ENG-2 summary"
    assert story["description"] == "Story description"


def test_set_epic_repo_after_sync(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    repo_id = sm.add_project_repo(project_id, "acme/widgets")
    sm.set_epic_project_repo("ENG-1", repo_id)

    assert sm.get_epic("ENG-1")["repo"] == "acme/widgets"


def test_sync_orders_levels_by_blocking_links(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1"),
        _ticket("ENG-2", links=[{"key": "ENG-3", "link_type": "is blocked by"}]),
        _ticket("ENG-3"),
    ]

    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    epic = sm.get_epic("ENG-1")
    assert epic["levels"] == [["ENG-3"], ["ENG-2"]]
    assert sm.get_story("ENG-2")["wave"] == 1
    assert sm.get_story("ENG-3")["wave"] == 0


def test_sync_handles_dependency_cycle_without_hanging(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1"),
        _ticket("ENG-2", links=[{"key": "ENG-3", "link_type": "is blocked by"}]),
        _ticket("ENG-3", links=[{"key": "ENG-2", "link_type": "is blocked by"}]),
    ]

    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    levels = sm.get_epic("ENG-1")["levels"]
    assert sorted(key for level in levels for key in level) == ["ENG-2", "ENG-3"]


def test_resync_picks_up_new_ticket_without_resetting_existing_story(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    sm.set_story_status("ENG-2", "merged")

    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2"), _ticket("ENG-4")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    assert sm.get_story("ENG-2")["status"] == "merged"  # untouched by re-sync
    assert sm.get_story("ENG-4") is not None
    assert sm.get_epic("ENG-1")["levels"] == [["ENG-2", "ENG-4"]]


def test_resync_under_different_project_raises(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    other_project_id = StateManager().create_project("Other")
    with pytest.raises(ValueError, match="different project"):
        epic_sync.sync_epic_from_jira("ENG-1", project_id=other_project_id)


def test_sync_propagates_not_found_as_jira_error(project_id):
    _FakeJiraService.raise_error = JIRAError("no such issue", status_code=404)

    with pytest.raises(JIRAError):
        epic_sync.sync_epic_from_jira("ENG-404", project_id=project_id)


def test_sync_story_from_jira_refreshes_prose_only(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1"),
        _ticket("ENG-2", summary="Old summary", description="Old description"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    sm.set_story_status("ENG-2", "dispatched")
    sm.set_story_kind("ENG-2", "qa", reason="manual")
    sm.set_story_wave("ENG-2", 3)

    _FakeJiraService.ticket_by_key["ENG-2"] = _ticket(
        "ENG-2", summary="New summary", description="New description",
    )
    refreshed = epic_sync.sync_story_from_jira("ENG-2")

    assert refreshed["summary"] == "New summary"
    assert refreshed["description"] == "New description"
    assert refreshed["status"] == "dispatched"
    assert refreshed["kind"] == "qa"
    assert refreshed["kind_reason"] == "manual"
    assert refreshed["wave"] == 3


def test_sync_epic_fields_from_jira_refreshes_prose_only(project_id):
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1", summary="Old epic", description="Old epic body"),
        _ticket("ENG-2"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    sm.set_epic_phase("ENG-1", "building")
    sm.set_epic_manual_gate("ENG-1", True)

    _FakeJiraService.ticket_by_key["ENG-1"] = _ticket(
        "ENG-1", summary="New epic", description="New epic body",
    )
    refreshed = epic_sync.sync_epic_fields_from_jira("ENG-1")

    assert refreshed["summary"] == "New epic"
    assert refreshed["description"] == "New epic body"
    assert refreshed["phase"] == "building"
    assert refreshed["manual_gate"] is True
    assert refreshed["levels"] == [["ENG-2"]]
    assert sm.get_story("ENG-2")["summary"] == "ENG-2 summary"  # children untouched


def test_sync_story_from_jira_unknown_story_raises(project_id):
    with pytest.raises(ValueError, match="No such story"):
        epic_sync.sync_story_from_jira("ENG-999")


# --- story kinds -----------------------------------------------------------
# The fixture below is PROJ-3480 as it actually exists in JIRA: six children,
# all `issuetype: Story`, one of which is a validation ticket that says so
# only in its prose. Nothing structural separates it from its siblings.

_PROJ_3480 = [
    ("PROJ-3480", "[Shop App][Checkout] Multi-item type selection in the full checkout flow"),
    ("PROJ-3481", "[Shop App] Select multiple item types on the search form"),
    ("PROJ-3482", "[Shop App] Search and retrieve offers for a multi-item order"),
    ("PROJ-3483", "[Shop App] View multi-item pricing on the results list and details"),
    ("PROJ-3484", "[Shop App] Complete booking for multiple item types through review and acknowledgement"),
    ("PROJ-3485", "[Shop App] Handle multi-item errors and persist selections"),
    ("PROJ-3722", "[Shop App] Validate implemented end-to-end multi-item flow"),
]


def _sync_proj_3480(project_id: int) -> StateManager:
    _FakeJiraService.tree_by_root["PROJ-3480"] = [
        _ticket(key, summary=summary,
                description=("perform an independent end-to-end validation" if key == "PROJ-3722" else "Implement it."))
        for key, summary in _PROJ_3480
    ]
    epic_sync.sync_epic_from_jira("PROJ-3480", project_id=project_id)
    return StateManager()


def test_sync_classifies_proj_3722_as_qa_and_the_rest_as_dev(project_id):
    sm = _sync_proj_3480(project_id)
    kinds = {s["jira_key"]: s["kind"] for s in sm.list_stories_for_epic("PROJ-3480")}
    assert kinds == {
        "PROJ-3481": "dev", "PROJ-3482": "dev", "PROJ-3483": "dev",
        "PROJ-3484": "dev", "PROJ-3485": "dev", "PROJ-3722": "qa",
    }
    assert sm.get_story("PROJ-3722")["kind_reason"] == "lead_token:validate+scope_term"


def test_sync_puts_the_qa_ticket_in_its_own_final_wave(project_id):
    """None of PROJ-3480's tickets carry blocking links, so without the
    QA-last rule all six would share wave 0 and the validation ticket would
    run alongside the code it validates."""
    sm = _sync_proj_3480(project_id)
    levels = sm.get_epic("PROJ-3480")["levels"]
    assert levels == [["PROJ-3481", "PROJ-3482", "PROJ-3483", "PROJ-3484", "PROJ-3485"], ["PROJ-3722"]]
    assert sm.get_story("PROJ-3722")["wave"] == 1
    assert sm.get_story("PROJ-3481")["wave"] == 0


def test_relevel_moves_a_reclassified_story_and_preserves_dev_topology(project_id):
    sm = _sync_proj_3480(project_id)
    sm.set_story_kind("PROJ-3485", "qa")

    levels = epic_sync.relevel_epic("PROJ-3480")

    assert levels == [["PROJ-3481", "PROJ-3482", "PROJ-3483", "PROJ-3484"], ["PROJ-3485", "PROJ-3722"]]
    assert sm.get_story("PROJ-3485")["wave"] == 1


def test_relevel_puts_a_qa_to_dev_reclassification_in_a_final_dev_wave(project_id):
    """Its original position in the topology isn't recoverable, so it goes
    last among the dev work — always dependency-safe."""
    sm = _sync_proj_3480(project_id)
    sm.set_story_kind("PROJ-3722", "dev")

    levels = epic_sync.relevel_epic("PROJ-3480")

    assert levels == [
        ["PROJ-3481", "PROJ-3482", "PROJ-3483", "PROJ-3484", "PROJ-3485"],
        ["PROJ-3722"],
    ]
    assert sm.get_story("PROJ-3722")["kind"] == "dev"


def test_relevel_drops_the_qa_level_entirely_when_no_qa_stories_remain(project_id):
    sm = _sync_proj_3480(project_id)
    sm.set_story_kind("PROJ-3722", "dev")
    epic_sync.relevel_epic("PROJ-3480")
    # Now reclassify it back and away again to prove no empty level lingers.
    sm.set_story_kind("PROJ-3722", "qa")
    assert epic_sync.relevel_epic("PROJ-3480")[-1] == ["PROJ-3722"]
    sm.set_story_kind("PROJ-3722", "dev")
    levels = epic_sync.relevel_epic("PROJ-3480")
    assert all(level for level in levels)  # no empty levels
    assert sorted(k for level in levels for k in level) == sorted(key for key, _ in _PROJ_3480[1:])


# --- hierarchy -------------------------------------------------------------
# PROJ-3480's real shape: 6 top-level stories, 25 sub-tasks beneath five of
# them. get_ticket_tree returns all 31 in one flat breadth-first list, so
# the split has to come from parent_key, not from list position.


def _subtask(key: str, *, parent: str, summary: str = "") -> JiraTicket:
    ticket = _ticket(key, summary=summary or f"{key} sub-task")
    ticket.issue_type = "Sub-task"
    ticket.parent_key = parent
    return ticket


def _story_under(key: str, epic: str, *, summary: str = "") -> JiraTicket:
    ticket = _ticket(key, summary=summary or f"{key} summary")
    ticket.parent_key = epic
    return ticket


def _sync_hierarchy(project_id: int) -> StateManager:
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1", summary="The epic"),
        _story_under("ENG-2", "ENG-1", summary="Build the form"),
        _story_under("ENG-3", "ENG-1", summary="Wire the API"),
        _subtask("ENG-20", parent="ENG-2", summary="[New] SelectedLine model"),
        _subtask("ENG-21", parent="ENG-2", summary="[Extend] Params for lines list"),
        _subtask("ENG-30", parent="ENG-3", summary="[Extend] Request mapper"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)
    return StateManager()


def test_subtasks_are_stored_with_their_parent_not_as_epic_children(project_id):
    sm = _sync_hierarchy(project_id)
    parents = {s["jira_key"]: s["parent_key"] for s in sm.list_stories_for_epic("ENG-1")}
    assert parents == {
        "ENG-2": None, "ENG-3": None,
        "ENG-20": "ENG-2", "ENG-21": "ENG-2", "ENG-30": "ENG-3",
    }


def test_only_top_level_stories_enter_the_epics_levels(project_id):
    """The reconciler only ever iterates `levels`, so keeping sub-tasks out
    of it is what actually prevents them being dispatched."""
    sm = _sync_hierarchy(project_id)
    levels = sm.get_epic("ENG-1")["levels"]
    assert levels == [["ENG-2", "ENG-3"]]
    assert not {"ENG-20", "ENG-21", "ENG-30"} & {k for level in levels for k in level}


def test_subtasks_are_marked_skipped_so_nothing_dispatches_them(project_id):
    sm = _sync_hierarchy(project_id)
    assert sm.get_story("ENG-20")["status"] == "skipped"
    assert sm.get_story("ENG-2")["status"] == "pending"


def test_subtasks_inherit_their_parents_kind(project_id):
    """A sub-task is never routed on its own, so classifying it separately
    would produce a value that means nothing and could contradict its
    parent's."""
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1"),
        _story_under("ENG-2", "ENG-1", summary="Validate the end-to-end flow"),
        _subtask("ENG-20", parent="ENG-2", summary="Build the checklist"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    assert sm.get_story("ENG-2")["kind"] == "qa"
    assert sm.get_story("ENG-20")["kind"] == "qa"
    assert sm.get_story("ENG-20")["kind_reason"] == "inherited_from_parent"


def test_subtasks_share_their_parents_wave(project_id):
    sm = _sync_hierarchy(project_id)
    assert sm.get_story("ENG-20")["wave"] == sm.get_story("ENG-2")["wave"]


def test_list_subtasks_returns_only_that_storys_children(project_id):
    sm = _sync_hierarchy(project_id)
    assert [s["jira_key"] for s in sm.list_subtasks("ENG-2")] == ["ENG-20", "ENG-21"]
    assert [s["jira_key"] for s in sm.list_subtasks("ENG-3")] == ["ENG-30"]
    assert sm.list_subtasks("ENG-20") == []


def test_ticket_with_no_parent_field_is_treated_as_top_level(project_id):
    """Some JIRA projects link stories to an epic via the legacy Epic Link
    custom field, so `parent` comes back empty. Those must not be mistaken
    for sub-tasks."""
    _FakeJiraService.tree_by_root["ENG-1"] = [_ticket("ENG-1"), _ticket("ENG-2"), _ticket("ENG-3")]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    assert sm.get_epic("ENG-1")["levels"] == [["ENG-2", "ENG-3"]]
    assert sm.get_story("ENG-2")["parent_key"] is None


def test_orphan_subtask_is_kept_as_top_level_rather_than_dropped(project_id):
    """A sub-task whose parent isn't in the synced set would otherwise
    vanish silently. Planning an orphan is recoverable; losing it is not."""
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1"),
        _story_under("ENG-2", "ENG-1"),
        _subtask("ENG-99", parent="ENG-NOT-SYNCED"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    assert sm.get_story("ENG-99") is not None
    assert "ENG-99" in {k for level in sm.get_epic("ENG-1")["levels"] for k in level}


def test_qa_ticket_still_lands_last_when_stories_have_subtasks(project_id):
    """The QA-last rule and the hierarchy split have to compose."""
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1"),
        _story_under("ENG-2", "ENG-1", summary="Build the form"),
        _subtask("ENG-20", parent="ENG-2"),
        _story_under("ENG-9", "ENG-1", summary="Validate the end-to-end flow"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    assert sm.get_epic("ENG-1")["levels"] == [["ENG-2"], ["ENG-9"]]
    assert sm.get_story("ENG-20")["wave"] == 0


# --- story-level sync ------------------------------------------------------
# "Add ticket" accepts a lone Story/Task or an Epic. Non-epic roots with no
# story-level children become the sole work unit; empty Epics wait for kids.


def test_lone_story_is_the_sole_work_unit(project_id):
    _FakeJiraService.tree_by_root["ENG-50"] = [
        _ticket("ENG-50", summary="Implement the widget", description="Do the thing."),
    ]
    epic_sync.sync_epic_from_jira("ENG-50", project_id=project_id)

    sm = StateManager()
    assert sm.get_epic("ENG-50")["levels"] == [["ENG-50"]]
    story = sm.get_story("ENG-50")
    assert story is not None
    assert story["epic_key"] == "ENG-50"
    assert story["parent_key"] is None
    assert story["kind"] == "dev"
    assert story["wave"] == 0


def test_empty_epic_has_no_work_units(project_id):
    _FakeJiraService.tree_by_root["ENG-EPIC"] = [
        _ticket("ENG-EPIC", summary="Future epic", issue_type="Epic"),
    ]
    epic_sync.sync_epic_from_jira("ENG-EPIC", project_id=project_id)

    sm = StateManager()
    assert sm.get_epic("ENG-EPIC")["levels"] == []
    assert sm.get_story("ENG-EPIC") is None


def test_story_with_only_subtasks_uses_root_as_work_unit(project_id):
    _FakeJiraService.tree_by_root["ENG-100"] = [
        _ticket("ENG-100", summary="Build the form"),
        _subtask("ENG-101", parent="ENG-100", summary="[New] SelectedLine model"),
        _subtask("ENG-102", parent="ENG-100", summary="[Extend] Params for lines list"),
    ]
    epic_sync.sync_epic_from_jira("ENG-100", project_id=project_id)

    sm = StateManager()
    assert sm.get_epic("ENG-100")["levels"] == [["ENG-100"]]
    assert sm.get_story("ENG-100")["parent_key"] is None
    assert sm.get_story("ENG-101")["parent_key"] == "ENG-100"
    assert sm.get_story("ENG-102")["parent_key"] == "ENG-100"
    assert sm.get_story("ENG-101")["status"] == "skipped"
    assert sm.get_story("ENG-102")["status"] == "skipped"
    assert sm.get_story("ENG-101")["wave"] == 0


def test_resync_keeps_a_human_kind_override_out_of_the_dev_waves(project_id):
    """Story.kind is never recomputed on re-sync — and the levels derived
    from it must not be either. A story a human reclassified dev->qa belongs
    in the trailing QA level, and re-classifying its prose on every sweep
    would quietly march it back into a dev wave while the column still said
    "qa"."""
    _FakeJiraService.tree_by_root["ENG-1"] = [
        _ticket("ENG-1", issue_type="Epic"),
        _ticket("ENG-2", summary="Build the export endpoint"),
        _ticket("ENG-3", summary="Build the import endpoint"),
    ]
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    sm = StateManager()
    assert sm.get_epic("ENG-1")["levels"] == [["ENG-2", "ENG-3"]]

    sm.set_story_kind("ENG-3", "qa", reason="manual")
    epic_sync.relevel_epic("ENG-1")
    assert sm.get_epic("ENG-1")["levels"] == [["ENG-2"], ["ENG-3"]]

    # A later sweep re-walks the epic; ENG-3's prose still reads like dev work.
    epic_sync.sync_epic_from_jira("ENG-1", project_id=project_id)

    assert sm.get_story("ENG-3")["kind"] == "qa"
    assert sm.get_epic("ENG-1")["levels"] == [["ENG-2"], ["ENG-3"]]
