"""Durable per-run activity log for the daemon's agent roles.

Persists to Postgres (forge/db/models/activity.py's ActivityEvent) instead
of the process-local in-memory ring this module used to keep — activity now
survives an app restart and is queryable after a run finishes, which is what
the UI's story detail dialog reads via GET /api/stories/{key}/activity and
streams live over GET /api/stories/{key}/stream (forge/api/run_routes.py).

Every append also hands the event to forge/services/activity_stream.py,
which pushes it straight to whoever is watching that run right now. The
write to Postgres stays the durable record and the source of truth for a
fresh page load; the fan-out is only a shortcut that removes the poll
interval between an agent doing something and a human seeing it.

append_activity()/list_activity() keep their original signatures and return
shape on purpose: every call site across forge/agents/roles/*.py,
forge/orchestration/graph.py, and forge/tools/dart_tools.py, plus the API
route and the UI's rendering code, needed zero changes for this swap.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import ActivityEvent
from forge.services.activity_stream import publish

ActivityType = Literal["status", "thinking", "tool", "llm", "token"]
ActivityPhase = Literal["planner", "dev", "verifier", "qa"]

logger = logging.getLogger("forge.activity")


def _row_to_dict(row: ActivityEvent) -> dict[str, Any]:
    return {
        # Monotonic per-insert, so it doubles as the stream cursor: the SSE
        # route sends it as the event id and a reconnecting EventSource
        # replays from there via Last-Event-ID (forge/api/run_routes.py).
        "id": row.id,
        "run_id": row.run_id,
        "type": row.type,
        "phase": row.phase,
        "title": row.title,
        "detail": row.detail,
        "files": row.files or [],
        "meta": row.meta or {},
        "ts": row.created_at.isoformat(),
    }


def append_activity(
    run_id: str,
    *,
    type: ActivityType,
    phase: ActivityPhase,
    title: str,
    detail: str | None = None,
    files: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not run_id:
        return {}

    with session_scope() as session:
        row = ActivityEvent(
            run_id=run_id, type=type, phase=phase, title=title,
            detail=detail, files=files or [], meta=meta or {},
        )
        session.add(row)
        session.flush()
        event = _row_to_dict(row)

    logger.info("activity run_id=%s phase=%s title=%s", run_id, phase, title)
    # After the session closes, so a live viewer never sees an event that a
    # rollback then un-writes. publish() swallows its own failures — an
    # agent turn must not break because nobody could be notified.
    publish(event)
    return event


def list_activity(run_id: str, *, limit: int | None = None, since_id: int = 0) -> list[dict[str, Any]]:
    """Chronological (oldest first) — matches the shape the ring buffer used
    to return. `limit` (when given) keeps the most recent N events, so a
    long-running story's feed stays a bounded, fast fetch. `since_id`
    returns only what was written after that event id — the incremental
    read behind stream reconnects and slow-subscriber catch-up
    (forge/services/activity_stream.py)."""
    with session_scope() as session:
        query = select(ActivityEvent).where(ActivityEvent.run_id == run_id)
        if since_id:
            query = query.where(ActivityEvent.id > since_id)
        if limit is not None:
            rows = list(session.execute(query.order_by(ActivityEvent.id.desc()).limit(limit)).scalars().all())
            rows.reverse()
        else:
            rows = list(session.execute(query.order_by(ActivityEvent.id)).scalars().all())
        return [_row_to_dict(row) for row in rows]
