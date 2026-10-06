"""Per-repo worktrees and pull requests for a story.

The writable repo is no longer bound to an epic or a story: the approved
plan names which repos it touches (elstin/tools/workspace.py), so one story
can have a worktree, a branch and a pull request in each of several repos.
Two scalar columns on `stories` cannot hold that.

- `story_worktrees` is the full picture, one row per (story, repo).
  `stories.worktree_path`/`branch` stay as a mirror of the DEFAULT repo's
  row so the dashboard and every single-repo path keep working unchanged.
- `pull_requests` gains `repo_label` and its uniqueness moves from
  `story_key` alone to the (story_key, repo_label) pair.

Backfill: `pull_requests.repo_label` defaults to the empty string, which
means "the story's default repo" and is exactly what every pre-existing
row is. Nothing is rewritten. `story_worktrees` starts empty: a story
dispatched before this migration keeps using `stories.worktree_path`, and
readers treat a missing row set as "the default repo only", so an
in-flight story is unaffected. Rows appear from the next dispatch onward.

`stories.project_repo_id` (revision 0018) is deliberately left in place
but is no longer consulted for dispatch — the plan decides. It is kept
only so an operator can still pin which repo an *unprefixed* path means.

Revision ID: 20260919_0019
Revises: 20260919_0018
Create Date: 2026-09-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0019"
down_revision: Union[str, None] = "20260919_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "story_worktrees",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("story_key", sa.String(64), sa.ForeignKey("stories.jira_key"), nullable=False, index=True),
        sa.Column("repo_label", sa.String(64), nullable=False),
        sa.Column("repo_path", sa.Text(), nullable=False),
        sa.Column("project_repo_id", sa.Integer(), sa.ForeignKey("project_repos.id"), nullable=True),
        sa.Column("worktree_path", sa.Text(), nullable=False),
        sa.Column("branch", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("story_key", "repo_label", name="uq_story_worktrees_story_repo"),
    )

    op.add_column(
        "pull_requests",
        sa.Column("repo_label", sa.String(64), nullable=False, server_default=""),
    )
    # The old constraint allowed one PR per story; a story spanning repos
    # needs one per repo.
    op.drop_constraint("pull_requests_story_key_key", "pull_requests", type_="unique")
    op.create_unique_constraint(
        "uq_pull_requests_story_repo", "pull_requests", ["story_key", "repo_label"]
    )
    op.create_index("ix_pull_requests_story_key", "pull_requests", ["story_key"])


def downgrade() -> None:
    op.drop_index("ix_pull_requests_story_key", table_name="pull_requests")
    op.drop_constraint("uq_pull_requests_story_repo", "pull_requests", type_="unique")
    op.create_unique_constraint("pull_requests_story_key_key", "pull_requests", ["story_key"])
    op.drop_column("pull_requests", "repo_label")
    op.drop_table("story_worktrees")
