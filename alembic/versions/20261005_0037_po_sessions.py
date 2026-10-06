"""PO sessions: a person and the PO agent drafting a backlog together.

`po_sessions` holds one piece of backlog planning — brief, mode, the agent's
draft (and a person's edits to it), the questions it asked, and the
`publish_result` checkpoint that makes publishing resumable. `po_messages` is
the conversation around the draft.

Backfill: none — both tables are new, so there are no existing rows to give a
meaning to. (`draft_version`, `edited_by_person` and `cost_usd` carry server
defaults only so a row written by hand, outside elstin, is still valid.)

Revision ID: 20261005_0037
Revises: 20261003_0036
Create Date: 2026-10-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261005_0037"
down_revision: Union[str, None] = "20261003_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "po_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("epic_key", sa.String(length=64), nullable=True),
        sa.Column("jira_project_key", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("edited_by_person", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("person_edits", sa.JSON(), nullable=True),
        sa.Column("questions", sa.JSON(), nullable=True),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("publish_result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_po_sessions_project_id", "po_sessions", ["project_id"])
    op.create_index("ix_po_sessions_status", "po_sessions", ["status"])

    op.create_table(
        "po_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("po_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_po_messages_session_id", "po_messages", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_po_messages_session_id", table_name="po_messages")
    op.drop_table("po_messages")
    op.drop_index("ix_po_sessions_status", table_name="po_sessions")
    op.drop_index("ix_po_sessions_project_id", table_name="po_sessions")
    op.drop_table("po_sessions")
