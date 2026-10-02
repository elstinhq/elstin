"""story_run_outcomes.peak_memory_mb / peak_memory_command — how heavy a run's commands were.

The gate and the dev step run builds and tests (a JVM, a Node process and whatever
they spawn), and up to `max_parallel_stories` stories do so at once. Nothing said how
much memory those commands needed, so nothing could say whether three at once fit on
the machine. forge/tools/process_monitor.py samples each command's process tree while
it runs, and the highest figure a run reached is kept here with the command that
reached it (forge/services/memory_usage.py).

Backfill: none — NULL for every existing row, and NULL means "not measured", which is
the truth for every run before this existed. It is deliberately not zero: "measured as
zero" and "never measured" are different claims (see StoryRunOutcome's docstring).

Revision ID: 20260930_0034
Revises: 20260930_0033
Create Date: 2026-09-30
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0034"
down_revision: Union[str, None] = "20260930_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("story_run_outcomes", sa.Column("peak_memory_mb", sa.Integer(), nullable=True))
    op.add_column("story_run_outcomes", sa.Column("peak_memory_command", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("story_run_outcomes", "peak_memory_command")
    op.drop_column("story_run_outcomes", "peak_memory_mb")
