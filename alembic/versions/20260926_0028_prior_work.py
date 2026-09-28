"""Work already done on a ticket, looked up before forge makes a branch.

`stories.prior_work` and `epics.prior_work` hold what the lookup in
forge/services/prior_work.py found for the ticket's key: per repo, the
branches carrying it, how far ahead of the default branch they are, and
whether their work is already in it — plus a human's pick where several
diverged. A story's branch (and the epic branch) starts from what it found.

Backfill: none — both stay NULL. The daemon looks up only a story that is
still pending and has no branch (and an epic with no integration branch),
so a row that is already underway is never looked up or re-based; a pending
story that has not started yet is looked up on the next poll.

Revision ID: 20260926_0028
Revises: 20260926_0027
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0028"
down_revision: Union[str, None] = "20260926_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("prior_work", sa.JSON(), nullable=True))
    op.add_column("epics", sa.Column("prior_work", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("epics", "prior_work")
    op.drop_column("stories", "prior_work")
