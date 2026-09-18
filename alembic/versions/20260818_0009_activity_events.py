"""activity_events — durable per-run planner/dev/verifier/qa activity log.

Replaces forge/services/run_activity.py's in-memory ring buffer, which lost
every run's history on process restart and couldn't be queried across
restarts. run_id is a story's own JIRA key in production (== LangGraph
thread_id by construction), but deliberately not a foreign key to
stories.jira_key — forge/orchestration/graph.py logs activity keyed on
whatever thread_id it's given, an opaque identifier as far as the graph is
concerned (tests/test_graph.py runs it against synthetic uuid4() thread_ids
with no backing story row).

Revision ID: 20260818_0009
Revises: 20260817_0008
Create Date: 2026-08-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0009"
down_revision: Union[str, None] = "20260817_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("files", sa.JSON(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_activity_events_run_id", "activity_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_activity_events_run_id", table_name="activity_events")
    op.drop_table("activity_events")
