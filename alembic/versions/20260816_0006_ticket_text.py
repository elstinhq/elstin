"""epics.summary/description, stories.description — text already fetched
from JIRA during sync (see forge/services/jira_service.py's JiraTicket) but
previously discarded. Populated once at sync time, same "set at creation,
never overwritten by a re-sync" convention as everything else create_epic/
create_story already own.

Revision ID: 20260816_0006
Revises: 20260816_0005
Create Date: 2026-08-16
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_0006"
down_revision: Union[str, None] = "20260816_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("epics", sa.Column("summary", sa.String(512), nullable=False, server_default=""))
    op.add_column("epics", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("stories", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "description")
    op.drop_column("epics", "description")
    op.drop_column("epics", "summary")
