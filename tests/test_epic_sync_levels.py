"""No database, no JIRA — exercises the level computation directly.

The property that matters: a QA ticket always lands in the final level, no
matter where the JIRA link topology would otherwise put it. Everything else
about the topological sort is unchanged, so these also pin that the dev
ordering still comes out the way it did before QA tickets existed.
"""
from __future__ import annotations

from forge.services.epic_sync import _compute_levels
from forge.services.jira_service import JiraTicket


def _ticket(key: str, *, blocked_by: list[str] | None = None) -> JiraTicket:
    return JiraTicket(
        key=key, summary=key, description="", status="To Do", issue_type="Story", priority=None,
        linked_issues=[{"key": other, "link_type": "is blocked by"} for other in (blocked_by or [])],
    )


def test_unlinked_dev_tickets_share_wave_zero():
    children = [_ticket("A"), _ticket("B")]
    assert _compute_levels(children, {"A": "dev", "B": "dev"}) == [["A", "B"]]


def test_blocked_by_links_produce_sequential_levels():
    children = [_ticket("A"), _ticket("B", blocked_by=["A"]), _ticket("C", blocked_by=["B"])]
    kinds = {"A": "dev", "B": "dev", "C": "dev"}
    assert _compute_levels(children, kinds) == [["A"], ["B"], ["C"]]


def test_qa_ticket_lands_last_despite_having_no_links():
    """The case this exists for: PROJ-3722 has no "is blocked by" links, so
    the plain topological sort would put it in wave 0 alongside the stories
    it is supposed to validate — i.e. it would run before any of them."""
    children = [_ticket("QA-1"), _ticket("DEV-1"), _ticket("DEV-2", blocked_by=["DEV-1"])]
    kinds = {"QA-1": "qa", "DEV-1": "dev", "DEV-2": "dev"}
    assert _compute_levels(children, kinds) == [["DEV-1"], ["DEV-2"], ["QA-1"]]


def test_multiple_qa_tickets_share_one_final_level():
    """They depend on the dev work, not on each other — so they can run
    concurrently rather than being serialized one wave each."""
    children = [_ticket("DEV-1"), _ticket("QA-1"), _ticket("QA-2")]
    kinds = {"DEV-1": "dev", "QA-1": "qa", "QA-2": "qa"}
    assert _compute_levels(children, kinds) == [["DEV-1"], ["QA-1", "QA-2"]]


def test_qa_ticket_is_pulled_out_of_the_topology_not_just_sorted_late():
    """A dev ticket declaring it is blocked BY the QA ticket must not drag
    the QA ticket earlier — the QA ticket's position is not negotiable."""
    children = [_ticket("DEV-1", blocked_by=["QA-1"]), _ticket("QA-1")]
    kinds = {"DEV-1": "dev", "QA-1": "qa"}
    assert _compute_levels(children, kinds) == [["DEV-1"], ["QA-1"]]


def test_epic_of_only_qa_tickets_is_a_single_level():
    children = [_ticket("QA-1")]
    assert _compute_levels(children, {"QA-1": "qa"}) == [["QA-1"]]


def test_no_kinds_supplied_behaves_exactly_as_before():
    """Back-compat: callers that don't classify get the plain topological
    sort, unchanged."""
    children = [_ticket("A"), _ticket("B", blocked_by=["A"])]
    assert _compute_levels(children) == [["A"], ["B"]]


def test_level_order_is_by_issue_key_not_jira_return_order():
    """JIRA's search order is arbitrary and was returning PROJ-3480's
    stories newest-first, so the epic rendered 3485, 3484, 3483, …"""
    children = [_ticket("ENG-5"), _ticket("ENG-3"), _ticket("ENG-4")]
    kinds = dict.fromkeys(("ENG-3", "ENG-4", "ENG-5"), "dev")
    assert _compute_levels(children, kinds) == [["ENG-3", "ENG-4", "ENG-5"]]


def test_level_order_is_natural_not_lexicographic():
    from forge.services.jira_service import issue_key_sort_key

    assert sorted(["ENG-1000", "ENG-999", "ENG-99"], key=issue_key_sort_key) == ["ENG-99", "ENG-999", "ENG-1000"]
    # A key with no trailing number still sorts deterministically.
    assert issue_key_sort_key("NOPE")[0] == "NOPE"
