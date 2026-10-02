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

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forge.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Epic-level phase — trimmed to what a single-tenant local reconciler needs
# to branch on.
# "complete": every story merged and the epic's pull request is not open yet
# (or GitHub is not configured); "pr_open": the epic's one pull request is
# open; "merged": a human merged it.
EpicPhase = str  # "planning" | "implementing" | "complete" | "pr_open" | "merged" | "failed"

# Coarse per-story status the reconciler reasons about. Fine-grained progress
# within a dispatched story (planning/developing/verifying/qa — a
# RunStatus) lives inside that story's LangGraph checkpoint, not here.
# "ready_to_merge": the run passed and its work is committed on the story
# branch; "merging": the merge queue is landing it on the epic branch
# (forge/daemon/main.py); "merged": it is on the epic branch.
StoryStatus = str  # "pending" | "awaiting_plan_approval" | "dispatched" | "ready_to_merge" | "merging" | "merged" | "done" | "failed" | "skipped"

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
    # What already existed for the epic's own key before forge made its
    # branch — a branch named after the epic, which the epic branch then
    # starts from. Shape and meaning: forge/services/prior_work.py. NULL
    # until looked up; never looked up once integration_branch is set.
    prior_work: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # A human-set hold on this epic alone — independent of the per-story
    # plan-approval/PR-review gates. The reconciler checks this before
    # looking at levels/stories at all (see forge/orchestration/reconciler.py).
    # New rows from StateManager.create_epic() start True (paused); the DB
    # column default stays false for rows created before that contract.
    manual_gate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Where this ticket came from — see alembic revision 20260920_0020.
    # `external_source` is NULL for a ticket created natively on the board.
    # `external_hash` digests the content as last applied, so a later sync
    # can tell whether the ticket changed; NULL means baseline unknown.
    external_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    external_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # What the last sync saw — alembic revision 20260920_0021. `external_hash`
    # above is the digest of source content last *resolved* (applied or
    # dismissed by a human), not merely last seen.
    external_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The source says this ticket is done, closed, or gone. Not-yet-started
    # stories are skipped, in-flight ones only flagged, epics put on hold.
    external_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

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

    # The top-level stories this one must wait for — list[story_key], from
    # the source's "blocks"/"is blocked by" links (a QA story waits for every
    # dev story). What the reconciler schedules on: a story starts once each
    # of these has merged, regardless of the rest of the epic. See
    # forge/services/epic_sync.py's compute_dependencies. NULL only on a row
    # that predates the column and has not been re-synced; readers fall back
    # to "every story in an earlier level" (epic_sync.stored_dependencies).
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Work already done on this ticket, found before forge created its
    # branch: per repo, the branches carrying the key and whether their work
    # is already in the default branch, plus a human's pick where several
    # diverged (forge/services/prior_work.py). The story's branch starts
    # from what this found. NULL until looked up — which happens only while
    # the story is pending and has no branch, so a story already underway is
    # never re-based.
    prior_work: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    # "dev" | "qa" — see StoryKind above. `kind_reason` records which
    # classification rule fired (or "manual" once a human overrides), so
    # the guess is inspectable rather than opaque.
    kind: Mapped[str] = mapped_column(String(16), default="dev", nullable=False)
    kind_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The ticket's own type and labels, as the source reports them. Kept
    # because the signals behind `kind` are worth more than the verdict:
    # branch naming reads them to choose feat/fix/docs/chore
    # (forge/tools/worktree.py's branch_type_for), and for a GitHub issue
    # the labels are the only place that distinction exists at all.
    issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    labels: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Cleared by a human via POST /api/stories/{key}/approve — the daemon
    # reads this into OrchestratorContext.approved_story_keys each poll
    # cycle. Whichever gate currently applies (plan-approval or PR-review)
    # is the one this clears; a story is only ever waiting on one at a time.
    gate_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The planner's pending clarification question(s) — see alembic
    # revision 20260922_0024. Straight from run_planner's `questions` field:
    # [{id, question, context, options: [{id, label, description}]}]. NULL
    # means nothing is pending; set on every run completion
    # (forge/daemon/main.py), not only when a question exists, so a story
    # that plans cleanly never shows a stale form. The reconciler cannot
    # tell this state apart from an ordinary plan-approval gate — both map
    # to "awaiting_plan_approval" — so POST /api/stories/{key}/answer clears
    # gate_approved the same way /approve does.
    clarification_questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # A human's resolved answers — [{question, option_label,
    # option_description}], the shape forge/agents/roles/planner_loop.py's
    # _build_prompt_text expects. Written alongside clearing
    # clarification_questions, and re-supplied into the graph state patch on
    # the next resume (forge/daemon/main.py), same as worktree_path/
    # story_kind/etc.
    clarification_answers: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # LangGraph checkpointer thread id for this story's run — set at first
    # dispatch, used to resume (`graph.ainvoke(None, {"configurable":
    # {"thread_id": ...}})`) rather than restarting from scratch.
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(256), nullable=True)

    parked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "plan_approval_gate", "pr_review_gate"

    # When a story that failed on a provider limit (parked_reason ==
    # "usage_limit") is retried automatically — the instant the provider said
    # the limit lifts. NULL: no retry is scheduled (the provider did not say
    # when, or the story failed some other way). Cleared by every status
    # transition, like parked_reason. See forge/daemon/main.py's
    # _resume_stories_past_their_limit.
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Messages a person sent to this story's running agent that it has not read
    # yet — [{text, at}], oldest first. The agent takes them at the start of its
    # next turn (forge/services/guidance.py); whatever is left when the run ends is
    # cleared and reported. NULL: nothing waiting.
    pending_guidance: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Set when the verifier's only blocker across every iteration was a
    # coverage-threshold miss (tests passed; coverage percentage below the
    # repo's bar) — see forge/tools/vue_tools.py's coverage-only-failure
    # detection. Distinct from a real implementation failure: the story
    # still completes and merges; the gap is called out in the epic's pull
    # request (forge/daemon/main.py) so the reviewer knows which story did
    # not fully verify.
    coverage_gap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    coverage_gap_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Changes the ticket needs that forge may not make and a person has to —
    # [{path, purpose}] from the latest plan: in a read-only repo or reference
    # folder, or a file the project says agents must not change
    # (forge/agents/roles/plan_gate.py's normalize_plan). A
    # copy of the checkpoint's plan field, so the board and the epic's pull
    # request can name them. NULL: none.
    read_only_changes: Mapped[list | None] = mapped_column(JSON, nullable=True)

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

    # How many times landing this story on the epic branch has sent it back to
    # dev (a conflict, or checks failing on the combined code) — see
    # forge/daemon/main.py's merge queue. Capped there; reset by a retry.
    merge_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Where this ticket came from — see alembic revision 20260920_0020.
    # `external_source` is NULL for a ticket created natively on the board.
    # `external_hash` digests the content as last applied, so a later sync
    # can tell whether the ticket changed; NULL means baseline unknown.
    external_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    external_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # What the last sync saw — alembic revision 20260920_0021. `external_hash`
    # above is the digest of source content last *resolved* (applied or
    # dismissed by a human), not merely last seen.
    external_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The source says this ticket is done, closed, or gone. Not-yet-started
    # stories are skipped, in-flight ones only flagged, epics put on hold.
    external_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # Content the source changed after this story was planned or started,
    # held for a human: {"summary", "description", "hash", "reasons"} — only
    # the keys that changed. NULL = nothing waiting.
    external_pending: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    external_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    epic: Mapped[Epic] = relationship(back_populates="stories")
    # One per repo the story wrote — see PullRequest's docstring.
    pull_requests: Mapped[list["PullRequest"]] = relationship(back_populates="story", cascade="all, delete-orphan")


class EpicPullRequest(Base):
    """The epic's pull request — the only one forge opens, per repo the epic's
    stories wrote: every story merges into the epic branch locally
    (forge/orchestration/integration.py), and once they all have, the epic
    branch is pushed and proposed for the default branch (forge/daemon/
    main.py's _open_epic_pull_request). A human reviews and merges it.

    `number`/`url` are NULL while it could not be opened yet — `error` then
    says why (a push or API failure), and the next poll tries again.
    `ci_status` is GitHub's combined status for its head ("pending" |
    "success" | "failure"), refreshed while it is open.
    """

    __tablename__ = "epic_pull_requests"
    __table_args__ = (UniqueConstraint("epic_key", "repo_label", name="uq_epic_pull_requests_epic_repo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epic_key: Mapped[str] = mapped_column(ForeignKey("epics.jira_key"), nullable=False, index=True)
    repo_label: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    repo_path: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    branch: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)  # "open" | "merged" | "closed"
    ci_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class PullRequest(Base):
    """One PR per (story, repo).

    A story whose plan writes two repos produces two branches and so two
    pull requests — that is git's constraint, not a forge choice. The
    uniqueness is therefore on the pair, not on the story alone.

    `repo_label` is the workspace label (forge/tools/workspace.py). The
    empty string means "the story's default repo" and is what rows written
    before per-repo PRs existed carry.
    """

    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("story_key", "repo_label", name="uq_pull_requests_story_repo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_key: Mapped[str] = mapped_column(ForeignKey("stories.jira_key"), nullable=False, index=True)
    repo_label: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)  # "open" | "merged" | "closed"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    story: Mapped[Story] = relationship(back_populates="pull_requests")


class JiraWatermark(Base):
    """Exactly-once comment/event polling per JIRA issue — watermarks are
    comment IDs, not timestamps."""

    __tablename__ = "jira_watermarks"

    jira_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
