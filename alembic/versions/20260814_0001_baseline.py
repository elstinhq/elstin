"""baseline schema: app_config, epics, stories, pull_requests, jira_watermarks

Revision ID: 20260814_0001
Revises:
Create Date: 2026-08-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "epics",
        sa.Column("jira_key", sa.String(64), primary_key=True),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False, server_default="planning"),
        sa.Column("levels", sa.JSON(), nullable=True),
        sa.Column("integration_branch", sa.String(256), nullable=True),
        sa.Column("plan_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "stories",
        sa.Column("jira_key", sa.String(64), primary_key=True),
        sa.Column("epic_key", sa.String(64), sa.ForeignKey("epics.jira_key"), nullable=False),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("wave", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("thread_id", sa.String(64), nullable=True),
        sa.Column("worktree_path", sa.Text(), nullable=True),
        sa.Column("branch", sa.String(256), nullable=True),
        sa.Column("parked_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_stories_epic_key", "stories", ["epic_key"])

    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("story_key", sa.String(64), sa.ForeignKey("stories.jira_key"), nullable=False, unique=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(512), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "jira_watermarks",
        sa.Column("jira_key", sa.String(64), primary_key=True),
        sa.Column("last_comment_id", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("jira_watermarks")
    op.drop_table("pull_requests")
    op.drop_index("ix_stories_epic_key", table_name="stories")
    op.drop_table("stories")
    op.drop_table("epics")
    op.drop_table("app_config")
