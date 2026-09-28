"""Stories merge locally; only the epic opens a pull request.

A story used to open its own GitHub pull request, wait for CI and a review,
then merge into the epic's integration branch. Now a finished story is
committed on its branch, brought up to date with the epic branch,
re-verified and fast-forwarded into it — all locally (forge/orchestration/
integration.py) — and the epic opens the one pull request once every story
has merged. So:

- `stories.merge_attempts` (int, default 0): how many times landing a story
  sent it back to dev (a conflict with the epic branch, or checks failing on
  the combined code). The merge queue caps it; a retry resets it.
  Backfill: 0 — no story has been through the new queue yet.
- Story statuses `awaiting_pr_open` and `awaiting_pr_review` no longer
  exist. A story in either has passed its run and has its work committed, so
  it becomes `ready_to_merge` and the merge queue lands it; the gate reasons
  parked on it (`pr_open_gate`, `pr_review_gate`, `ci_pending`, `ci_failed`)
  are cleared. A per-story pull request already open on GitHub is left alone
  — forge no longer tracks or merges it; close it by hand.
- The settings `require_pr_open_approval` and `require_pr_review` are
  removed, globally and per project.

Downgrade restores the column set only; statuses stay `ready_to_merge`,
which an older forge does not know, so a downgrade needs those stories
retried by hand.

Revision ID: 20260926_0026
Revises: 20260926_0025
Create Date: 2026-09-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0026"
down_revision: Union[str, None] = "20260926_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REMOVED_SETTINGS = ("require_pr_open_approval", "require_pr_review")
_PR_GATE_REASONS = ("pr_open_gate", "pr_review_gate", "ci_pending", "ci_failed")


def upgrade() -> None:
    op.add_column("stories", sa.Column("merge_attempts", sa.Integer(), nullable=False, server_default="0"))

    stories = sa.table(
        "stories", sa.column("status", sa.String), sa.column("parked_reason", sa.String),
        sa.column("gate_approved", sa.Boolean),
    )
    op.execute(
        stories.update()
        .where(stories.c.status.in_(("awaiting_pr_open", "awaiting_pr_review")))
        .values(status="ready_to_merge", gate_approved=False)
    )
    op.execute(stories.update().where(stories.c.parked_reason.in_(_PR_GATE_REASONS)).values(parked_reason=None))

    for table in ("app_config", "project_config"):
        config = sa.table(table, sa.column("key", sa.String))
        op.execute(config.delete().where(config.c.key.in_(_REMOVED_SETTINGS)))


def downgrade() -> None:
    op.drop_column("stories", "merge_attempts")
