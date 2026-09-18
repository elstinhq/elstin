"""epics.manual_gate — a human-set hold on a single epic, independent of the
plan-approval/PR-review gates (which apply per-story, driven by project
config). Defaults false so existing epics keep running unaffected.

Revision ID: 20260816_0005
Revises: 20260815_0004
Create Date: 2026-08-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0005"
down_revision: Union[str, None] = "20260815_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "epics",
        sa.Column("manual_gate", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("epics", "manual_gate")
