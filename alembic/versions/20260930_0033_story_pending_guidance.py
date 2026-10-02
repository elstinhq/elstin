"""stories.pending_guidance — messages a person sent to a running agent.

A person can send a message to a story's agent while it works
(POST /api/stories/{key}/guidance); the agent reads it at the start of its next
turn (forge/agents/loop.py's `pending_messages`), and it is then removed. Until
then it waits here as [{text, at}], oldest first, so a message sent between two
turns is not lost and nothing needs to be held in the process's memory.

Backfill: none — NULL for every existing row, meaning "nothing waiting", which is
what every story had before this existed. Whatever is still waiting when a run
ends is cleared and reported in the activity feed, so nothing lingers here.

Revision ID: 20260930_0033
Revises: 20260930_0032
Create Date: 2026-09-30
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0033"
down_revision: Union[str, None] = "20260930_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("pending_guidance", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "pending_guidance")
