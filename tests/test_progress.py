"""forge/tools/progress.py — the dev agent's live checklist, plus the
`update_progress_tool` that maintains it and the loop hook that re-shows
it. The mechanism exists because the idle-turn guard could detect drift but
not correct it; what makes it work is that the checklist is re-sent every
turn while staying out of the history.
"""
from __future__ import annotations

import pytest

from forge.tools.dev_tools import PROGRESS_TOOLS, READ_ONLY_TOOLS, make_dev_tools
from forge.tools.progress import (
    PROGRESS_STATUSES,
    merge_progress_update,
    render_progress,
    seed_progress_items,
)


def _plan() -> dict:
    return {
        "files_to_create": [{"path": "lib/a.dart", "purpose": "x"}],
        "files_to_modify": [{"path": "/lib/b.dart"}, {"path": "lib/a.dart"}],
    }


def test_seeding_covers_every_planned_file_once_in_plan_order():
    items = seed_progress_items(_plan())

    assert items == [
        {"path": "lib/a.dart", "action": "create", "status": "pending"},
        {"path": "lib/b.dart", "action": "modify", "status": "pending"},
    ]


def test_seeding_an_empty_plan_costs_nothing():
    assert seed_progress_items({}) == []
    assert render_progress([]) == ""


def test_render_shows_status_counts_and_what_is_left():
    items = seed_progress_items(_plan())
    merge_progress_update(items, [{"path": "lib/a.dart", "status": "done"}])

    rendered = render_progress(items)

    assert "[x] create lib/a.dart" in rendered
    assert "[ ] modify lib/b.dart" in rendered
    assert "1/2 done" in rendered
    assert "Still to do: lib/b.dart" in rendered


def test_render_tells_the_model_to_stop_once_everything_is_done():
    """Without this the model has no signal that the checklist is finished
    and keeps looking for work."""
    items = seed_progress_items(_plan())
    merge_progress_update(items, [{"path": p, "status": "done"} for p in ("lib/a.dart", "lib/b.dart")])

    rendered = render_progress(items)

    assert "2/2 done" in rendered
    assert "summarize and stop" in rendered


@pytest.mark.parametrize("status", PROGRESS_STATUSES)
def test_every_declared_status_is_accepted(status):
    items = seed_progress_items(_plan())

    applied, rejected = merge_progress_update(items, [{"path": "lib/a.dart", "status": status}])

    assert (applied, rejected) == (1, [])
    assert items[0]["status"] == status


def test_unplanned_paths_are_rejected_not_added():
    """The checklist mirrors the approved plan. Letting the agent invent
    entries would turn its own bookkeeping into a second, unenforced
    scope."""
    items = seed_progress_items(_plan())

    applied, rejected = merge_progress_update(items, [{"path": "lib/sneaky.dart", "status": "done"}])

    assert applied == 0
    assert rejected == ["lib/sneaky.dart"]
    assert len(items) == 2


def test_unknown_status_is_rejected():
    items = seed_progress_items(_plan())

    applied, rejected = merge_progress_update(items, [{"path": "lib/a.dart", "status": "finished"}])

    assert applied == 0
    assert rejected == ["lib/a.dart"]
    assert items[0]["status"] == "pending"


def test_leading_slash_is_normalized_so_the_models_path_still_matches():
    items = seed_progress_items(_plan())

    applied, _ = merge_progress_update(items, [{"path": "/lib/b.dart", "status": "doing"}])

    assert applied == 1


# --- the tool ----------------------------------------------------------


def _tools(items):
    functions, _ = make_dev_tools(
        worktree_path="/tmp", allowed_paths={"lib/a.dart"}, analyze_targets=[],
        allow_test_run=False, planned_test_files=[], progress_items=items,
    )
    return functions


def test_tool_applies_updates_and_echoes_the_checklist():
    items = seed_progress_items(_plan())

    result = _tools(items)["update_progress_tool"]([{"path": "lib/a.dart", "status": "done"}])

    assert items[0]["status"] == "done"
    assert "Updated 1" in result
    # Echoing the board back means the model sees the effect immediately,
    # not only on the next turn's reminder.
    assert "PROGRESS CHECKLIST" in result


def test_tool_reports_rejections_with_the_allowed_statuses():
    items = seed_progress_items(_plan())

    result = _tools(items)["update_progress_tool"]([{"path": "nope.dart", "status": "done"}])

    assert "Rejected 1" in result
    assert "pending" in result and "doing" in result and "done" in result


def test_tool_without_a_store_is_a_harmless_noop():
    """The schema is always offered so the model never gets an
    unknown-tool error for calling what the prompt told it to call."""
    functions, _ = make_dev_tools(
        worktree_path="/tmp", allowed_paths=set(), analyze_targets=[],
        allow_test_run=False, planned_test_files=[], progress_items=None,
    )

    assert "not enabled" in functions["update_progress_tool"]([{"path": "a", "status": "done"}])


def test_updating_the_checklist_is_not_implementation_progress():
    """Counting it would hand the model a way to reset the idle-turn budget
    forever without ever touching a file."""
    assert "update_progress_tool" not in PROGRESS_TOOLS
    # It mutates shared state, so it must not run concurrently either.
    assert "update_progress_tool" not in READ_ONLY_TOOLS
