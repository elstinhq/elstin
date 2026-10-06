"""PO sessions — a person and the PO agent drafting a backlog together.

A session is one piece of backlog planning: a brief, the mode elstin picked for
it (brainstorm / split / gap — elstin/services/po_sessions.py's `select_mode`),
the agent's draft, the questions it asked, the conversation around the draft,
and — once a person publishes it — which JIRA ticket each part became.

**The draft is a document a person may edit, not a transcript.** `draft` holds
the latest version of it, whoever wrote that: the agent's turn, or a person
saving the editor (`edited_by_person`, `person_edits` say the latter happened
and where, so the next revise turn is told not to undo it). `draft_version`
rises on every change and a save carries the version it was based on — an edit
made over a draft that has moved on is refused rather than silently lost.

**Work is the daemon's.** The API only creates a session or queues another turn
(`status="queued"`); elstin/daemon/po_runner.py picks it up, which is what keeps
a turn that takes minutes out of a request. A session left `running` by a
crashed daemon is queued again.

**`publish_result` is the publisher's checkpoint**: `{ref -> created JIRA key}`,
written after every ticket is created, so a publish that stops half-way (JIRA
refused the third create) continues where it was and never makes a duplicate.

Both tables are new: no backfill. Telemetry for a session is keyed on run id
`po-<id>` in the same activity, usage and run-outcome tables stories use, and is
removed with the session (state/manager_po.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from elstin.db.base import Base

PO_QUEUED = "queued"
PO_RUNNING = "running"
PO_AWAITING_INPUT = "awaiting_input"
PO_READY = "ready"
PO_PUBLISHING = "publishing"
PO_PUBLISHED = "published"
PO_FAILED = "failed"
PO_DISCARDED = "discarded"
PO_STATUSES = (
    PO_QUEUED, PO_RUNNING, PO_AWAITING_INPUT, PO_READY, PO_PUBLISHING, PO_PUBLISHED, PO_FAILED, PO_DISCARDED,
)

PO_MODE_BRAINSTORM = "brainstorm"
PO_MODE_SPLIT = "split"
PO_MODE_GAP = "gap"
PO_MODES = (PO_MODE_BRAINSTORM, PO_MODE_SPLIT, PO_MODE_GAP)

ROLE_PERSON = "person"
ROLE_AGENT = "agent"


def po_run_id(session_id: int) -> str:
    """The id a session's activity, usage and run outcome are filed under —
    never a story key, so the two never meet."""
    return f"po-{session_id}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PoSession(Base):
    __tablename__ = "po_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    # The epic being split or checked; NULL for a brainstorm, which makes a new one.
    epic_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Where the tickets are created. Resolved when the session starts (the
    # epic's own prefix, else the project's ticket filter, else what the
    # person typed) so a brainstorm has somewhere to put its epic.
    jira_project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PO_QUEUED, index=True)

    # What the draft is checked against that is not in it — the epic's summary,
    # its stories' keys and summaries, the ticket keys the agent may depend on —
    # as of the last turn. Kept so a person's save and a publish can run the
    # draft's structural checks (elstin/agents/roles/po_gate.py) without a JIRA
    # round trip.
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    edited_by_person: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Dotted paths a person changed ("stories.s2.description"), cumulative —
    # told to the agent on a revise turn so their edits are not undone.
    person_edits: Mapped[list | None] = mapped_column(JSON, nullable=True)
    questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    publish_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the session has cost across every turn (services/usage.py's rollup
    # for run id `po-<id>`), kept here so the session list can show it.
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class PoMessage(Base):
    """One line of the conversation around a draft. The person's opening brief
    is the first; the agent's reply closes each of its turns.

    `delivered_at` is for a person's message only: NULL until an agent turn
    has read it — at the start of a turn, or live between two of its steps
    (agents/loop.py's `pending_messages`). A message still NULL when a turn
    ends is what queues the next one."""

    __tablename__ = "po_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("po_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "person" | "agent"
    role: Mapped[str] = mapped_column(String(8), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # The draft version the agent's reply answers; NULL for a person's message.
    draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
