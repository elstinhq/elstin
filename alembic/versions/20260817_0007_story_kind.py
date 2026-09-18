"""stories.kind/kind_reason — routes a story to the dev loop or to epic QA.

Existing rows default to "dev", which is the correct backfill: every story
synced before this migration was dispatched through planner -> dev ->
verifier, so labelling them anything else would misdescribe what already
happened. Re-syncing an epic does not reclassify existing stories (see
StateManager.create_story's no-op-on-existing contract), so a human wanting
old rows reclassified sets them explicitly via PUT /api/stories/{key}/kind.

Revision ID: 20260817_0007
Revises: 20260816_0006
Create Date: 2026-08-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0007"
down_revision: Union[str, None] = "20260816_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("kind", sa.String(16), nullable=False, server_default="dev"))
    op.add_column("stories", sa.Column("kind_reason", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "kind_reason")
    op.drop_column("stories", "kind")
