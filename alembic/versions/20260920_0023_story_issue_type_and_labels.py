"""The ticket's own issue type and labels, kept on the story.

Both already existed on `Ticket` at sync time (forge/services/ticket_source.py)
and were handed to `classify_story_kind`, but only the dev/qa verdict was
persisted — the signals behind it were thrown away. Branch naming needs them:
`feat/` vs `fix/` is exactly the distinction a label carries, and for a GitHub
issue the labels are the ONLY place it lives (every GitHub issue's issue_type
is the literal "Issue"; `enhancement` and `bug` are labels).

- `stories.issue_type`: the ticket's type as the source reports it
  ("Story", "Bug", "Issue", …).
- `stories.labels`: the ticket's labels, as a JSON array of strings.

Backfill: both NULL for existing rows. Nothing recomputes them for a story
already on the board — a re-sync leaves an existing story's fields alone, the
same rule `Story.kind` already follows — so a story created before this
migration keeps a NULL type and falls back to the default branch prefix
(forge/tools/worktree.py's DEFAULT_TYPE_PREFIX). Existing branches are not
renamed: they are recorded in `story_worktrees.branch` and, once pushed, own
an open pull request that a rename would close.

Revision ID: 20260920_0023
Revises: 20260920_0022
Create Date: 2026-09-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0023"
down_revision: Union[str, None] = "20260920_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("issue_type", sa.String(length=64), nullable=True))
    op.add_column("stories", sa.Column("labels", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "labels")
    op.drop_column("stories", "issue_type")
