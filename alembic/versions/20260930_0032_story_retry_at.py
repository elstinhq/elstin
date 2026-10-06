"""stories.retry_at — when a story parked on a provider limit is retried.

A run that fails because the provider refused calls on a limit (a rate window,
or a quota that resets) used to be a plain "failed" story waiting for a person.
When the provider says when the limit lifts, elstin records the instant here with
`parked_reason = 'usage_limit'`, and the daemon retries the story on its own
once it has passed (elstin/daemon/main.py's `_resume_stories_past_their_limit`).
The story stays "failed" until then, so nothing downstream of it starts, exactly
as before.

Backfill: none — NULL for every existing row, meaning "no automatic retry is
scheduled", which is what every story failed before this column existed. A
failed story whose parked_reason is not 'usage_limit' is never auto-retried.

Revision ID: 20260930_0032
Revises: 20260929_0031
Create Date: 2026-09-30
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0032"
down_revision: Union[str, None] = "20260929_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "retry_at")
