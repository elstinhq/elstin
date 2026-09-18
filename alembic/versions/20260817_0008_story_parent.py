"""stories.parent_key — restores the JIRA hierarchy that sync was flattening.

Before this, `epic_sync` treated every descendant returned by
`JiraService.get_ticket_tree`'s breadth-first walk as a direct child of the
epic. For PROJ-3480 that turned 6 stories + 25 sub-tasks into 31 flat,
independently-dispatched stories: the same work planned twice (once as the
story, once as each of its sub-tasks) and sub-tasks that depend on each
other's code sent to concurrent worktrees off the same base.

Existing rows backfill to NULL, which reads as "top-level story". That is
the pre-migration behaviour exactly — every existing row WAS treated as a
direct child of its epic — so the backfill preserves the meaning of what
already ran. Re-syncing an epic does not rewrite existing rows
(create_story no-ops on conflict), so an already-flattened epic keeps its
flat shape until it is re-synced from scratch.

Revision ID: 20260817_0008
Revises: 20260817_0007
Create Date: 2026-08-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0008"
down_revision: Union[str, None] = "20260817_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("parent_key", sa.String(64), nullable=True))
    op.create_index("ix_stories_parent_key", "stories", ["parent_key"])


def downgrade() -> None:
    op.drop_index("ix_stories_parent_key", table_name="stories")
    op.drop_column("stories", "parent_key")
