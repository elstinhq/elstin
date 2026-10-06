"""The epic's pull request — the only one elstin opens.

Stories now merge into the epic branch locally (20260926_0026); once all of
them have, elstin pushes the epic branch and opens one pull request from it
into the default branch, per repo the epic's stories wrote.
`epic_pull_requests` records it: number/url (NULL until it could be opened,
with `error` saying why), `state` ("open" | "merged" | "closed") and
`ci_status` ("pending" | "success" | "failure"), refreshed while it is open.

Backfill: none — no epic has opened a pull request before this table
existed. An epic already at phase "complete" stays complete; one still in
progress opens its pull request when its last story merges.

Revision ID: 20260926_0027
Revises: 20260926_0026
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0027"
down_revision: Union[str, None] = "20260926_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "epic_pull_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("epic_key", sa.String(64), sa.ForeignKey("epics.jira_key"), nullable=False, index=True),
        sa.Column("repo_label", sa.String(64), nullable=False, server_default=""),
        sa.Column("repo_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column("branch", sa.String(256), nullable=False, server_default=""),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(512), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="open"),
        sa.Column("ci_status", sa.String(16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("epic_key", "repo_label", name="uq_epic_pull_requests_epic_repo"),
    )


def downgrade() -> None:
    op.drop_table("epic_pull_requests")
