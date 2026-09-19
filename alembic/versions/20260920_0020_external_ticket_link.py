"""Where each ticket on the local board came from.

forge's Postgres board is the source of truth for the agents; JIRA (and,
next, GitHub Issues) is the source of truth for what a ticket *says*. Until
now the only trace of that was that the local key happened to be a JIRA
key. These columns record the link explicitly, so a ticket can come from a
source other than JIRA, or from none:

- `epics` / `stories`: `external_source` ("jira" | "github" | NULL for a
  ticket created natively on the board), `external_id` (the ticket's id in
  that system), `external_synced_at` (last time it was read from there) and
  `external_hash` (a digest of the content as last applied, so a later sync
  can tell whether the ticket changed).
- `projects.ticket_source`: which source a project's tickets are pulled
  from.

Backfill: every existing epic and story was synced from JIRA and its local
key *is* the JIRA key, so they become `external_source = 'jira'` with
`external_id = jira_key`. `external_synced_at` and `external_hash` stay
NULL — nothing recorded when they were last read or what they said — and a
NULL hash simply means "baseline unknown" to the sync. Every existing
project becomes `ticket_source = 'jira'` via the column default, so an
installed forge behaves exactly as before until a project is switched.

Revision ID: 20260920_0020
Revises: 20260919_0019
Create Date: 2026-09-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0020"
down_revision: Union[str, None] = "20260919_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("epics", "stories"):
        op.add_column(table, sa.Column("external_source", sa.String(16), nullable=True))
        op.add_column(table, sa.Column("external_id", sa.String(256), nullable=True))
        op.add_column(table, sa.Column("external_synced_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(table, sa.Column("external_hash", sa.String(64), nullable=True))
        op.execute(f"UPDATE {table} SET external_source = 'jira', external_id = jira_key")

    op.add_column("projects", sa.Column("ticket_source", sa.String(16), nullable=False, server_default="jira"))


def downgrade() -> None:
    op.drop_column("projects", "ticket_source")
    for table in ("stories", "epics"):
        op.drop_column(table, "external_hash")
        op.drop_column(table, "external_synced_at")
        op.drop_column(table, "external_id")
        op.drop_column(table, "external_source")
