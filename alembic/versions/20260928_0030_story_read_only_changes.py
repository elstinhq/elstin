"""Changes a story needs in a read-only repo, for a person to make.

`stories.read_only_changes`: [{path, purpose}] from the story's latest plan —
files in a read-only repo or reference folder the ticket needs changed,
which forge may not change (forge/agents/roles/plan_gate.py's
normalize_plan). Kept outside the checkpoint so the board and the epic's
pull request can show them without opening one.

Backfill: none — NULL for every existing row, the same as "no such
changes". Each story's next plan sets it.

Revision ID: 20260928_0030
Revises: 20260927_0029
Create Date: 2026-09-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0030"
down_revision: Union[str, None] = "20260927_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("read_only_changes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "read_only_changes")
