"""Pure unit tests, no database, no LLM — plan_orchestration is a plain
function over in-memory fixtures. A story is ready once every story it
depends on has merged; independent stories run in parallel; a failure stops
only the stories that depend on it.

`_snapshot` defaults each story's dependencies to "every story in an earlier
level" — the fallback the daemon uses for a row synced before dependencies
were stored — so a test that only passes levels reads the old way; tests
about per-story scheduling pass `depends_on` explicitly."""
from __future__ import annotations

from forge.orchestration.reconciler import (
    Blocked,
    DispatchStory,
    EpicSnapshot,
    MergeIntoEpic,
    NoOp,
    OrchestratorContext,
    ParkForGate,
    ResumeStory,
    StorySnapshot,
    describe_story_action,
    display_epic_phase,
    epic_completion_status,
    plan_orchestration,
)


def _snapshot(
    levels: list[list[str]],
    statuses: dict[str, str],
    thread_ids: dict[str, str] | None = None,
    manual_gate: bool = False,
    depends_on: dict[str, list[str]] | None = None,
) -> EpicSnapshot:
    thread_ids = thread_ids or {}
    earlier: dict[str, list[str]] = {}
    seen: list[str] = []
    for level in levels:
        for key in level:
            earlier[key] = list(seen)
        seen.extend(level)
    deps = earlier if depends_on is None else depends_on
    stories = {
        key: StorySnapshot(
            jira_key=key, status=status, thread_id=thread_ids.get(key), depends_on=tuple(deps.get(key, [])),
        )
        for key, status in statuses.items()
    }
    return EpicSnapshot(jira_key="ENG-1", levels=levels, stories=stories, manual_gate=manual_gate)


def test_no_levels_is_a_noop():
    snapshot = EpicSnapshot(jira_key="ENG-1", levels=[], stories={})
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [NoOp(reason="no_levels")]


def test_dispatch_paused_overrides_everything():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"})
    intents = plan_orchestration(snapshot, OrchestratorContext(dispatch_paused=True))
    assert intents == [NoOp(reason="dispatch_paused")]


def test_manual_gate_overrides_everything_for_this_epic_only():
    gated = _snapshot([["ENG-2"]], {"ENG-2": "pending"}, manual_gate=True)
    intents = plan_orchestration(gated, OrchestratorContext())
    assert intents == [NoOp(reason="manual_gate")]

    # An epic with the gate off is unaffected.
    ungated = _snapshot([["ENG-2"]], {"ENG-2": "pending"})
    intents = plan_orchestration(ungated, OrchestratorContext())
    assert intents == [DispatchStory("ENG-2")]


def test_single_pending_story_dispatches():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"})
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [DispatchStory("ENG-2")]


def test_stories_with_no_unmet_dependencies_dispatch_together_the_rest_wait():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "pending", "ENG-3": "pending", "ENG-4": "pending"},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [
        DispatchStory("ENG-2"), DispatchStory("ENG-3"), NoOp(story_key="ENG-4", reason="waiting_on_dependencies"),
    ]


def test_a_story_waits_only_for_its_own_dependencies_not_the_whole_earlier_level():
    """The point of storing dependencies: ENG-4 needs ENG-2 only, so it
    starts as soon as ENG-2 merges, while ENG-3 is still running."""
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "merged", "ENG-3": "dispatched", "ENG-4": "pending"},
        thread_ids={"ENG-3": "ENG-3"},
        depends_on={"ENG-4": ["ENG-2"]},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext(live_thread_ids=frozenset({"ENG-3"})))
    assert DispatchStory("ENG-4") in intents


def test_a_failure_stops_only_its_dependents_and_independent_work_continues():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4", "ENG-5"]],
        {"ENG-2": "failed", "ENG-3": "merged", "ENG-4": "pending", "ENG-5": "pending"},
        depends_on={"ENG-4": ["ENG-2"], "ENG-5": ["ENG-3"]},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [NoOp(story_key="ENG-4", reason="dependency_failed"), DispatchStory("ENG-5")]


def test_a_failure_stops_dependents_transitively():
    snapshot = _snapshot(
        [["ENG-2"], ["ENG-3"], ["ENG-4"], ["ENG-5"]],
        {"ENG-2": "failed", "ENG-3": "pending", "ENG-4": "pending", "ENG-5": "pending"},
        depends_on={"ENG-3": ["ENG-2"], "ENG-4": ["ENG-3"], "ENG-5": []},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [
        NoOp(story_key="ENG-3", reason="dependency_failed"),
        NoOp(story_key="ENG-4", reason="dependency_failed"),
        DispatchStory("ENG-5"),
    ]


def test_the_epic_fails_only_once_nothing_can_make_progress():
    running = _snapshot(
        [["ENG-2", "ENG-3"]], {"ENG-2": "failed", "ENG-3": "dispatched"}, depends_on={},
    )
    stuck = _snapshot(
        [["ENG-2", "ENG-3"]], {"ENG-2": "failed", "ENG-3": "merged"}, depends_on={},
    )
    assert all(i != NoOp(reason="epic_failed", story_key="ENG-2") for i in plan_orchestration(running, OrchestratorContext()))
    assert epic_completion_status(running) is None
    assert plan_orchestration(stuck, OrchestratorContext()) == [NoOp(reason="epic_failed", story_key="ENG-2")]
    assert epic_completion_status(stuck) == "failed"


def test_a_dependency_outside_the_epic_does_not_hold_a_story_back():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"}, depends_on={"ENG-2": ["GONE-9"]})
    assert plan_orchestration(snapshot, OrchestratorContext()) == [DispatchStory("ENG-2")]


def test_plan_approval_gate_parks_when_unapproved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_plan_approval"})
    intents = plan_orchestration(snapshot, OrchestratorContext(require_plan_approval=True))
    assert intents == [ParkForGate("ENG-2", "plan_approval_gate")]


def test_plan_approval_gate_resumes_once_approved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_plan_approval"})
    ctx = OrchestratorContext(require_plan_approval=True, approved_story_keys=frozenset({"ENG-2"}))
    intents = plan_orchestration(snapshot, ctx)
    assert intents == [ResumeStory("ENG-2")]


def test_plan_approval_gate_off_resumes_without_approval():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_plan_approval"})
    intents = plan_orchestration(snapshot, OrchestratorContext(require_plan_approval=False))
    assert intents == [ResumeStory("ENG-2")]


def test_dispatched_story_with_live_thread_is_a_noop():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "dispatched"}, thread_ids={"ENG-2": "thread-1"})
    ctx = OrchestratorContext(live_thread_ids=frozenset({"thread-1"}))
    intents = plan_orchestration(snapshot, ctx)
    assert intents == [NoOp(story_key="ENG-2", reason="already_running")]


def test_dispatched_story_without_live_thread_resumes():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "dispatched"}, thread_ids={"ENG-2": "thread-1"})
    intents = plan_orchestration(snapshot, OrchestratorContext(live_thread_ids=frozenset()))
    assert intents == [ResumeStory("ENG-2")]


def test_a_finished_story_is_merged_into_the_epic_without_any_gate():
    """No per-story pull request and no PR gates: a story that passed its
    run is landed on the epic branch locally; the epic opens the one PR."""
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "ready_to_merge"})
    assert plan_orchestration(snapshot, OrchestratorContext()) == [MergeIntoEpic("ENG-2")]


def test_a_merge_left_behind_by_a_crash_is_merged_again():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "merging"})
    assert plan_orchestration(snapshot, OrchestratorContext()) == [MergeIntoEpic("ENG-2")]


def test_a_merge_in_flight_is_not_started_twice():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "merging"})
    ctx = OrchestratorContext(live_thread_ids=frozenset({"ENG-2"}))
    assert plan_orchestration(snapshot, ctx) == [NoOp(story_key="ENG-2", reason="already_merging")]


def test_a_dependent_starts_only_once_its_dependency_has_merged_not_when_it_finished():
    """N1 done -> merged into the epic -> N2 starts from the epic branch."""
    finished = _snapshot([["ENG-2"], ["ENG-3"]], {"ENG-2": "ready_to_merge", "ENG-3": "pending"})
    merged = _snapshot([["ENG-2"], ["ENG-3"]], {"ENG-2": "merged", "ENG-3": "pending"})
    assert plan_orchestration(finished, OrchestratorContext()) == [
        MergeIntoEpic("ENG-2"), NoOp(story_key="ENG-3", reason="waiting_on_dependencies"),
    ]
    assert plan_orchestration(merged, OrchestratorContext()) == [DispatchStory("ENG-3")]


def test_a_failed_story_halts_the_whole_epic():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "merged", "ENG-3": "failed", "ENG-4": "pending"},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [NoOp(reason="epic_failed", story_key="ENG-3")]


def test_level_advances_once_fully_merged():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "merged", "ENG-3": "merged", "ENG-4": "pending"},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [DispatchStory("ENG-4")]


def test_skipped_counts_as_resolved_for_level_advancement():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "merged", "ENG-3": "skipped", "ENG-4": "pending"},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [DispatchStory("ENG-4")]


def test_fully_complete_epic_is_a_noop():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "merged"})
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert intents == [NoOp(reason="epic_complete")]


# --- describe_story_action: the manual-run-mode, single-story counterpart
# to plan_orchestration's scan. Mirrors the same rules (dependency
# ordering, epic-wide failure halt) but validates one requested story
# instead of acting on everything ready. ---------------------------------


def test_describe_story_action_dispatches_a_pending_story_in_the_open_level():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"})
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == DispatchStory("ENG-2")


def test_describe_story_action_blocks_unknown_story():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"})
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-999") == Blocked("ENG-999", "unknown_story")


def test_describe_story_action_blocks_when_epic_on_hold():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"}, manual_gate=True)
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == Blocked("ENG-2", "epic_on_hold")


def test_describe_story_action_blocks_when_no_levels():
    snapshot = EpicSnapshot(jira_key="ENG-1", levels=[], stories={"ENG-2": StorySnapshot("ENG-2", "pending")})
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == Blocked("ENG-2", "no_levels")


def test_describe_story_action_blocks_a_story_whose_dependencies_have_not_merged():
    snapshot = _snapshot(
        [["ENG-2"], ["ENG-3"]],
        {"ENG-2": "pending", "ENG-3": "pending"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-3") == Blocked("ENG-3", "waiting_on_dependencies")


def test_describe_story_action_allows_a_later_level_story_with_no_dependencies():
    snapshot = _snapshot(
        [["ENG-2"], ["ENG-3"]],
        {"ENG-2": "pending", "ENG-3": "pending"},
        depends_on={},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-3") == DispatchStory("ENG-3")


def test_describe_story_action_allows_the_earlier_story_in_the_same_open_level():
    snapshot = _snapshot(
        [["ENG-2"], ["ENG-3"]],
        {"ENG-2": "pending", "ENG-3": "pending"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == DispatchStory("ENG-2")


def test_describe_story_action_blocks_a_story_whose_dependency_failed():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "merged", "ENG-3": "failed", "ENG-4": "pending"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-4") == Blocked("ENG-4", "dependency_failed")


def test_describe_story_action_blocks_a_finished_or_failed_story_itself():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3", "ENG-4"]],
        {"ENG-2": "merged", "ENG-3": "failed", "ENG-4": "pending"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == Blocked("ENG-2", "story_finished")
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-3") == Blocked("ENG-3", "story_failed")


def test_describe_story_action_blocks_when_epic_already_resolved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "merged"})
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == Blocked("ENG-2", "epic_resolved")


def test_describe_story_action_parks_for_plan_approval_gate():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_plan_approval"})
    ctx = OrchestratorContext(require_plan_approval=True)
    assert describe_story_action(snapshot, ctx, "ENG-2") == ParkForGate("ENG-2", "plan_approval_gate")


def test_describe_story_action_resumes_once_approved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_plan_approval"})
    ctx = OrchestratorContext(require_plan_approval=True, approved_story_keys=frozenset({"ENG-2"}))
    assert describe_story_action(snapshot, ctx, "ENG-2") == ResumeStory("ENG-2")


def test_describe_story_action_matches_plan_orchestration_for_the_same_story():
    """Same story, same snapshot — a manual Start must produce exactly what
    the auto-mode scan would have, so switching auto_run_enabled off never
    changes *what* happens, only *when* (a tap instead of a timer)."""
    snapshot = _snapshot([["ENG-2", "ENG-3"]], {"ENG-2": "pending", "ENG-3": "pending"})
    ctx = OrchestratorContext()
    auto_intents = {i.story_key: i for i in plan_orchestration(snapshot, ctx) if isinstance(i, DispatchStory)}
    assert describe_story_action(snapshot, ctx, "ENG-2") == auto_intents["ENG-2"]


# --- epic_completion_status: keeps Epic.phase accurate for a manual-run
# epic that poll_once's automatic scan never visits. -----------------------


def test_epic_completion_status_none_while_in_progress():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "pending"})
    assert epic_completion_status(snapshot) is None


def test_epic_completion_status_none_with_no_levels():
    snapshot = EpicSnapshot(jira_key="ENG-1", levels=[], stories={})
    assert epic_completion_status(snapshot) is None


def test_epic_completion_status_complete_once_every_level_resolved():
    snapshot = _snapshot([["ENG-2"], ["ENG-3"]], {"ENG-2": "merged", "ENG-3": "skipped"})
    assert epic_completion_status(snapshot) == "complete"


def test_epic_completion_status_failed_on_any_failed_story():
    snapshot = _snapshot([["ENG-2", "ENG-3"]], {"ENG-2": "merged", "ENG-3": "failed"})
    assert epic_completion_status(snapshot) == "failed"


# --- display_epic_phase ------------------------------------------------


def test_epic_phase_is_planning_until_a_story_gets_past_plan_approval():
    for statuses in (["pending"], ["awaiting_plan_approval"], ["skipped", "pending"], []):
        assert display_epic_phase("planning", statuses) == "planning"


def test_epic_phase_is_implementing_once_any_story_is_under_way_or_further():
    for status in ("dispatched", "awaiting_pr_open", "awaiting_pr_review", "merged", "done", "failed"):
        assert display_epic_phase("planning", ["pending", status]) == "implementing"


def test_epic_phase_derivation_covers_both_non_terminal_stored_phases():
    # a retry stores "implementing" for a story that is back to pending
    assert display_epic_phase("implementing", ["pending"]) == "planning"
    assert display_epic_phase("implementing", ["dispatched"]) == "implementing"


def test_a_stored_terminal_or_unknown_phase_is_passed_through():
    assert display_epic_phase("complete", ["pending"]) == "complete"
    assert display_epic_phase("failed", ["merged"]) == "failed"
    assert display_epic_phase("building", ["merged"]) == "building"
