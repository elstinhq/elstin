"""stories.summary, stories.gate_approved

Revision ID: 20260814_0002
Revises: 20260814_0001
Create Date: 2026-08-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0002"
down_revision: Union[str, None] = "20260814_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("summary", sa.String(512), nullable=False, server_default=""))
    op.add_column("stories", sa.Column("gate_approved", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("stories", "gate_approved")
    op.drop_column("stories", "summary")
