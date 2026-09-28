"""Reference folders, and read-only repos.

`project_repos.kind`: "repo" (a git repo, as every row has been) or
"reference" — a plain folder read for context (docs, notes, a CLAUDE.md
about the repos beside it), never written or branched.
`project_repos.read_only`: a git repo every run may read but no plan may
change (a shared contract repo, say).

Backfill: every existing row is a writable git repo — kind "repo",
read_only false — which is what they all were.

Revision ID: 20260927_0029
Revises: 20260926_0028
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0029"
down_revision: Union[str, None] = "20260926_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project_repos", sa.Column("kind", sa.String(16), nullable=False, server_default="repo"))
    op.add_column("project_repos", sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("project_repos", "read_only")
    op.drop_column("project_repos", "kind")
