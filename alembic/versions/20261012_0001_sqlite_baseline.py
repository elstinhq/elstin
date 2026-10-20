"""The whole schema, as of the move from Postgres to SQLite.

Revision ID: 0001
Revises:
Create Date: 2026-10-12

Replaces the 37 Postgres-era revisions (20260814_0001 .. 20261005_0037, still in
git history). Nothing upgrades *through* this one: it creates every table on an
empty database, so a database that existed before it has no path here — it was
Postgres, which elstin no longer runs on. There is no backfill and no data
conversion; every column's default is the model's own.

Timestamps are `DateTime(timezone=True)` here. SQLite stores them as text with
no zone, and elstin/db/types.py's `UtcDateTime` is what stores them as UTC and
reads them back aware — the migration only needs the column to exist.

New revisions are written with `op.batch_alter_table`, which rebuilds a table
for what SQLite cannot ALTER in place (alembic/env.py turns batch mode on).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('activity_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('phase', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('files', sa.JSON(), nullable=True),
    sa.Column('meta', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('activity_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_activity_events_run_id'), ['run_id'], unique=False)

    op.create_table('app_config',
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('secret', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('jira_watermarks',
    sa.Column('jira_key', sa.String(length=64), nullable=False),
    sa.Column('last_comment_id', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('jira_key')
    )
    op.create_table('projects',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('ticket_source', sa.String(length=16), server_default='jira', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('story_run_outcomes',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('planner_turns', sa.Integer(), nullable=False),
    sa.Column('explorer_count', sa.Integer(), nullable=False),
    sa.Column('dev_passes', sa.Integer(), nullable=False),
    sa.Column('dev_turns', sa.BigInteger(), nullable=False),
    sa.Column('dev_terminal_reason', sa.String(length=32), nullable=True),
    sa.Column('dev_soft_stops', sa.Integer(), nullable=False),
    sa.Column('gate_runs', sa.Integer(), nullable=False),
    sa.Column('gate_failures', sa.Integer(), nullable=False),
    sa.Column('verifier_runs', sa.Integer(), nullable=False),
    sa.Column('verifier_rejections', sa.Integer(), nullable=False),
    sa.Column('peak_memory_mb', sa.Integer(), nullable=True),
    sa.Column('peak_memory_command', sa.String(length=200), nullable=True),
    sa.Column('outcome', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('run_id')
    )
    op.create_table('story_usage',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('phase', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('input_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cache_creation_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cache_read_tokens', sa.BigInteger(), nullable=False),
    sa.Column('output_tokens', sa.BigInteger(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('cost_source', sa.String(length=16), nullable=False),
    sa.Column('duration_ms', sa.BigInteger(), nullable=False),
    sa.Column('api_duration_ms', sa.BigInteger(), nullable=True),
    sa.Column('invocation_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'phase', 'model', 'provider', name='uq_story_usage_run_phase_model_provider')
    )
    with op.batch_alter_table('story_usage', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_story_usage_run_id'), ['run_id'], unique=False)

    op.create_table('po_sessions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('epic_key', sa.String(length=64), nullable=True),
    sa.Column('jira_project_key', sa.String(length=64), nullable=True),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('brief', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('context', sa.JSON(), nullable=True),
    sa.Column('draft', sa.JSON(), nullable=True),
    sa.Column('draft_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('edited_by_person', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('person_edits', sa.JSON(), nullable=True),
    sa.Column('questions', sa.JSON(), nullable=True),
    sa.Column('answers', sa.JSON(), nullable=True),
    sa.Column('publish_result', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('po_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_po_sessions_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_po_sessions_status'), ['status'], unique=False)

    op.create_table('project_config',
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=128), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('secret', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('project_id', 'key')
    )
    op.create_table('project_repos',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('label', sa.String(length=256), nullable=True),
    sa.Column('clone_url', sa.String(length=1024), nullable=True),
    sa.Column('kind', sa.String(length=16), server_default='repo', nullable=False),
    sa.Column('read_only', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('generated_guide', sa.Text(), nullable=True),
    sa.Column('generated_guide_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('project_repos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_project_repos_project_id'), ['project_id'], unique=False)

    op.create_table('project_ticket_filters',
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('jira_project_key', sa.String(length=64), nullable=True),
    sa.Column('assignee', sa.String(length=256), nullable=True),
    sa.Column('statuses', sa.JSON(), nullable=True),
    sa.Column('labels', sa.JSON(), nullable=True),
    sa.Column('issue_types', sa.JSON(), nullable=True),
    sa.Column('extra_jql', sa.Text(), nullable=True),
    sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_matched_count', sa.Integer(), nullable=True),
    sa.Column('last_synced_count', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('project_id')
    )
    op.create_table('epics',
    sa.Column('jira_key', sa.String(length=64), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('summary', sa.String(length=512), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('project_repo_id', sa.Integer(), nullable=True),
    sa.Column('phase', sa.String(length=32), nullable=False),
    sa.Column('levels', sa.JSON(), nullable=True),
    sa.Column('integration_branch', sa.String(length=256), nullable=True),
    sa.Column('prior_work', sa.JSON(), nullable=True),
    sa.Column('plan_approved', sa.Boolean(), nullable=False),
    sa.Column('manual_gate', sa.Boolean(), nullable=False),
    sa.Column('pr_approval', sa.String(length=16), nullable=True),
    sa.Column('external_source', sa.String(length=16), nullable=True),
    sa.Column('external_id', sa.String(length=256), nullable=True),
    sa.Column('external_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('external_hash', sa.String(length=64), nullable=True),
    sa.Column('external_status', sa.String(length=128), nullable=True),
    sa.Column('external_closed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], ),
    sa.PrimaryKeyConstraint('jira_key')
    )
    with op.batch_alter_table('epics', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_epics_project_id'), ['project_id'], unique=False)

    op.create_table('po_messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=8), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('draft_version', sa.Integer(), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['po_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('po_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_po_messages_session_id'), ['session_id'], unique=False)

    op.create_table('project_lessons',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('project_repo_id', sa.Integer(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('scope_globs', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('occurrences', sa.Integer(), nullable=False),
    sa.Column('evidence_story_keys', sa.JSON(), nullable=True),
    sa.Column('evidence_quote', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('project_lessons', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_project_lessons_project_id'), ['project_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_project_lessons_status'), ['status'], unique=False)

    op.create_table('epic_pull_requests',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('epic_key', sa.String(length=64), nullable=False),
    sa.Column('repo_label', sa.String(length=64), nullable=False),
    sa.Column('repo_path', sa.String(length=1024), nullable=False),
    sa.Column('branch', sa.String(length=256), nullable=False),
    sa.Column('number', sa.Integer(), nullable=True),
    sa.Column('url', sa.String(length=512), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('ci_status', sa.String(length=16), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['epic_key'], ['epics.jira_key'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('epic_key', 'repo_label', name='uq_epic_pull_requests_epic_repo')
    )
    with op.batch_alter_table('epic_pull_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_epic_pull_requests_epic_key'), ['epic_key'], unique=False)

    op.create_table('stories',
    sa.Column('jira_key', sa.String(length=64), nullable=False),
    sa.Column('epic_key', sa.String(length=64), nullable=False),
    sa.Column('summary', sa.String(length=512), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('parent_key', sa.String(length=64), nullable=True),
    sa.Column('project_repo_id', sa.Integer(), nullable=True),
    sa.Column('wave', sa.Integer(), nullable=False),
    sa.Column('depends_on', sa.JSON(), nullable=True),
    sa.Column('prior_work', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('kind_reason', sa.String(length=64), nullable=True),
    sa.Column('issue_type', sa.String(length=64), nullable=True),
    sa.Column('labels', sa.JSON(), nullable=True),
    sa.Column('gate_approved', sa.Boolean(), nullable=False),
    sa.Column('clarification_questions', sa.JSON(), nullable=True),
    sa.Column('clarification_answers', sa.JSON(), nullable=True),
    sa.Column('thread_id', sa.String(length=64), nullable=True),
    sa.Column('worktree_path', sa.Text(), nullable=True),
    sa.Column('branch', sa.String(length=256), nullable=True),
    sa.Column('parked_reason', sa.String(length=64), nullable=True),
    sa.Column('retry_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pending_guidance', sa.JSON(), nullable=True),
    sa.Column('coverage_gap', sa.Boolean(), nullable=False),
    sa.Column('coverage_gap_detail', sa.Text(), nullable=True),
    sa.Column('read_only_changes', sa.JSON(), nullable=True),
    sa.Column('person_actions', sa.JSON(), nullable=True),
    sa.Column('pr_open_error', sa.Text(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('failure_stage', sa.String(length=16), nullable=True),
    sa.Column('merge_attempts', sa.Integer(), nullable=False),
    sa.Column('external_source', sa.String(length=16), nullable=True),
    sa.Column('external_id', sa.String(length=256), nullable=True),
    sa.Column('external_synced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('external_hash', sa.String(length=64), nullable=True),
    sa.Column('external_status', sa.String(length=128), nullable=True),
    sa.Column('external_closed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('external_pending', sa.JSON(), nullable=True),
    sa.Column('external_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['epic_key'], ['epics.jira_key'], ),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], ),
    sa.PrimaryKeyConstraint('jira_key')
    )
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stories_epic_key'), ['epic_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_stories_parent_key'), ['parent_key'], unique=False)

    op.create_table('pull_requests',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('story_key', sa.String(length=64), nullable=False),
    sa.Column('repo_label', sa.String(length=64), nullable=False),
    sa.Column('number', sa.Integer(), nullable=True),
    sa.Column('url', sa.String(length=512), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['story_key'], ['stories.jira_key'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('story_key', 'repo_label', name='uq_pull_requests_story_repo')
    )
    with op.batch_alter_table('pull_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pull_requests_story_key'), ['story_key'], unique=False)

    op.create_table('story_worktrees',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('story_key', sa.String(length=64), nullable=False),
    sa.Column('repo_label', sa.String(length=64), nullable=False),
    sa.Column('repo_path', sa.Text(), nullable=False),
    sa.Column('project_repo_id', sa.Integer(), nullable=True),
    sa.Column('worktree_path', sa.Text(), nullable=False),
    sa.Column('branch', sa.String(length=256), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_repo_id'], ['project_repos.id'], ),
    sa.ForeignKeyConstraint(['story_key'], ['stories.jira_key'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('story_key', 'repo_label', name='uq_story_worktrees_story_repo')
    )
    with op.batch_alter_table('story_worktrees', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_story_worktrees_story_key'), ['story_key'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('story_worktrees', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_story_worktrees_story_key'))

    op.drop_table('story_worktrees')
    with op.batch_alter_table('pull_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pull_requests_story_key'))

    op.drop_table('pull_requests')
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stories_parent_key'))
        batch_op.drop_index(batch_op.f('ix_stories_epic_key'))

    op.drop_table('stories')
    with op.batch_alter_table('epic_pull_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_epic_pull_requests_epic_key'))

    op.drop_table('epic_pull_requests')
    with op.batch_alter_table('project_lessons', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_project_lessons_status'))
        batch_op.drop_index(batch_op.f('ix_project_lessons_project_id'))

    op.drop_table('project_lessons')
    with op.batch_alter_table('po_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_po_messages_session_id'))

    op.drop_table('po_messages')
    with op.batch_alter_table('epics', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_epics_project_id'))

    op.drop_table('epics')
    op.drop_table('project_ticket_filters')
    with op.batch_alter_table('project_repos', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_project_repos_project_id'))

    op.drop_table('project_repos')
    op.drop_table('project_config')
    with op.batch_alter_table('po_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_po_sessions_status'))
        batch_op.drop_index(batch_op.f('ix_po_sessions_project_id'))

    op.drop_table('po_sessions')
    with op.batch_alter_table('story_usage', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_story_usage_run_id'))

    op.drop_table('story_usage')
    op.drop_table('story_run_outcomes')
    op.drop_table('projects')
    op.drop_table('jira_watermarks')
    op.drop_table('app_config')
    with op.batch_alter_table('activity_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_activity_events_run_id'))

    op.drop_table('activity_events')
