"""stories.pr_open_error — surfaces a failed PR-open attempt in the UI.

_maybe_open_pull_request (forge/daemon/main.py) is intentionally
best-effort: a push or GitHub-API failure never blocks the story's own
progress through forge's approval gates. Before this column existed, that
failure only ever reached a logger.warning() call, invisible outside the
daemon's own stdout — a story could sit at "awaiting PR review" with no PR
ever opened and no way to tell why short of reading server logs. This
records the failure reason so the UI can show it, and is cleared on the
next successful PR-open attempt.

Existing rows backfill to NULL — no story synced before this migration ever
recorded a PR-open failure; that's indistinguishable from "never tried" or
"succeeded," and doesn't need to be.

Revision ID: 20260819_0014
Revises: 20260819_0013
Create Date: 2026-08-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0014"
down_revision: Union[str, None] = "20260819_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("pr_open_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "pr_open_error")
