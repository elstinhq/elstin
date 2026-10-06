"""A generated project guide per repo.

A repo with no AGENTS.md (or CLAUDE.md) of its own leaves the planner and dev
agents to rediscover its layout, commands and existing tooling every run, and
they sometimes get it wrong — one run planned a new codegen script for a repo
that already had one. elstin can now write such a guide itself, once, from the
repo's manifests, README and CI files (elstin/services/project_guide.py).

- `project_repos.generated_guide`: the guide's markdown. It is kept here, not
  committed to the repo: elstin does not edit checkouts it does not own, and a
  guide the repo ships itself always takes precedence over this one.
- `project_repos.generated_guide_at`: when it was written, for display.

Backfill: both NULL — no existing repo has a generated guide, and nothing is
generated for one until the project's `generate_project_guide` setting (off
by default) or the explicit generate endpoint asks for it.

Revision ID: 20260920_0022
Revises: 20260920_0021
Create Date: 2026-09-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0022"
down_revision: Union[str, None] = "20260920_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("project_repos", sa.Column("generated_guide", sa.Text(), nullable=True))
    op.add_column("project_repos", sa.Column("generated_guide_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("project_repos", "generated_guide_at")
    op.drop_column("project_repos", "generated_guide")
