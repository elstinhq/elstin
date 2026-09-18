"""projects, project_repos, project_config; epics gain project_id/
project_repo_id and drop repo; stories drop repo

Assumes zero pre-existing epic rows (true for every environment this
unreleased tool has run in so far) — epics.project_id is added NOT NULL
with no default/backfill.

Revision ID: 20260815_0004
Revises: 20260815_0003
Create Date: 2026-08-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0004"
down_revision: Union[str, None] = "20260815_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "project_repos",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("label", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_repos_project_id", "project_repos", ["project_id"])

    op.create_table(
        "project_config",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.add_column("epics", sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False))
    op.create_index("ix_epics_project_id", "epics", ["project_id"])
    op.add_column("epics", sa.Column("project_repo_id", sa.Integer(), sa.ForeignKey("project_repos.id"), nullable=True))
    op.drop_column("epics", "repo")

    op.drop_column("stories", "repo")


def downgrade() -> None:
    op.add_column("stories", sa.Column("repo", sa.String(256), nullable=True))

    op.add_column("epics", sa.Column("repo", sa.String(256), nullable=True))
    op.drop_column("epics", "project_repo_id")
    op.drop_index("ix_epics_project_id", table_name="epics")
    op.drop_column("epics", "project_id")

    op.drop_table("project_config")
    op.drop_index("ix_project_repos_project_id", table_name="project_repos")
    op.drop_table("project_repos")
    op.drop_table("projects")
