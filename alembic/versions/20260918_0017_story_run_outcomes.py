"""story_run_outcomes — per-run work counters (the shape of a run).

elstin/db/models/story_usage.py records what a run cost. Nothing recorded
how much work it took: gate failures, verifier rejections, dev passes and
turns, and how the dev loop terminated. Those lived only inside the
LangGraph checkpoint blob and as prose in the activity feed, so no question
about whether the Tier 1 gate or the turn budgets are set sensibly could be
answered from data. See elstin/db/models/run_outcome.py's docstring.

Backfill: none, and none is possible. Every counter here is observed while
a run executes (elstin/services/usage.py:record_run_outcome, called from
elstin/orchestration/graph.py's nodes), and runs that finished before this
table existed never emitted them. Existing stories therefore have NO row
at all rather than a row of zeros — a missing row means "not measured",
which is materially different from "measured as zero" and readers must
render it that way. Rows appear from the next dispatch onward.

Revision ID: 20260918_0017
Revises: 20260912_0016
Create Date: 2026-09-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0017"
down_revision: Union[str, None] = "20260912_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "story_run_outcomes",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("planner_turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("explorer_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dev_passes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dev_turns", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("dev_terminal_reason", sa.String(32), nullable=True),
        sa.Column("dev_soft_stops", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gate_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gate_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verifier_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verifier_rejections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("story_run_outcomes")
