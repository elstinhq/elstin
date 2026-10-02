from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import Epic, ProjectRepo, Story, StoryWorktree


# Messages a person may have waiting for one story's agent at once. The agent
# reads them at the start of its next turn; a run that has not got to its next
# turn in this many messages is not one a person is steering.
MAX_PENDING_GUIDANCE = 10


class GuidanceQueueFull(Exception):
    """The story already has MAX_PENDING_GUIDANCE unread messages."""


class _StoryMixin:
    def create_story(
        self,
        jira_key: str,
        *,
        epic_key: str,
        wave: int,
        summary: str = "",
        description: str | None = None,
        kind: str = "dev",
        kind_reason: str | None = None,
        issue_type: str | None = None,
        labels: list[str] | None = None,
        parent_key: str | None = None,
        status: str = "pending",
        external_source: str | None = None,
        external_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> None:
        with session_scope() as session:
            if session.get(Story, jira_key) is not None:
                return
            session.add(Story(
                jira_key=jira_key, epic_key=epic_key, wave=wave, status=status,
                summary=summary, description=description,
                kind=kind, kind_reason=kind_reason, parent_key=parent_key,
                issue_type=issue_type, labels=list(labels) if labels else None,
                external_source=external_source, external_id=external_id,
                # None (not []) when the caller does not know them: readers then
                # fall back to the epic's levels (epic_sync.stored_dependencies)
                # rather than treating the story as free to start at once.
                depends_on=list(depends_on) if depends_on is not None else None,
            ))

    def list_subtasks(self, parent_key: str) -> list[dict]:
        """A story's JIRA sub-tasks, ordered by key. These are never
        dispatched — they exist to enrich the parent's brief and to render
        the hierarchy (see Story.parent_key)."""
        from forge.services.jira_service import issue_key_sort_key

        with session_scope() as session:
            rows = session.execute(select(Story).where(Story.parent_key == parent_key)).scalars().all()
            return sorted((_story_to_dict(session, row) for row in rows), key=lambda s: issue_key_sort_key(s["jira_key"]))

    def get_story(self, jira_key: str) -> dict | None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            return _story_to_dict(session, story) if story is not None else None

    def update_story_jira_fields(
        self, jira_key: str, *, summary: str, description: str | None
    ) -> None:
        """Refresh JIRA-sourced prose only. Orchestration fields (status,
        kind, wave, gate, branch, …) stay untouched — epic re-sync still
        no-ops on existing rows; this is the explicit per-ticket refresh."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.summary = summary
                story.description = description

    def list_stories_for_epic(self, epic_key: str) -> list[dict]:
        with session_scope() as session:
            rows = session.execute(select(Story).where(Story.epic_key == epic_key)).scalars().all()
            return [_story_to_dict(session, row) for row in rows]

    def set_story_project_repo(self, jira_key: str, project_repo_id: int | None) -> bool:
        """Point one story at a specific repo, or None to inherit the
        epic's. Returns False for an unknown story so the API layer can
        answer 404 rather than silently succeeding."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is None:
                return False
            story.project_repo_id = project_repo_id
            return True

    def set_story_status(
        self, jira_key: str, status: str, *, parked_reason: str | None = None, failure_stage: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.status = status
                story.parked_reason = parked_reason
                # Same convention as parked_reason: always overwritten, not
                # preserved — a status transition away from "failed" means
                # whatever failure caused it no longer applies.
                story.failure_stage = failure_stage
                story.retry_at = retry_at

    def add_story_guidance(self, jira_key: str, text: str) -> int | None:
        """Queue a person's message for the story's running agent; returns how
        many are now waiting, or None for an unknown story. Raises
        GuidanceQueueFull past MAX_PENDING_GUIDANCE."""
        with session_scope() as session:
            story = session.get(Story, jira_key, with_for_update=True)
            if story is None:
                return None
            pending = list(story.pending_guidance or [])
            if len(pending) >= MAX_PENDING_GUIDANCE:
                raise GuidanceQueueFull(jira_key)
            pending.append({"text": text, "at": datetime.now(timezone.utc).isoformat()})
            story.pending_guidance = pending  # a new list: in-place changes to a JSON column are not tracked
            return len(pending)

    def take_story_guidance(self, jira_key: str) -> list[dict]:
        """Remove and return every message waiting for the story's agent, oldest
        first. Atomic: a message added while this runs is in this batch or the
        next, never lost and never delivered twice."""
        with session_scope() as session:
            story = session.get(Story, jira_key, with_for_update=True)
            if story is None or not story.pending_guidance:
                return []
            taken = list(story.pending_guidance)
            story.pending_guidance = None
            return taken

    def list_stories_waiting_on_a_limit(self, now: datetime | None = None) -> list[dict]:
        """Failed stories parked on a provider limit whose retry time has come
        (parked_reason "usage_limit", retry_at <= now), oldest first — what the
        daemon retries on its own. A story with no retry_at is not here: the
        provider did not say when, so only a person can."""
        moment = now or datetime.now(timezone.utc)
        with session_scope() as session:
            rows = session.execute(
                select(Story)
                .where(
                    Story.status == "failed",
                    Story.parked_reason == "usage_limit",
                    Story.failure_stage == "run",
                    Story.retry_at.is_not(None),
                    Story.retry_at <= moment,
                )
                .order_by(Story.retry_at)
            ).scalars().all()
            return [_story_to_dict(session, row) for row in rows]

    def set_story_thread_id(self, jira_key: str, thread_id: str) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.thread_id = thread_id

    def set_story_worktree(self, jira_key: str, *, worktree_path: str, branch: str) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.worktree_path = worktree_path
                story.branch = branch

    def record_story_worktree(
        self,
        jira_key: str,
        *,
        repo_label: str,
        repo_path: str,
        worktree_path: str,
        branch: str,
        project_repo_id: int | None = None,
        is_default: bool = False,
    ) -> None:
        """Record one repo's worktree for a story, replacing any prior row
        for that (story, repo).

        `is_default` also mirrors the values onto Story.worktree_path /
        Story.branch. That mirror is what keeps the dashboard and every
        single-repo code path working unchanged now that a story can have
        several worktrees — the table is the full picture, the two columns
        are the default repo's."""
        with session_scope() as session:
            row = (
                session.query(StoryWorktree)
                .filter_by(story_key=jira_key, repo_label=repo_label)
                .one_or_none()
            )
            if row is None:
                row = StoryWorktree(story_key=jira_key, repo_label=repo_label)
                session.add(row)
            row.repo_path = repo_path
            row.project_repo_id = project_repo_id
            row.worktree_path = worktree_path
            row.branch = branch

            if is_default:
                story = session.get(Story, jira_key)
                if story is not None:
                    story.worktree_path = worktree_path
                    story.branch = branch

    def list_story_worktrees(self, jira_key: str) -> list[dict]:
        """Every repo this story has a worktree in, creation order.

        Empty for a story dispatched before per-repo worktrees existed;
        callers fall back to Story.worktree_path/branch, so an in-flight
        story keeps working across the upgrade."""
        with session_scope() as session:
            rows = (
                session.query(StoryWorktree)
                .filter_by(story_key=jira_key)
                .order_by(StoryWorktree.id)
                .all()
            )
            return [
                {
                    "repo_label": r.repo_label,
                    "repo_path": r.repo_path,
                    "project_repo_id": r.project_repo_id,
                    "worktree_path": r.worktree_path,
                    "branch": r.branch,
                }
                for r in rows
            ]

    def list_worktrees_of_stories_in(self, statuses: tuple[str, ...]) -> list[dict]:
        """Every worktree row of a story whose status is one of `statuses`,
        newest worktree first, with what a cleanup needs to judge it: the
        story's status and its epic's integration branch. For
        forge/daemon/worktree_retention.py, which reclaims the disk of stories
        that are finished."""
        with session_scope() as session:
            rows = (
                session.query(StoryWorktree, Story, Epic)
                .join(Story, Story.jira_key == StoryWorktree.story_key)
                .join(Epic, Epic.jira_key == Story.epic_key)
                .filter(Story.status.in_(statuses))
                .order_by(StoryWorktree.created_at.desc(), StoryWorktree.id.desc())
                .all()
            )
            return [
                {
                    "story_key": story.jira_key,
                    "status": story.status,
                    "repo_label": wt.repo_label,
                    "repo_path": wt.repo_path,
                    "worktree_path": wt.worktree_path,
                    "branch": wt.branch,
                    "integration_branch": epic.integration_branch,
                    "created_at": wt.created_at,
                }
                for wt, story, epic in rows
            ]

    def set_story_kind(self, jira_key: str, kind: str, *, reason: str = "manual") -> None:
        """A human correcting forge's sync-time guess. Records the reason as
        "manual" so a later reader can tell an overridden classification
        apart from a heuristic one."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.kind = kind
                story.kind_reason = reason

    def set_story_wave(self, jira_key: str, wave: int) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.wave = wave

    def set_story_dependencies(self, jira_key: str, depends_on: list[str]) -> None:
        """The stories this one waits for (Story.depends_on) — written by
        forge/services/epic_sync.py on every sync and relevel."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.depends_on = list(depends_on)

    def set_story_prior_work(self, jira_key: str, prior_work: dict | None) -> None:
        """The lookup of work already done on this ticket
        (forge/services/prior_work.py) — or None to look again."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.prior_work = dict(prior_work) if prior_work is not None else None

    def send_back_to_planning(self, jira_key: str, *, keep_answers: bool = False) -> bool:
        """A plan waiting for approval that no longer fits the code — the
        epic branch gained work it was made without — goes back to `pending`
        to be planned again, its questions and answers with it. Only from
        "awaiting_plan_approval": anything later has work to keep. False,
        changing nothing, otherwise.

        `keep_answers`: a person asked for the new plan (POST /{key}/replan)
        — the code has not changed under their answers, which stand as
        decisions for the fresh run."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is None or story.status != "awaiting_plan_approval":
                return False
            story.status = "pending"
            story.parked_reason = None
            story.gate_approved = False
            story.clarification_questions = None
            if not keep_answers:
                story.clarification_answers = None
            return True

    def pick_story_prior_branch(self, jira_key: str, repo_label: str, branch: str | None) -> bool:
        """A human's answer where several branches diverged: continue from
        `branch` in `repo_label`, or start fresh (None). False — nothing
        recorded — unless that repo's lookup offered that branch and the
        story has not started."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            # A ticket closed as done is picked for too: its chosen branch is
            # what gets merged into the epic branch.
            if story is None or story.status not in ("pending", "skipped") or story.branch or not story.prior_work:
                return False
            finding = (story.prior_work.get("repos") or {}).get(repo_label)
            if not finding:
                return False
            if branch is not None and branch not in {c.get("branch") for c in finding.get("candidates") or []}:
                return False
            prior_work = dict(story.prior_work)
            prior_work["picks"] = {**(prior_work.get("picks") or {}), repo_label: branch or ""}
            story.prior_work = prior_work  # a new object: JSON columns do not track in-place edits
            return True

    def increment_story_merge_attempts(self, jira_key: str) -> int:
        """One more send-back over landing this story; returns the new count."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is None:
                return 0
            story.merge_attempts = (story.merge_attempts or 0) + 1
            return story.merge_attempts

    def set_story_gate_approved(self, jira_key: str, approved: bool = True) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.gate_approved = approved

    def set_story_clarification_questions(self, jira_key: str, questions: list[dict] | None) -> None:
        """The planner's pending questions, persisted so the API/UI can
        show them and a human's answer can survive past this process (see
        forge/api/run_routes.py's /answer endpoint). Called on every run
        completion, not only when questions exist — None clears a stale
        form left over from an earlier round."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.clarification_questions = list(questions) if questions else None

    def set_story_clarification_answers(self, jira_key: str, answers: list[dict]) -> bool:
        """A human's resolved answers to `clarification_questions` (see
        forge/api/run_routes.py's /answer endpoint, which resolves each
        submitted option against the question that actually offered it).

        Clears clarification_questions — it is no longer pending — and sets
        gate_approved the same way /approve does: the reconciler cannot
        tell an awaiting_clarification story apart from an ordinary
        plan-approval one (both map to "awaiting_plan_approval" in
        forge/daemon/main.py's _GRAPH_STATUS_TO_STORY_STATUS), so answering
        clears the same gate. Returns False for an unknown story."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is None:
                return False
            story.clarification_answers = list(answers)
            story.clarification_questions = None
            story.gate_approved = True
            return True

    def set_story_coverage_gap(self, jira_key: str, coverage_gap: bool, *, detail: str | None = None) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.coverage_gap = coverage_gap
                story.coverage_gap_detail = detail

    def set_story_read_only_changes(self, jira_key: str, changes: list[dict] | None) -> None:
        """From the story's latest plan: changes it needs that forge may not
        make, for a person. None clears what an earlier plan listed."""
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.read_only_changes = list(changes) if changes else None

    def set_story_pr_open_error(self, jira_key: str, error: str | None) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.pr_open_error = error

    def retry_story(self, jira_key: str, *, resume_at_dev: bool = False) -> None:
        """Recovers a story the reconciler halted the epic on (see
        forge/orchestration/reconciler.py's epic-wide halt on any "failed"
        story). Caller (forge/api/run_routes.py) is responsible for
        confirming the story is actually "failed" first — this method
        trusts that and just performs the transition; the next daemon poll
        does the real work (redispatch or re-merge) once it sees a story
        that's no longer "failed" and an epic phase that's no longer
        "failed" (list_active_epic_keys excludes "failed"-phase epics
        entirely — see forge/state/manager_epic.py — so that has to move
        too, not just the story).

        failure_stage picks the recovery path: "merge" (the run succeeded,
        only landing it on the epic branch failed) goes back to
        ready_to_merge, so the merge queue tries again with the story's work
        intact and a fresh attempt budget. Anything else — "run", or unset
        for a story that failed before this column existed — goes back to
        pending for a full redispatch, since a run failure has nothing
        committed worth keeping.

        `resume_at_dev`: the caller has already re-armed the story's
        checkpoint at dev (forge/daemon/main.py's arm_resume_at_dev), so a
        run that failed in dev continues with the plan it has instead of
        planning again. The story goes to "dispatched" — what the
        reconciler answers with a ResumeStory when nothing is executing it
        — rather than "pending", whose DispatchStory starts the graph over.
        Ignored for a merge failure, which never re-plans.
        """
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is None:
                return
            stage = story.failure_stage
            story.retry_count += 1
            story.parked_reason = None
            story.retry_at = None
            story.failure_stage = None
            story.merge_attempts = 0
            if stage == "merge":
                story.status = "ready_to_merge"
            elif resume_at_dev:
                story.status = "dispatched"
            else:
                story.status = "pending"
            epic_key = story.epic_key

        # Cross-mixin call — StateManager composes _StoryMixin and
        # _EpicMixin onto one `self` (see forge/state/manager.py).
        self.set_epic_phase(epic_key, "implementing")

    def approved_story_keys_for_epic(self, epic_key: str) -> frozenset[str]:
        with session_scope() as session:
            rows = (
                session.execute(select(Story.jira_key).where(Story.epic_key == epic_key, Story.gate_approved.is_(True)))
                .scalars()
                .all()
            )
            return frozenset(rows)


def _resolve_story_repo(session, story: Story) -> tuple[int | None, str | None]:
    """(effective project_repo_id, filesystem path) for the repo this story
    writes to.

    Falls back to the epic's repo when the story has no override, which is
    the normal case — a per-story repo only appears on a project whose
    repos are split by concern. Resolved here rather than in the daemon so
    every caller of get_story/list_stories_for_epic sees one answer and
    cannot accidentally use the epic's repo for a story that overrode it."""
    repo_id = story.project_repo_id
    if repo_id is None:
        epic = session.get(Epic, story.epic_key)
        repo_id = epic.project_repo_id if epic is not None else None
    if repo_id is None:
        return None, None
    project_repo = session.get(ProjectRepo, repo_id)
    return repo_id, (project_repo.path if project_repo is not None else None)


def _story_to_dict(session, story: Story) -> dict:
    effective_repo_id, repo_path = _resolve_story_repo(session, story)
    return {
        # The story's own override, or None when it inherits the epic's.
        "project_repo_id": story.project_repo_id,
        # The repo this story actually writes to, override or inherited.
        # Every story-scoped git operation in forge/daemon/main.py keys off
        # this rather than epic["repo"].
        "effective_project_repo_id": effective_repo_id,
        "repo": repo_path,
        "jira_key": story.jira_key,
        "epic_key": story.epic_key,
        "parent_key": story.parent_key,
        "summary": story.summary,
        "description": story.description,
        "issue_type": story.issue_type,
        "labels": list(story.labels or []),
        "wave": story.wave,
        # None only for a row that predates the column and was never
        # re-synced — see forge/services/epic_sync.py's stored_dependencies.
        "depends_on": list(story.depends_on) if story.depends_on is not None else None,
        "prior_work": story.prior_work,
        "status": story.status,
        "kind": story.kind,
        "kind_reason": story.kind_reason,
        "thread_id": story.thread_id,
        "worktree_path": story.worktree_path,
        "branch": story.branch,
        "parked_reason": story.parked_reason,
        "retry_at": story.retry_at.isoformat() if story.retry_at else None,
        "gate_approved": story.gate_approved,
        "clarification_questions": list(story.clarification_questions or []),
        "clarification_answers": list(story.clarification_answers or []),
        "coverage_gap": story.coverage_gap,
        "coverage_gap_detail": story.coverage_gap_detail,
        "read_only_changes": list(story.read_only_changes or []),
        "pr_open_error": story.pr_open_error,
        "retry_count": story.retry_count,
        "failure_stage": story.failure_stage,
        "merge_attempts": story.merge_attempts or 0,
        "external_source": story.external_source,
        "external_id": story.external_id,
        "external_synced_at": story.external_synced_at.isoformat() if story.external_synced_at else None,
        "external_hash": story.external_hash,
        "external_status": story.external_status,
        "external_closed": story.external_closed,
        "external_pending": story.external_pending,
        "external_changed_at": story.external_changed_at.isoformat() if story.external_changed_at else None,
    }
