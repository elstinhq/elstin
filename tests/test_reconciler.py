"""Pure unit tests, no database, no LLM — plan_orchestration is a plain
function over in-memory fixtures. Mirrors epic_runner.py's level-by-level
dispatch semantics: same-level stories run in parallel, a level only
advances once every story in it is terminal, and any failure halts
everything downstream."""
from __future__ import annotations

from forge.orchestration.reconciler import (
    Blocked,
    DispatchStory,
    EpicSnapshot,
    MergeIntegrationBranch,
    NoOp,
    OpenPullRequest,
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
    coverage_gap_keys: frozenset[str] = frozenset(),
) -> EpicSnapshot:
    thread_ids = thread_ids or {}
    stories = {
        key: StorySnapshot(jira_key=key, status=status, thread_id=thread_ids.get(key), coverage_gap=key in coverage_gap_keys)
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


def test_same_level_stories_dispatch_together_next_level_untouched():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"], ["ENG-4"]],
        {"ENG-2": "pending", "ENG-3": "pending", "ENG-4": "pending"},
    )
    intents = plan_orchestration(snapshot, OrchestratorContext())
    assert set(intents) == {DispatchStory("ENG-2"), DispatchStory("ENG-3")}


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


def test_pr_open_gate_parks_when_unapproved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_pr_open"})
    intents = plan_orchestration(snapshot, OrchestratorContext(require_pr_open_approval=True))
    assert intents == [ParkForGate("ENG-2", "pr_open_gate")]


def test_pr_open_gate_opens_once_approved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_pr_open"})
    ctx = OrchestratorContext(require_pr_open_approval=True, approved_story_keys=frozenset({"ENG-2"}))
    intents = plan_orchestration(snapshot, ctx)
    assert intents == [OpenPullRequest("ENG-2")]


def test_pr_open_gate_off_opens_without_approval():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_pr_open"})
    intents = plan_orchestration(snapshot, OrchestratorContext(require_pr_open_approval=False))
    assert intents == [OpenPullRequest("ENG-2")]


def test_pr_open_gate_forced_on_for_coverage_gap_even_when_project_disabled_it():
    """A coverage-gap story didn't fully verify — it must not silently ride
    along on a gate the project turned off for the fully-verified case."""
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_pr_open"}, coverage_gap_keys=frozenset({"ENG-2"}))
    intents = plan_orchestration(snapshot, OrchestratorContext(require_pr_open_approval=False))
    assert intents == [ParkForGate("ENG-2", "pr_open_gate")]


def test_pr_open_gate_coverage_gap_still_opens_once_approved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_pr_open"}, coverage_gap_keys=frozenset({"ENG-2"}))
    ctx = OrchestratorContext(require_pr_open_approval=False, approved_story_keys=frozenset({"ENG-2"}))
    intents = plan_orchestration(snapshot, ctx)
    assert intents == [OpenPullRequest("ENG-2")]


def test_pr_review_gate_parks_then_merges_once_approved():
    snapshot = _snapshot([["ENG-2"]], {"ENG-2": "awaiting_pr_review"})

    parked = plan_orchestration(snapshot, OrchestratorContext(require_pr_review=True))
    assert parked == [ParkForGate("ENG-2", "pr_review_gate")]

    approved_ctx = OrchestratorContext(require_pr_review=True, approved_story_keys=frozenset({"ENG-2"}))
    merged = plan_orchestration(snapshot, approved_ctx)
    assert merged == [MergeIntegrationBranch("ENG-2")]


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


def test_describe_story_action_blocks_a_story_in_an_unresolved_later_level():
    snapshot = _snapshot(
        [["ENG-2"], ["ENG-3"]],
        {"ENG-2": "pending", "ENG-3": "pending"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-3") == Blocked("ENG-3", "earlier_level_unresolved")


def test_describe_story_action_allows_the_earlier_story_in_the_same_open_level():
    snapshot = _snapshot(
        [["ENG-2"], ["ENG-3"]],
        {"ENG-2": "pending", "ENG-3": "pending"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == DispatchStory("ENG-2")


def test_describe_story_action_blocks_when_epic_halted_by_a_failure():
    snapshot = _snapshot(
        [["ENG-2", "ENG-3"]],
        {"ENG-2": "merged", "ENG-3": "failed"},
    )
    assert describe_story_action(snapshot, OrchestratorContext(), "ENG-2") == Blocked("ENG-2", "epic_halted")


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
