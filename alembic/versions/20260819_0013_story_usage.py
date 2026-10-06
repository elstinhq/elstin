"""story_usage — accumulated per-run, per-phase token/cost/timing rollups.

New table, no backfill: existing ActivityEvent token-usage rows (type="token")
are not migrated into it. This table is a forward-looking cache that phase
runners populate on every future planner/dev/verifier/qa invocation (see
elstin/services/usage.py's record_phase_usage) — stories that ran before this
migration simply have no rows here, and the UI should render that as "no
usage data" rather than a zero-cost run.

Revision ID: 20260819_0013
Revises: 20260819_0012
Create Date: 2026-08-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0013"
down_revision: Union[str, None] = "20260819_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "story_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("cost_source", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("api_duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("invocation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "phase", "model", name="uq_story_usage_run_phase_model"),
    )
    op.create_index("ix_story_usage_run_id", "story_usage", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_story_usage_run_id", table_name="story_usage")
    op.drop_table("story_usage")
