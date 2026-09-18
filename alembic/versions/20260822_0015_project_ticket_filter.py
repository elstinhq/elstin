"""project_ticket_filters — one saved JIRA query per project, the
background-ingest alternative to typing one issue key at a time into
"Add ticket" (forge/api/epic_routes.py's POST /api/epics/sync, which stays).

New table, no backfill and deliberately no default row per project:
absence of a row is the meaningful state ("this project only takes tickets
added by hand"), so every project that existed before this migration keeps
exactly the behavior it had. `enabled` defaults to false for the same
reason — a filter saved by a future UI never starts sweeping JIRA in the
background until someone turns it on.

The structured columns hold the *parts* of the query, not compiled JQL;
forge/services/jira_filter.py's build_jql assembles them (plus the
free-form `extra_jql` escape hatch) on every use, so a stored filter can be
round-tripped back into the UI's fields.

The `last_*` columns are observability only — the sweep re-derives what to
do from JIRA plus the epics/stories already in Postgres, so they are safe
to be NULL forever on a filter that has never run.

Revision ID: 20260822_0015
Revises: 20260819_0014
Create Date: 2026-08-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0015"
down_revision: Union[str, None] = "20260819_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_ticket_filters",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("jira_project_key", sa.String(64), nullable=True),
        sa.Column("assignee", sa.String(256), nullable=True),
        sa.Column("statuses", sa.JSON(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=True),
        sa.Column("issue_types", sa.JSON(), nullable=True),
        sa.Column("extra_jql", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_matched_count", sa.Integer(), nullable=True),
        sa.Column("last_synced_count", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("project_ticket_filters")
