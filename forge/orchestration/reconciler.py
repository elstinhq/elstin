"""The deterministic reconciler — the central architectural idea: a level
loop over the epic's dependency graph (stories in the same level run
concurrently, a level only starts once every earlier level is fully merged,
any failure halts everything downstream).

Pure function, no I/O, no LLM calls, no imports from forge.state or forge.db
— `plan_orchestration(snapshot, ctx) -> list[Intent]` takes a plain
in-memory view of one epic and returns what should happen next. The daemon
(forge/daemon/main.py) is the only caller with I/O: it builds the snapshot
from StateManager, calls this, and turns each Intent into an actual
LangGraph run spawn/resume or a git merge.

Per-run implementation progress (planning/developing/verifying/qa) is a
LangGraph checkpoint concern, not this reconciler's — a story only carries
the coarse status this module needs to decide what's next.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Statuses that mean "this story is done contributing to level progression" —
# a level advances once every story in it is in one of these.
_TERMINAL_OK = frozenset({"merged", "done", "skipped"})


@dataclass(frozen=True)
class StorySnapshot:
    jira_key: str
    status: str  # "pending" | "awaiting_plan_approval" | "dispatched" | "awaiting_pr_open" | "awaiting_pr_review" | "merged" | "done" | "failed" | "skipped"
    thread_id: str | None = None
    # Set when the verifier's only blocker was a coverage-threshold miss,
    # not a real implementation failure (see forge/tools/vue_tools.py).
    # Forces the pr_open_gate on below regardless of
    # ctx.require_pr_open_approval — a story that didn't fully verify
    # shouldn't ride along on a gate a project configured for the
    # fully-verified case.
    coverage_gap: bool = False


@dataclass(frozen=True)
class EpicSnapshot:
    jira_key: str
    levels: list[list[str]]  # topological levels, list of story-key lists
    stories: dict[str, StorySnapshot]  # jira_key -> snapshot, for every story referenced in `levels`
    # A human-set hold on this epic alone (persisted on Epic.manual_gate) —
    # distinct from OrchestratorContext.dispatch_paused, which is a
    # process-wide flag with no per-epic granularity.
    manual_gate: bool = False


@dataclass(frozen=True)
class OrchestratorContext:
    dispatch_paused: bool = False
    live_thread_ids: frozenset[str] = field(default_factory=frozenset)
    require_plan_approval: bool = True
    require_pr_open_approval: bool = True
    require_pr_review: bool = True
    # Story keys where a human has cleared whichever gate currently applies
    # to them (plan-approval, PR-open, or PR-review — a story is only ever
    # waiting on one at a time, so this single set serves all three).
    approved_story_keys: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class DispatchStory:
    story_key: str


@dataclass(frozen=True)
class ResumeStory:
    story_key: str


@dataclass(frozen=True)
class ParkForGate:
    story_key: str
    reason: str  # "plan_approval_gate" | "pr_open_gate" | "pr_review_gate"


@dataclass(frozen=True)
class OpenPullRequest:
    story_key: str


@dataclass(frozen=True)
class MergeIntegrationBranch:
    story_key: str


@dataclass(frozen=True)
class NoOp:
    reason: str
    story_key: str | None = None


Intent = DispatchStory | ResumeStory | ParkForGate | OpenPullRequest | MergeIntegrationBranch | NoOp


@dataclass(frozen=True)
class Blocked:
    """Returned by describe_story_action when a specific requested action —
    the Start button, or an approve/retry follow-through for a project with
    auto_run_enabled off (see forge/daemon/main.py's trigger helpers) —
    isn't allowed right now. Distinct from NoOp: a NoOp is something
    plan_orchestration's automatic scan looked at and decided not to act on
    for now, a perfectly normal outcome of a background pass. Blocked is a
    rejection of a specific, named request, and API callers turn it into an
    HTTP 409 rather than a quiet no-op."""

    story_key: str
    reason: str  # "unknown_story" | "epic_on_hold" | "no_levels" | "epic_halted" | "epic_resolved" | "earlier_level_unresolved"


def plan_orchestration(snapshot: EpicSnapshot, ctx: OrchestratorContext) -> list[Intent]:
    if ctx.dispatch_paused:
        return [NoOp(reason="dispatch_paused")]

    if snapshot.manual_gate:
        return [NoOp(reason="manual_gate")]

    if not snapshot.levels:
        return [NoOp(reason="no_levels")]

    for level_idx, level_keys in enumerate(snapshot.levels):
        level_stories = [snapshot.stories[key] for key in level_keys if key in snapshot.stories]

        if any(story.status == "failed" for story in level_stories):
            return [NoOp(reason="epic_failed", story_key=_first_failed(level_stories))]

        if all(story.status in _TERMINAL_OK for story in level_stories):
            continue  # this level is fully resolved — look at the next one

        # First not-fully-resolved level is the only one that gets acted on
        # this pass, mirroring epic_runner's sequential-levels behavior.
        return [_intent_for(story, ctx) for story in level_stories if story.status not in _TERMINAL_OK]

    return [NoOp(reason="epic_complete")]


def _intent_for(story: StorySnapshot, ctx: OrchestratorContext) -> Intent:
    if story.status == "pending":
        return DispatchStory(story.jira_key)

    if story.status == "awaiting_plan_approval":
        if not ctx.require_plan_approval or story.jira_key in ctx.approved_story_keys:
            return ResumeStory(story.jira_key)
        return ParkForGate(story.jira_key, "plan_approval_gate")

    if story.status == "dispatched":
        if story.thread_id and story.thread_id in ctx.live_thread_ids:
            return NoOp(story_key=story.jira_key, reason="already_running")
        return ResumeStory(story.jira_key)  # no live process for it — resume from checkpoint

    if story.status == "awaiting_pr_open":
        gate_required = ctx.require_pr_open_approval or story.coverage_gap
        if not gate_required or story.jira_key in ctx.approved_story_keys:
            return OpenPullRequest(story.jira_key)
        return ParkForGate(story.jira_key, "pr_open_gate")

    if story.status == "awaiting_pr_review":
        if not ctx.require_pr_review or story.jira_key in ctx.approved_story_keys:
            return MergeIntegrationBranch(story.jira_key)
        return ParkForGate(story.jira_key, "pr_review_gate")

    return NoOp(story_key=story.jira_key, reason=f"unknown_status:{story.status}")


def _first_failed(stories: list[StorySnapshot]) -> str | None:
    return next((s.jira_key for s in stories if s.status == "failed"), None)


def describe_story_action(snapshot: EpicSnapshot, ctx: OrchestratorContext, story_key: str) -> Intent | Blocked:
    """The manual-run-mode counterpart to plan_orchestration: instead of
    scanning every level and acting on everything in the first open one,
    this validates and decides for exactly one requested story — the pure
    logic behind a project's Start button, and behind an approve/retry
    follow-through once a project has turned auto_run_enabled off (see
    forge/daemon/main.py). Deliberately mirrors plan_orchestration's own
    level walk rather than sharing code with it, so that function — pinned
    by tests/test_reconciler.py — never has to change shape for this.

    Reuses `_intent_for` so a manual Start produces exactly the same Intent
    plan_orchestration would have produced for the same story in auto mode:
    dependency ordering (a story must be in the first not-yet-resolved
    level) and the epic-wide failure halt both still apply, only the
    "what's ready gets acted on automatically" part is gone.
    """
    if story_key not in snapshot.stories:
        return Blocked(story_key, "unknown_story")
    if snapshot.manual_gate:
        return Blocked(story_key, "epic_on_hold")
    if not snapshot.levels:
        return Blocked(story_key, "no_levels")

    for level_keys in snapshot.levels:
        level_stories = [snapshot.stories[key] for key in level_keys if key in snapshot.stories]

        if any(story.status == "failed" for story in level_stories):
            return Blocked(story_key, "epic_halted")

        if all(story.status in _TERMINAL_OK for story in level_stories):
            continue  # this level is fully resolved — look at the next one

        if story_key not in level_keys:
            return Blocked(story_key, "earlier_level_unresolved")
        return _intent_for(snapshot.stories[story_key], ctx)

    return Blocked(story_key, "epic_resolved")  # every level already terminal


def epic_completion_status(snapshot: EpicSnapshot) -> str | None:
    """"complete" once every level is fully resolved, "failed" if any level
    contains a failed story, else None (still in progress). plan_orchestration
    computes this inline as part of its own scan; forge/daemon/main.py's
    trigger_story_action calls this explicitly after a single-story action
    because poll_once's automatic scan skips epics whose project has
    auto_run_enabled off, so nothing else keeps Epic.phase accurate for
    them."""
    if not snapshot.levels:
        return None
    for level_keys in snapshot.levels:
        level_stories = [snapshot.stories[key] for key in level_keys if key in snapshot.stories]
        if any(story.status == "failed" for story in level_stories):
            return "failed"
        if not all(story.status in _TERMINAL_OK for story in level_stories):
            return None
    return "complete"
