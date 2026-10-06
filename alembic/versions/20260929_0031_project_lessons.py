"""project_lessons — candidate lessons elstin distilled from its own runs.

elstin/services/lessons.py reads a finished story's evidence (gate failures,
verifier rejections, merge send-backs, a person's answers) and proposes a
few short lessons for the project guide. A person accepts or rejects each;
only accepted ones are ever handed to an agent. See
elstin/db/models/lesson.py.

Backfill: none, and none is meaningful — the table starts empty. Stories
that merged before it existed are never mined (their evidence is not
reconstructible in the form the extractor reads), so lessons appear only
from the next story that lands. `project_repo_id` is nullable (NULL =
whole project) and set NULL when its folder is detached.

Revision ID: 20260929_0031
Revises: 20260928_0030
Create Date: 2026-09-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0031"
down_revision: Union[str, None] = "20260928_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_lessons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("project_repo_id", sa.Integer(), sa.ForeignKey("project_repos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="gotcha"),
        sa.Column("scope_globs", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("evidence_story_keys", sa.JSON(), nullable=True),
        sa.Column("evidence_quote", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_project_lessons_project_id", "project_lessons", ["project_id"])
    op.create_index("ix_project_lessons_status", "project_lessons", ["status"])


def downgrade() -> None:
    op.drop_index("ix_project_lessons_status", table_name="project_lessons")
    op.drop_index("ix_project_lessons_project_id", table_name="project_lessons")
    op.drop_table("project_lessons")
