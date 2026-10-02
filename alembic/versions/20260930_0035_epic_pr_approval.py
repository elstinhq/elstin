"""epics.pr_approval — a person's say over whether the epic's pull request opens.

With `require_pr_approval` on, an epic whose stories have all merged does not push
its branch or open its pull request on its own: it parks as 'awaiting' and a person
chooses (POST /api/epics/{key}/pr-approval) to 'approved' (open it) or 'skipped'
(keep the branch local; the epic completes without a pull request).

Backfill: none — NULL for every existing row, meaning "no decision asked for", which
is what every epic had before this existed. An epic that already opened its pull
request is not affected. The column only matters while the setting is on and every
story has merged.

Revision ID: 20260930_0035
Revises: 20260930_0034
Create Date: 2026-09-30
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0035"
down_revision: Union[str, None] = "20260930_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("epics", sa.Column("pr_approval", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("epics", "pr_approval")
