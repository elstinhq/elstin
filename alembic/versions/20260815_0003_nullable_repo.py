"""epics.repo and stories.repo become nullable — an epic can be synced from
JIRA before a target repo is chosen

Revision ID: 20260815_0003
Revises: 20260814_0002
Create Date: 2026-08-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0003"
down_revision: Union[str, None] = "20260814_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("epics", "repo", existing_type=sa.String(256), nullable=True)
    op.alter_column("stories", "repo", existing_type=sa.String(256), nullable=True)


def downgrade() -> None:
    op.alter_column("stories", "repo", existing_type=sa.String(256), nullable=False)
    op.alter_column("epics", "repo", existing_type=sa.String(256), nullable=False)
