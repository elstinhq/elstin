"""Actions a story's plan leaves to a person.

`stories.person_actions`: [{action, reason}] from the story's latest plan —
things its acceptance criteria lean on that no agent here can do: run a script
against data the agent cannot reach and record the result, check open pull
requests or other branches, publish, review or sign off
(elstin/agents/roles/plan_gate.py's person_actions_of). The non-file
counterpart of `read_only_changes`, kept outside the checkpoint for the same
reason: the board and the epic's pull request can show them without opening
one.

Backfill: none — NULL for every existing row, the same as "no such actions".
Each story's next plan sets it.

Revision ID: 20261003_0036
Revises: 20260930_0035
Create Date: 2026-10-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261003_0036"
down_revision: Union[str, None] = "20260930_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("person_actions", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "person_actions")
