"""The stories each story waits for, stored.

Until now the "blocks" / "is blocked by" links were read at sync, collapsed
into `Epic.levels`, and thrown away — so the reconciler could only schedule
level by level: a story in level 2 waited for EVERY level-1 story, not just
the ones it depends on, and nothing in an epic ran in parallel with an
unrelated failure. `stories.depends_on` keeps the edges themselves
(list[story_key]); forge/services/epic_sync.py writes it on every sync and
relevel, and the reconciler starts a story once each of them has merged.

Backfill: every top-level story in an epic's levels gets "every story in an
earlier level" — exactly the order the level-by-level scheduler enforced, so
nothing already queued starts earlier than it would have. The next sync of
the epic replaces that with the real links. Stories not in any level
(sub-tasks, never dispatched) get an empty list.

Revision ID: 20260926_0025
Revises: 20260922_0024
Create Date: 2026-09-26
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0025"
down_revision: Union[str, None] = "20260922_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("depends_on", sa.JSON(), nullable=True))

    bind = op.get_bind()
    epics = sa.table("epics", sa.column("jira_key", sa.String), sa.column("levels", sa.JSON))
    stories = sa.table(
        "stories", sa.column("jira_key", sa.String), sa.column("epic_key", sa.String), sa.column("depends_on", sa.JSON)
    )
    for epic_key, levels in bind.execute(sa.select(epics.c.jira_key, epics.c.levels)).all():
        if isinstance(levels, str):
            levels = json.loads(levels)
        earlier: list[str] = []
        for level in levels or []:
            for story_key in level:
                bind.execute(
                    stories.update().where(stories.c.jira_key == story_key).values(depends_on=list(earlier))
                )
            earlier.extend(level)
    bind.execute(stories.update().where(stories.c.depends_on.is_(None)).values(depends_on=[]))


def downgrade() -> None:
    op.drop_column("stories", "depends_on")
