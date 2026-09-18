"""Epic/story/PR bookkeeping: LangGraph's Postgres checkpointer already
persists per-run graph state (plan, dev diffs, verifier iterations, a
fine-grained RunStatus), so these tables only need what LangGraph doesn't
track — which wave a story is in, PR metadata, JIRA watermarks, and a
coarse status the reconciler can reason about without opening a checkpoint.

JIRA remains source of truth for story existence/assignee/summary — these
tables never duplicate that, only forge-internal tracking keyed by JIRA key.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Epic-level phase — trimmed to what a single-tenant local reconciler needs
# to branch on.
EpicPhase = str  # "planning" | "awaiting_plan_approval" | "implementing" | "awaiting_pr_review" | "complete" | "failed"

# Coarse per-story status the reconciler reasons about. Fine-grained progress
# within a dispatched story (planning/developing/verifying/qa — a
# RunStatus) lives inside that story's LangGraph checkpoint, not here.
StoryStatus = str  # "pending" | "ready" | "dispatched" | "awaiting_pr_review" | "pr_open" | "merged" | "done" | "failed" | "skipped"

# What kind of work a story represents, which decides how it is routed
# through the graph: "dev" runs planner -> dev -> verifier, "qa" skips all
# of those and runs the epic-QA review instead. Classified once at sync
# time (forge/services/story_kind.py) and human-overridable — never
# recomputed on a re-sync.
StoryKind = str  # "dev" | "qa"


class Epic(Base):
    __tablename__ = "epics"

    jira_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # Pulled from JIRA at sync time (see forge/services/epic_sync.py) and
    # never refreshed by a re-sync — same convention as Story.summary below.
    summary: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable: an epic can be synced from JIRA before a folder is chosen
    # for it — see forge/api/epic_routes.py's PUT .../repo for how it's set.
    # The resolved filesystem path is exposed as StateManager.get_epic()'s
    # "repo" dict key (joined through ProjectRepo.path), not stored here.
    project_repo_id: Mapped[int | None] = mapped_column(ForeignKey("project_repos.id"), nullable=True)
    phase: Mapped[str] = mapped_column(String(32), default="planning", nullable=False)

    # list[list[story_key]] — topological levels, produced by epic planning
    # (not part of this schema).
    levels: Mapped[list | None] = mapped_column(JSON, nullable=True)

    integration_branch: Mapped[str | None] = mapped_column(String(256), nullable=True)
    plan_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # A human-set hold on this epic alone — independent of the per-story
    # plan-approval/PR-review gates. The reconciler checks this before
    # looking at levels/stories at all (see forge/orchestration/reconciler.py).
    # New rows from StateManager.create_epic() start True (paused); the DB
    # column default stays false for rows created before that contract.
    manual_gate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    stories: Mapped[list["Story"]] = relationship(back_populates="epic", cascade="all, delete-orphan")


class Story(Base):
    __tablename__ = "stories"

    jira_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    epic_key: Mapped[str] = mapped_column(ForeignKey("epics.jira_key"), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL for a top-level story (a direct child of the epic); set to the
    # parent story's key for a JIRA sub-task. JIRA models sub-tasks as
    # hierarchyLevel -1 / subtask: true — not independently deliverable —
    # and forge follows that: **only top-level stories are dispatched.** A
    # sub-task is stored so the hierarchy is inspectable and so its
    # file-level detail can enrich its parent's brief, but it never gets a
    # worktree, a run, or a PR of its own, and never appears in
    # `Epic.levels`.
    #
    # Deliberately a plain indexed column rather than a self-referential FK:
    # sync inserts rows in one pass and a sub-task whose parent falls
    # outside the synced set should degrade to an orphan row, not abort the
    # whole sync. `epic_key` is the real integrity anchor.
    parent_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Which repo THIS story writes to, when the epic's default is not the
    # right one. NULL means "use the epic's", which is the common case; a
    # value only appears on a project whose repos are split by concern, so
    # that one epic can carry an API story and a UI story that land in
    # different repos. The story's repo is what gets the worktree, the
    # branch, the commit and the pull request — see
    # forge/tools/workspace.py.
    project_repo_id: Mapped[int | None] = mapped_column(ForeignKey("project_repos.id"), nullable=True)

    wave: Mapped[int] = mapped_column(Integer, nullable=False)  # index into epic.levels
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    # "dev" | "qa" — see StoryKind above. `kind_reason` records which
    # classification rule fired (or "manual" once a human overrides), so
    # the guess is inspectable rather than opaque.
    kind: Mapped[str] = mapped_column(String(16), default="dev", nullable=False)
    kind_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Cleared by a human via POST /api/stories/{key}/approve — the daemon
    # reads this into OrchestratorContext.approved_story_keys each poll
    # cycle. Whichever gate currently applies (plan-approval or PR-review)
    # is the one this clears; a story is only ever waiting on one at a time.
    gate_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # LangGraph checkpointer thread id for this story's run — set at first
    # dispatch, used to resume (`graph.ainvoke(None, {"configurable":
    # {"thread_id": ...}})`) rather than restarting from scratch.
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)

    parked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "plan_approval_gate", "pr_review_gate"

    # Set when the verifier's only blocker across every iteration was a
    # coverage-threshold miss (tests passed; coverage percentage below the
    # repo's bar) — see forge/tools/vue_tools.py's coverage-only-failure
    # detection. Distinct from a real implementation failure: the story
    # still completes and reaches the PR-open gate, but that gate is forced
    # on regardless of require_pr_open_approval (forge/orchestration/
    # reconciler.py) so a human looks before a not-fully-verified PR opens.
    coverage_gap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    coverage_gap_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when _maybe_open_pull_request (forge/daemon/main.py) attempts a
    # push or GitHub API call and it fails — that function is best-effort
    # and never blocks the story's own gate progress, so without this the
    # failure only ever reached a log line. Cleared on the next successful
    # PR-open attempt; NULL means either it hasn't been tried yet or the
    # last attempt succeeded.
    pr_open_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # How many times a human has retried this story out of "failed" (see
    # forge/state/manager_story.py's retry_story). Purely observational
    # today — nothing caps it — but it's what a future bounded-auto-retry
    # policy would read.
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Set alongside status="failed", distinguishing which recovery path
    # retry_story should take: "run" (the LangGraph run itself failed, no PR
    # opened yet — retry means a full redispatch) vs "merge" (the run
    # succeeded but merging into the epic's integration branch conflicted —
    # retry means only re-attempting the merge). Cleared on every other
    # status transition, same convention as parked_reason.
    failure_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    epic: Mapped[Epic] = relationship(back_populates="stories")
    pull_request: Mapped["PullRequest | None"] = relationship(back_populates="story", uselist=False, cascade="all, delete-orphan")


class PullRequest(Base):
    __tablename__ = "pull_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_key: Mapped[str] = mapped_column(ForeignKey("stories.jira_key"), nullable=False, unique=True)

    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)  # "open" | "merged" | "closed"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    story: Mapped[Story] = relationship(back_populates="pull_request")


class JiraWatermark(Base):
    """Exactly-once comment/event polling per JIRA issue — watermarks are
    comment IDs, not timestamps."""

    __tablename__ = "jira_watermarks"

    jira_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
