"""The deterministic reconciler — the central architectural idea: a scan
over the epic's dependency graph. A story is ready as soon as every story it
depends on (`StorySnapshot.depends_on`, from the source's "blocks" links —
see elstin/services/epic_sync.py) has merged, so independent stories run in
parallel and a story waits only for its own blockers, not for everything in
an earlier level. A failure stops only the stories that depend on it,
directly or through another story; the rest of the epic carries on, and the
epic is "failed" only once nothing in it can make progress any more.

Pure function, no I/O, no LLM calls, no imports from elstin.state or elstin.db
— `plan_orchestration(snapshot, ctx) -> list[Intent]` takes a plain
in-memory view of one epic and returns what should happen next. The daemon
(elstin/daemon/main.py) is the only caller with I/O: it builds the snapshot
from StateManager, calls this, and turns each Intent into an actual
LangGraph run spawn/resume or a git merge.

Per-run implementation progress (planning/developing/verifying/qa) is a
LangGraph checkpoint concern, not this reconciler's — a story only carries
the coarse status this module needs to decide what's next.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

# Statuses that mean "this story is done contributing to level progression" —
# a level advances once every story in it is in one of these.
_TERMINAL_OK = frozenset({"merged", "done", "skipped"})

# Statuses of a story no implementation has started for: nothing is in flight
# and no plan has been approved. A "skipped" story never will be.
_NOT_STARTED = frozenset({"pending", "awaiting_plan_approval", "skipped"})


@dataclass(frozen=True)
class StorySnapshot:
    jira_key: str
    # "pending" | "awaiting_plan_approval" | "dispatched" | "ready_to_merge" | "merging"
    # | "merged" | "done" | "failed" | "skipped"
    status: str
    thread_id: str | None = None
    # The stories this one waits for. The daemon always fills it (falling
    # back to "every story in an earlier level" for a row that predates
    # stored dependencies — epic_sync.stored_dependencies).
    depends_on: tuple[str, ...] = ()
    # Work already done on this ticket was found on several diverged
    # branches and a human has not picked which one to continue from
    # (elstin/services/prior_work.py). A pending story waits for that pick —
    # elstin does not guess which code to build on.
    needs_branch_choice: bool = False


@dataclass(frozen=True)
class EpicSnapshot:
    jira_key: str
    # Topological levels, list of story-key lists. Which stories are work
    # units (only top-level stories appear) and the order intents come out
    # in — scheduling itself follows each story's depends_on.
    levels: list[list[str]]
    stories: dict[str, StorySnapshot]  # jira_key -> snapshot, for every story referenced in `levels`
    # A human-set hold on this epic alone (persisted on Epic.manual_gate) —
    # distinct from OrchestratorContext.dispatch_paused, which is a
    # process-wide flag with no per-epic granularity.
    manual_gate: bool = False


@dataclass(frozen=True)
class OrchestratorContext:
    dispatch_paused: bool = False
    # Stories with a run or a merge executing in this process right now.
    live_thread_ids: frozenset[str] = field(default_factory=frozenset)
    require_plan_approval: bool = True
    # Story keys where a human has cleared the plan-approval gate. It is the
    # only per-story human gate: a story is verified and merged into the epic
    # branch locally, and the one pull request is the epic's (see
    # elstin/orchestration/integration.py).
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
    reason: str  # "plan_approval_gate"


@dataclass(frozen=True)
class MergeIntoEpic:
    """Bring a finished story up to date with the epic branch, re-verify it,
    and fast-forward the epic branch to it — locally; see
    elstin/orchestration/integration.py."""

    story_key: str


@dataclass(frozen=True)
class NoOp:
    reason: str
    story_key: str | None = None


Intent = DispatchStory | ResumeStory | ParkForGate | MergeIntoEpic | NoOp


@dataclass(frozen=True)
class Blocked:
    """Returned by describe_story_action when a specific requested action —
    the Start button, or an approve/retry follow-through for a project with
    auto_run_enabled off (see elstin/daemon/main.py's trigger helpers) —
    isn't allowed right now. Distinct from NoOp: a NoOp is something
    plan_orchestration's automatic scan looked at and decided not to act on
    for now, a perfectly normal outcome of a background pass. Blocked is a
    rejection of a specific, named request, and API callers turn it into an
    HTTP 409 rather than a quiet no-op."""

    story_key: str
    # "unknown_story" | "epic_on_hold" | "no_levels" | "epic_resolved" | "story_finished" | "story_failed"
    # | "dependency_failed" | "waiting_on_dependencies" | "choose_prior_branch"
    reason: str


def plan_orchestration(snapshot: EpicSnapshot, ctx: OrchestratorContext) -> list[Intent]:
    if ctx.dispatch_paused:
        return [NoOp(reason="dispatch_paused")]

    if snapshot.manual_gate:
        return [NoOp(reason="manual_gate")]

    if not snapshot.levels:
        return [NoOp(reason="no_levels")]

    keys = _work_units(snapshot)
    if all(snapshot.stories[key].status in _TERMINAL_OK for key in keys):
        return [NoOp(reason="epic_complete")]

    halted = _halted_by_failure(snapshot, keys)
    intents: list[Intent] = []
    for key in keys:
        story = snapshot.stories[key]
        if story.status in _TERMINAL_OK or story.status == "failed":
            continue
        if key in halted:
            intents.append(NoOp(story_key=key, reason="dependency_failed"))
        elif story.status == "pending" and not _dependencies_met(snapshot, story):
            intents.append(NoOp(story_key=key, reason="waiting_on_dependencies"))
        elif story.status == "pending" and story.needs_branch_choice:
            intents.append(NoOp(story_key=key, reason="choose_prior_branch"))
        else:
            intents.append(_intent_for(story, ctx))

    if all(isinstance(intent, NoOp) and intent.reason == "dependency_failed" for intent in intents):
        # Nothing left can make progress: every unfinished story failed or
        # waits on one that did.
        return [NoOp(reason="epic_failed", story_key=_first_failed(snapshot, keys))]
    return intents


def _work_units(snapshot: EpicSnapshot) -> list[str]:
    """The epic's dispatchable stories, in level order (then issue-key
    order within a level, as sync stored them)."""
    return [key for level in snapshot.levels for key in level if key in snapshot.stories]


def _dependencies_met(snapshot: EpicSnapshot, story: StorySnapshot) -> bool:
    """Every dependency has merged (or is done/skipped). A dependency that is
    not a work unit of this epic — a key removed at the source — does not
    hold anything up."""
    return all(
        snapshot.stories[dep].status in _TERMINAL_OK for dep in story.depends_on if dep in snapshot.stories
    )


def _halted_by_failure(snapshot: EpicSnapshot, keys: list[str]) -> set[str]:
    """Stories that cannot proceed because something they depend on, directly
    or through another story, has failed."""
    failed = {key for key in keys if snapshot.stories[key].status == "failed"}
    halted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for key in keys:
            if key in halted or key in failed:
                continue
            if any(dep in failed or dep in halted for dep in snapshot.stories[key].depends_on):
                halted.add(key)
                changed = True
    return halted


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

    if story.status in ("ready_to_merge", "merging"):
        # "merging" left behind by a crash mid-merge is simply merged again:
        # every step of the merge is safe to repeat.
        if story.jira_key in ctx.live_thread_ids:
            return NoOp(story_key=story.jira_key, reason="already_merging")
        return MergeIntoEpic(story.jira_key)

    return NoOp(story_key=story.jira_key, reason=f"unknown_status:{story.status}")


def _first_failed(snapshot: EpicSnapshot, keys: list[str]) -> str | None:
    return next((key for key in keys if snapshot.stories[key].status == "failed"), None)


def describe_story_action(snapshot: EpicSnapshot, ctx: OrchestratorContext, story_key: str) -> Intent | Blocked:
    """The manual-run-mode counterpart to plan_orchestration: instead of
    scanning the epic and acting on everything ready, this validates and
    decides for exactly one requested story — the pure logic behind a
    project's Start button, and behind an approve/retry follow-through once
    a project has turned auto_run_enabled off (see elstin/daemon/main.py).

    Reuses `_intent_for` so a manual Start produces exactly the same Intent
    plan_orchestration would have produced for the same story in auto mode:
    dependency ordering (a pending story's dependencies must have merged)
    and failure propagation (nothing starts on top of a failed dependency)
    both still apply, only the "what's ready gets acted on automatically"
    part is gone.
    """
    if story_key not in snapshot.stories:
        return Blocked(story_key, "unknown_story")
    if snapshot.manual_gate:
        return Blocked(story_key, "epic_on_hold")
    if not snapshot.levels:
        return Blocked(story_key, "no_levels")

    keys = _work_units(snapshot)
    if all(snapshot.stories[key].status in _TERMINAL_OK for key in keys):
        return Blocked(story_key, "epic_resolved")
    story = snapshot.stories[story_key]
    if story.status in _TERMINAL_OK:
        return Blocked(story_key, "story_finished")
    if story.status == "failed":
        return Blocked(story_key, "story_failed")
    if story_key in _halted_by_failure(snapshot, keys):
        return Blocked(story_key, "dependency_failed")
    if story.status == "pending" and not _dependencies_met(snapshot, story):
        return Blocked(story_key, "waiting_on_dependencies")
    if story.status == "pending" and story.needs_branch_choice:
        return Blocked(story_key, "choose_prior_branch")
    return _intent_for(story, ctx)


def display_epic_phase(stored_phase: str, story_statuses: Iterable[str]) -> str:
    """The phase to show for an epic, read off its stories.

    `Epic.phase` is stored, but only "complete"/"failed" (the daemon's
    verdicts, which also decide whether the epic is polled at all) and a
    retry's "implementing" are ever written — an epic whose work is well
    under way still carries its creation default, "planning". So the two
    non-terminal phases are derived instead: "planning" until some story has
    got past plan approval, "implementing" from then on. A stored value that
    is anything else is passed through untouched.

    A story that is "dispatched" counts as implementing even during its
    planner run — the status does not distinguish the two."""
    if stored_phase not in ("planning", "implementing"):
        return stored_phase
    if any(status not in _NOT_STARTED for status in story_statuses):
        return "implementing"
    return "planning"


def epic_completion_status(snapshot: EpicSnapshot) -> str | None:
    """"complete" once every story is resolved, "failed" once nothing left
    can make progress (every unfinished story failed or waits on one that
    did), else None (still in progress). plan_orchestration computes this
    inline as part of its own scan; elstin/daemon/main.py's
    trigger_story_action calls this explicitly after a single-story action
    because poll_once's automatic scan skips epics whose project has
    auto_run_enabled off, so nothing else keeps Epic.phase accurate for
    them."""
    if not snapshot.levels:
        return None
    keys = _work_units(snapshot)
    if all(snapshot.stories[key].status in _TERMINAL_OK for key in keys):
        return "complete"
    failed = {key for key in keys if snapshot.stories[key].status == "failed"}
    halted = _halted_by_failure(snapshot, keys)
    unfinished = {key for key in keys if snapshot.stories[key].status not in _TERMINAL_OK}
    if failed and unfinished <= failed | halted:
        return "failed"
    return None
