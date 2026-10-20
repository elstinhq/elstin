"""Durable per-run activity log — replaces elstin/services/run_activity.py's
former in-memory ring buffer. run_id is the story's own JIRA key in
production (it equals LangGraph's thread_id by construction, see
elstin/daemon/main.py's _run_or_resume_story) — but deliberately NOT a
foreign key to stories.jira_key: elstin/orchestration/graph.py's node
functions log activity keyed on whatever thread_id a caller passes, and
that's an opaque LangGraph identifier as far as the graph is concerned, not
guaranteed to reference a Story row (tests/test_graph.py exercises the graph
directly against synthetic uuid4() thread_ids with no backing story).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from elstin.db.base import Base
from elstin.db.types import UtcDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # "status" | "thinking" | "tool" | "llm" | "token" — whatever a caller
    # passes; deliberately a plain string, not a DB enum, since callers
    # across elstin/agents/roles/*.py already don't agree on a fixed set.
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # "planner" | "dev" | "verifier" | "qa" | "po"
    phase: Mapped[str] = mapped_column(String(16), nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    files: Mapped[list | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # No updated_at — rows are append-only, never mutated after insert.
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_utcnow, nullable=False)
