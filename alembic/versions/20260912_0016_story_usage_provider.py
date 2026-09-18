"""story_usage.provider — the LLM provider a usage row belongs to.

forge/db/models/story_usage.py's uniqueness was previously (run_id, phase,
model) only. That collides once two providers can serve a same-named
model (e.g. an OpenAI-compatible self-hosted endpoint proxying a model
also reachable directly), and it can't distinguish which provider a row's
`model` string actually came from now that forge/llm/ supports more than
one. This migration adds the column and widens the uniqueness to include
it.

Backfill: every row written before this migration ran through the old
Claude Agent SDK path exclusively, so existing rows backfill to
provider='anthropic' (not the new default 'openai') — that reflects what
actually produced them, even though 'anthropic' is not itself a currently
wired forge/llm/ adapter. New rows get the column's own default
('openai') going forward; forge/services/usage.py:record_phase_usage
always writes it explicitly regardless of the column default.

Revision ID: 20260912_0016
Revises: 20260822_0015
Create Date: 2026-09-12
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260912_0016"
down_revision: Union[str, None] = "20260822_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "story_usage",
        sa.Column("provider", sa.String(32), nullable=False, server_default="openai"),
    )
    op.execute("UPDATE story_usage SET provider = 'anthropic'")
    op.drop_constraint("uq_story_usage_run_phase_model", "story_usage", type_="unique")
    op.create_unique_constraint(
        "uq_story_usage_run_phase_model_provider", "story_usage", ["run_id", "phase", "model", "provider"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_story_usage_run_phase_model_provider", "story_usage", type_="unique")
    op.create_unique_constraint("uq_story_usage_run_phase_model", "story_usage", ["run_id", "phase", "model"])
    op.drop_column("story_usage", "provider")
