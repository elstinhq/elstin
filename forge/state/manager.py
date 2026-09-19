"""StateManager — the only interface to forge's Postgres-backed epic/story/PR
bookkeeping (never touch forge.db.models directly outside forge/state/).

Uses a mixin-composition shape: the class body is empty, each concern lives
in its own `manager_*.py` file, all sharing `self` via MRO. That keeps
`patch("forge.state.StateManager.foo")` working regardless of which file
defines `foo`, and keeps each concern small and independently readable.

Deliberately narrow — per-run implementation progress (planning/developing/
verifying/qa) lives in the LangGraph
checkpoint for that story's thread_id, not here. This only owns what the
checkpointer doesn't: epic phase, wave assignment, PR metadata, JIRA
comment watermarks. JIRA itself remains source of truth for story
existence/assignee/summary — never written here for state decisions.
"""
from __future__ import annotations

from forge.state.manager_epic import _EpicMixin
from forge.state.manager_external import _ExternalSyncMixin
from forge.state.manager_pr import _PullRequestMixin
from forge.state.manager_project import _ProjectMixin
from forge.state.manager_story import _StoryMixin
from forge.state.manager_watermark import _WatermarkMixin


class StateManager(_EpicMixin, _ExternalSyncMixin, _ProjectMixin, _StoryMixin, _PullRequestMixin, _WatermarkMixin):
    pass
