"""Persistence for the link between a board ticket and its source — what the
last sync read, what it found changed, and whether the source closed it.

Content rules live in forge/services/external_sync.py; this file only
stores. Epics and stories share the columns, so the methods take a `kind`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import Epic, Story

Kind = Literal["epic", "story"]

# A story in one of these has a plan or a run behind it that re-planning can
# safely discard. Anything else either has not started (nothing to redo) or
# has work in flight / a PR (a re-plan would orphan it).
REPLAN_ALLOWED_STATUSES = frozenset({"awaiting_plan_approval", "failed"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _model(kind: Kind):
    return Epic if kind == "epic" else Story


class _ExternalSyncMixin:
    def apply_external_content(
        self, kind: Kind, key: str, *, summary: str, description: str | None, content_hash: str
    ) -> None:
        """Take the source's content as the ticket's content: write it, record
        it as resolved, and drop any held change it supersedes."""
        with session_scope() as session:
            row = session.get(_model(kind), key)
            if row is None:
                return
            row.summary = summary
            row.description = description
            row.external_hash = content_hash
            row.external_synced_at = _now()
            if kind == "story":
                row.external_pending = None
                row.external_changed_at = None

    def mark_external_seen(
        self, kind: Kind, key: str, *, baseline_hash: str | None = None, external_status: str | None = None
    ) -> None:
        """The source was read and the local content already matches it.
        `baseline_hash` is stored only when none is recorded yet — the
        first time a pre-existing row is confirmed against its source."""
        with session_scope() as session:
            row = session.get(_model(kind), key)
            if row is None:
                return
            row.external_synced_at = _now()
            if baseline_hash and row.external_hash is None:
                row.external_hash = baseline_hash
            if external_status is not None:
                row.external_status = external_status

    def set_story_external_pending(self, key: str, pending: dict | None) -> None:
        with session_scope() as session:
            story = session.get(Story, key)
            if story is None:
                return
            story.external_synced_at = _now()
            if pending is None:
                story.external_pending = None
                story.external_changed_at = None
                return
            if story.external_pending != pending:
                story.external_changed_at = _now()
            story.external_pending = pending

    def set_external_closed(self, kind: Kind, key: str, *, closed: bool, status_text: str | None) -> bool:
        """Record the source's closed state; returns the previous value so the
        caller acts only on a transition, not on every sweep."""
        with session_scope() as session:
            row = session.get(_model(kind), key)
            if row is None:
                return closed
            previous = row.external_closed
            row.external_closed = closed
            row.external_status = status_text
            row.external_synced_at = _now()
            return previous

    def resolve_external_change(self, story_key: str, action: Literal["accept", "replan", "dismiss"]) -> dict | None:
        """A human's answer to a held change. Returns the pending payload that
        was resolved, or None if nothing was waiting.

        accept   apply the new text, keep the plan and any work as it is.
        replan   accept, and send the story back to `pending` for a fresh
                 plan — only while there is nothing in flight to orphan.
        dismiss  keep the current text and stop flagging this version.
        """
        with session_scope() as session:
            story = session.get(Story, story_key)
            if story is None or story.external_pending is None:
                return None
            pending = dict(story.external_pending)

            if action == "replan" and story.status not in REPLAN_ALLOWED_STATUSES:
                raise ValueError(
                    f"{story_key} is {story.status} — only a story waiting on plan approval, or failed, can be re-planned"
                )

            if action in ("accept", "replan"):
                if "summary" in pending:
                    story.summary = pending["summary"]
                if "description" in pending:
                    story.description = pending["description"]
            story.external_hash = pending.get("hash") or story.external_hash
            story.external_pending = None
            story.external_changed_at = None

            replanned_epic = None
            if action == "replan":
                if story.status == "failed":
                    story.retry_count += 1
                    replanned_epic = story.epic_key
                story.status = "pending"
                story.parked_reason = None
                story.failure_stage = None
                story.gate_approved = False

        if replanned_epic is not None:
            # Same as retry_story: a failed story halts its epic, so the epic
            # has to leave "failed" too or the daemon never polls it again.
            self.set_epic_phase(replanned_epic, "implementing")  # type: ignore[attr-defined]
        return pending

    def list_active_epic_keys_for_project(self, project_id: int) -> list[str]:
        from forge.state.manager_epic import _TERMINAL_EPIC_PHASES

        with session_scope() as session:
            rows = session.execute(
                select(Epic.jira_key).where(Epic.project_id == project_id, Epic.phase.notin_(_TERMINAL_EPIC_PHASES))
            ).scalars().all()
            return list(rows)
