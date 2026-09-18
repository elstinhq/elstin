from __future__ import annotations

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import Story


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
    ) -> None:
        with session_scope() as session:
            if session.get(Story, jira_key) is not None:
                return
            session.add(Story(
                jira_key=jira_key, epic_key=epic_key, wave=wave, status=status,
                summary=summary, description=description,
                kind=kind, kind_reason=kind_reason, parent_key=parent_key,
            ))

    def list_subtasks(self, parent_key: str) -> list[dict]:
        """A story's JIRA sub-tasks, ordered by key. These are never
        dispatched — they exist to enrich the parent's brief and to render
        the hierarchy (see Story.parent_key)."""
        from forge.services.jira_service import issue_key_sort_key

        with session_scope() as session:
            rows = session.execute(select(Story).where(Story.parent_key == parent_key)).scalars().all()
            return sorted((_story_to_dict(row) for row in rows), key=lambda s: issue_key_sort_key(s["jira_key"]))

    def get_story(self, jira_key: str) -> dict | None:
        with session_scope() as session:
            story = session.get(Story, jira_key)
            return _story_to_dict(story) if story is not None else None

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
            return [_story_to_dict(row) for row in rows]

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


def _story_to_dict(story: Story) -> dict:
    return {
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
    }
