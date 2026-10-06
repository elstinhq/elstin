"""stories.coverage_gap/coverage_gap_detail — flags a story whose only
verifier blocker was a coverage-threshold miss (tests passed, coverage
percentage below the repo's configured bar), distinct from a real
implementation failure.

Existing rows default to false, which is the correct backfill: no story
synced before this migration was ever classified this way (the detection
this flag records didn't exist yet), so labelling old rows otherwise would
assert something never actually checked. See elstin/tools/vue_tools.py's
coverage-only-failure detection and elstin/orchestration/reconciler.py's
pr_open_gate, which is forced on for a coverage-gap story even when a
project has require_pr_open_approval turned off — a story that didn't fully
verify shouldn't ride along on a gate configured for the fully-verified
case.

Revision ID: 20260818_0010
Revises: 20260818_0009
Create Date: 2026-08-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0010"
down_revision: Union[str, None] = "20260818_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("coverage_gap", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("stories", sa.Column("coverage_gap_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "coverage_gap_detail")
    op.drop_column("stories", "coverage_gap")
