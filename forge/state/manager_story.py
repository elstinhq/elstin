from __future__ import annotations

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import Epic, ProjectRepo, Story, StoryWorktree


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
        parent_key: str | None = None,
        status: str = "pending",
        external_source: str | None = None,
        external_id: str | None = None,
    ) -> None:
        with session_scope() as session:
            if session.get(Story, jira_key) is not None:
                return
            session.add(Story(
                jira_key=jira_key, epic_key=epic_key, wave=wave, status=status,
                summary=summary, description=description,
                kind=kind, kind_reason=kind_reason, parent_key=parent_key,
                external_source=external_source, external_id=external_id,
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
        self, jira_key: str, status: str, *, parked_reason: str | None = None, failure_stage: str | None = None
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

    def set_story_gate_approved(self, jira_key: str, approved: bool = True) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.gate_approved = approved

    def set_story_coverage_gap(self, jira_key: str, coverage_gap: bool, *, detail: str | None = None) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.coverage_gap = coverage_gap
                story.coverage_gap_detail = detail

    def set_story_pr_open_error(self, jira_key: str, error: str | None) -> None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is not None:
                story.pr_open_error = error

    def retry_story(self, jira_key: str) -> None:
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
        only merging the story's branch into the epic's integration branch
        conflicted) goes back to awaiting_pr_review with gate_approved
        cleared, mirroring how a CI failure already parks a story rather
        than discarding its work (see forge/daemon/main.py's
        _ci_checks_block_merge). Anything else — "run", or unset for a
        story that failed before this column existed — goes back to
        pending for a full redispatch, since a run failure never reaches a
        PR to preserve.
        """
        with session_scope() as session:
            story = session.get(Story, jira_key)
            if story is None:
                return
            stage = story.failure_stage
            story.retry_count += 1
            story.parked_reason = None
            story.failure_stage = None
            if stage == "merge":
                story.status = "awaiting_pr_review"
                story.gate_approved = False
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
        "wave": story.wave,
        "status": story.status,
        "kind": story.kind,
        "kind_reason": story.kind_reason,
        "thread_id": story.thread_id,
        "worktree_path": story.worktree_path,
        "branch": story.branch,
        "parked_reason": story.parked_reason,
        "gate_approved": story.gate_approved,
        "coverage_gap": story.coverage_gap,
        "coverage_gap_detail": story.coverage_gap_detail,
        "pr_open_error": story.pr_open_error,
        "retry_count": story.retry_count,
        "failure_stage": story.failure_stage,
        "external_source": story.external_source,
        "external_id": story.external_id,
        "external_synced_at": story.external_synced_at.isoformat() if story.external_synced_at else None,
        "external_hash": story.external_hash,
        "external_status": story.external_status,
        "external_closed": story.external_closed,
        "external_pending": story.external_pending,
        "external_changed_at": story.external_changed_at.isoformat() if story.external_changed_at else None,
    }
