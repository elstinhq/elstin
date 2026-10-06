"""A human's answer to the planner's clarification questions.

The planner can pause a run with a question when a design decision is
genuinely ambiguous (elstin/agents/roles/planner.py's CLARIFICATION
QUESTIONS rules). Until now nothing persisted what it asked: the daemon
collapses that pause into the same "awaiting_plan_approval" status a
normal plan review uses (elstin/daemon/main.py's
_GRAPH_STATUS_TO_STORY_STATUS), and the only thing a human could do at
that status was hit Approve, which resumed the graph with no answer at
all — the planner just re-ran with the same empty `clarification_answers`
it started with.

- `stories.clarification_questions`: the pending question list, straight
  from the planner's `questions` field (`[{id, question, context,
  options: [{id, label, description}]}]`). NULL means nothing is pending
  — set on every run completion (elstin/daemon/main.py), not only when a
  question exists, so a story that plans cleanly never shows a stale form.
- `stories.clarification_answers`: a human's resolved answers (`[{question,
  option_label, option_description}]`, the shape elstin/agents/roles/
  planner_loop.py's `_build_prompt_text` already expects), written by
  POST /api/stories/{key}/answer and fed into the next resume's graph
  state patch alongside worktree_path/story_kind/etc.

Backfill: both NULL for existing rows — nothing has ever recorded a
pending question before this column existed, so there is nothing to
backfill, and a story that reaches awaiting_plan_approval afterward gets
these populated on its next run as usual.

Revision ID: 20260922_0024
Revises: 20260920_0023
Create Date: 2026-09-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0024"
down_revision: Union[str, None] = "20260920_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stories", sa.Column("clarification_questions", sa.JSON(), nullable=True))
    op.add_column("stories", sa.Column("clarification_answers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("stories", "clarification_answers")
    op.drop_column("stories", "clarification_questions")
