"""One row per (story, repo) the story has a worktree in.

A story used to write exactly one repo, so `Story.worktree_path` and
`Story.branch` were enough. Once the *plan* decides which repos a story
writes (elstin/tools/workspace.py), a story can have a worktree and a
branch in each of several repos, and two scalar columns cannot hold that.

Story.worktree_path/branch are kept as a mirror of the DEFAULT repo's
entry, so the dashboard and every single-repo code path keep working
unchanged. This table is the complete picture.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from elstin.db.base import Base
from elstin.db.types import UtcDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoryWorktree(Base):
    __tablename__ = "story_worktrees"
    __table_args__ = (UniqueConstraint("story_key", "repo_label", name="uq_story_worktrees_story_repo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_key: Mapped[str] = mapped_column(ForeignKey("stories.jira_key"), nullable=False, index=True)

    # The workspace label this worktree belongs to — the same label a
    # plan path's `@prefix` carries (elstin/tools/workspace.py).
    repo_label: Mapped[str] = mapped_column(String(64), nullable=False)
    # The repo's own checkout, recorded so a later commit/push/PR does not
    # have to re-derive it from config that may since have changed.
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    project_repo_id: Mapped[int | None] = mapped_column(ForeignKey("project_repos.id"), nullable=True)

    worktree_path: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(String(256), nullable=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_utcnow, nullable=False)
