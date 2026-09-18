"""stories.retry_count/failure_stage — lets a human retry a story the
reconciler halted the epic on (forge/orchestration/reconciler.py's
`any(status == "failed")` check), instead of the epic staying dead until
someone edits the database by hand.

`failure_stage` records which of the two distinct things "failed" can mean
(see forge/daemon/main.py): "run" — the LangGraph run itself ended in
status=failed (verifier exhausted its iterations, or a node failed
outright) with no PR opened yet, so recovery is a full redispatch — versus
"merge" — the story's own run succeeded but merging its branch into the
epic's integration branch conflicted, so recovery is only a re-attempted
merge, not redoing the implementation. `retry_story` (forge/state/
manager_story.py) reads this to pick the right recovery path.

Existing rows backfill to retry_count=0 / failure_stage=NULL: no story
synced before this migration ever recorded which stage it failed at, and a
NULL failure_stage on a story that happens to already be "failed" simply
means retry isn't offered for it until it fails again post-migration and
gets tagged.

Revision ID: 20260818_0011
Revises: 20260818_0010
Create Date: 2026-08-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0011"
down_revision: Union[str, None] = "20260818_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("stories", sa.Column("failure_stage", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "failure_stage")
    op.drop_column("stories", "retry_count")
