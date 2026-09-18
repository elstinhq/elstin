"""project_repos.clone_url — records the git URL a repo was cloned from
(forge/services/repo_clone.py). Projects now always clone into storage the
API process controls (see forge/db/models/project.py's ProjectRepo
docstring) instead of pointing at a pre-existing local path, so the UI can
show provenance instead of just an opaque container-local path like
/data/cloned-repos/3/mkt-adminportal.

Nullable, no backfill: rows created before this migration were attached via
the old pick-folder/manual-path flow and never had a source URL to record.

Revision ID: 20260819_0012
Revises: 20260818_0011
Create Date: 2026-08-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0012"
down_revision: Union[str, None] = "20260818_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project_repos", sa.Column("clone_url", sa.String(1024), nullable=True))


def downgrade() -> None:
    op.drop_column("project_repos", "clone_url")
