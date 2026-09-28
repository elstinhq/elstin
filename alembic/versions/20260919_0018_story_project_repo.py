"""stories.project_repo_id — the repo an individual story writes to.

Multi-repo support previously pinned the writable repo to the Epic, which
breaks the case it exists for: one epic routinely carries an API story and
a UI story that must land in different repos. The writable root therefore
belongs to the story, with the epic's remaining the default.

Backfill: none needed and none performed. NULL means "use the epic's
repo", which is exactly what every existing story was already doing, so
untouched rows keep their current behaviour with no data written. A value
appears only when a human (or a future sync rule) overrides one story.

Revision ID: 20260919_0018
Revises: 20260918_0017
Create Date: 2026-09-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0018"
down_revision: Union[str, None] = "20260918_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("project_repo_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_stories_project_repo_id", "stories", "project_repos", ["project_repo_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_stories_project_repo_id", "stories", type_="foreignkey")
    op.drop_column("stories", "project_repo_id")
