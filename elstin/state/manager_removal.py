"""Removing a ticket from elstin: what it owns, and deleting its rows.

A ticket is an epic and everything under it — its stories and sub-tasks —
and it leaves rows in several tables besides those two. `run_id` in the
activity, usage and run-outcome tables is the story key (thread_id == story
key, see elstin/daemon/main.py), and for a lone-story epic the epic and its
one story share a key, so the set of keys below is de-duplicated.

Two methods, split so the caller can clean up what lives outside the database
(worktrees, branches, checkpoints, downloaded files — elstin/daemon/
ticket_removal.py) between reading the footprint and deleting the rows: the
rows go last, so a cleanup that fails part-way leaves a ticket that is still
listed and can be removed again, instead of files no row points at.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from elstin.db.engine import session_scope
from elstin.db.models import (
    ActivityEvent,
    Epic,
    EpicPullRequest,
    JiraWatermark,
    ProjectRepo,
    PullRequest,
    Story,
    StoryRunOutcome,
    StoryUsage,
    StoryWorktree,
)
from elstin.state.manager_lesson import forget_evidence
from elstin.state.manager_story import _story_to_dict


class _RemovalMixin:
    def get_ticket_footprint(self, epic_key: str) -> dict | None:
        """Everything the ticket rooted at `epic_key` owns that lives outside
        its own rows, or None if there is no such epic. Read-only."""
        with session_scope() as session:
            epic = session.get(Epic, epic_key)
            if epic is None:
                return None
            stories = session.execute(select(Story).where(Story.epic_key == epic_key)).scalars().all()
            story_keys = [s.jira_key for s in stories]
            keys = sorted({epic_key, *story_keys})

            epic_repo = None
            if epic.project_repo_id is not None:
                project_repo = session.get(ProjectRepo, epic.project_repo_id)
                epic_repo = project_repo.path if project_repo is not None else None

            worktrees = session.execute(
                select(StoryWorktree).where(StoryWorktree.story_key.in_(story_keys)).order_by(StoryWorktree.id)
            ).scalars().all()
            pull_requests = session.execute(
                select(PullRequest).where(PullRequest.story_key.in_(story_keys)).order_by(PullRequest.id)
            ).scalars().all()
            epic_pull_requests = session.execute(
                select(EpicPullRequest).where(EpicPullRequest.epic_key == epic_key).order_by(EpicPullRequest.id)
            ).scalars().all()
            # The epic branch is made in every repo on the project at the
            # epic's first dispatch (elstin/daemon/main.py), not only in the
            # repos its stories wrote.
            project_repos = session.execute(
                select(ProjectRepo.path).where(ProjectRepo.project_id == epic.project_id, ProjectRepo.kind == "repo")
            ).scalars().all()

            return {
                "epic": epic_key,
                "project_id": epic.project_id,
                "integration_branch": epic.integration_branch,
                "epic_repo": epic_repo,
                "project_repos": sorted(p for p in project_repos if p),
                "keys": keys,
                "stories": [_story_to_dict(session, s) for s in stories],
                "worktrees": [
                    {
                        "story_key": w.story_key, "repo_label": w.repo_label, "repo_path": w.repo_path,
                        "worktree_path": w.worktree_path, "branch": w.branch,
                    }
                    for w in worktrees
                ],
                "pull_requests": [
                    {"story_key": p.story_key, "repo_label": p.repo_label, "number": p.number, "url": p.url, "state": p.state}
                    for p in pull_requests
                ] + [
                    # The epic's own pull request — never closed by a removal.
                    {"story_key": epic_key, "repo_label": p.repo_label, "number": p.number, "url": p.url, "state": p.state}
                    for p in epic_pull_requests if p.number
                ],
            }

    def delete_ticket_records(self, epic_key: str) -> dict[str, int]:
        """Delete the ticket's rows in one transaction, children before
        parents (pull requests and worktrees reference stories, stories
        reference the epic). Returns how many of each went, plus how many
        lessons lost this ticket as evidence and how many were dropped for
        having none left. Zeros for an unknown epic; nothing else is touched."""
        with session_scope() as session:
            story_keys = list(session.execute(select(Story.jira_key).where(Story.epic_key == epic_key)).scalars())
            keys = sorted({epic_key, *story_keys})

            def gone(statement) -> int:
                return session.execute(statement).rowcount or 0

            # Lessons drawn from this ticket's runs lose it as evidence — see
            # elstin/state/manager_lesson.py's forget_evidence. Same transaction,
            # so a removal that fails part-way leaves them untouched.
            lessons_detached, lessons_dropped = forget_evidence(session, keys)

            counts = {
                "lessons_detached": lessons_detached,
                "lessons_dropped": lessons_dropped,
                "activity_events": gone(delete(ActivityEvent).where(ActivityEvent.run_id.in_(keys))),
                "usage_rows": gone(delete(StoryUsage).where(StoryUsage.run_id.in_(keys))),
                "run_outcomes": gone(delete(StoryRunOutcome).where(StoryRunOutcome.run_id.in_(keys))),
                "pull_requests": gone(delete(PullRequest).where(PullRequest.story_key.in_(story_keys))),
                "epic_pull_requests": gone(delete(EpicPullRequest).where(EpicPullRequest.epic_key == epic_key)),
                "worktree_rows": gone(delete(StoryWorktree).where(StoryWorktree.story_key.in_(story_keys))),
                "watermarks": gone(delete(JiraWatermark).where(JiraWatermark.jira_key.in_(keys))),
                "stories": gone(delete(Story).where(Story.epic_key == epic_key)),
                "epics": gone(delete(Epic).where(Epic.jira_key == epic_key)),
            }
            return counts
