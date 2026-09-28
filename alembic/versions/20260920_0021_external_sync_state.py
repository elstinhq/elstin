"""What the last sync learned about a ticket that forge has not acted on.

Revision 0020 recorded where each ticket came from. Sync now also refreshes
content and notices closure, and both need somewhere to land that does not
disturb a story forge is already working on:

- `epics.external_status` / `stories.external_status`: the status text the
  source last reported ("In Progress", "Done"), for display.
- `external_closed`: the source says the ticket is done, closed, or gone.
  A story that has not started is skipped (parked_reason
  'closed_externally'); one in flight is only flagged; an epic is put on
  hold (manual_gate).
- `stories.external_pending` / `external_changed_at`: content the source
  changed after forge planned or started the story, held back for a human
  to apply, apply-and-replan, or dismiss. NULL means nothing waiting.
  Epics have no pending column: their prose is applied directly.

`external_hash` (0020) now means "digest of the source content last
resolved" — applied, or dismissed by a human — so a dismissed edit is not
re-flagged on every sweep.

Backfill: `external_closed` false, everything else NULL — no existing row
is closed, changed or unread until a sweep says so.

Revision ID: 20260920_0021
Revises: 20260920_0020
Create Date: 2026-09-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0021"
down_revision: Union[str, None] = "20260920_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("epics", "stories"):
        op.add_column(table, sa.Column("external_status", sa.String(128), nullable=True))
        op.add_column(table, sa.Column("external_closed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("stories", sa.Column("external_pending", sa.JSON(), nullable=True))
    op.add_column("stories", sa.Column("external_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "external_changed_at")
    op.drop_column("stories", "external_pending")
    for table in ("stories", "epics"):
        op.drop_column(table, "external_closed")
        op.drop_column(table, "external_status")
