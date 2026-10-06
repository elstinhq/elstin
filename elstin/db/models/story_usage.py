"""Accumulated per-run, per-phase token/cost/timing rollups — the summary
counterpart to elstin/db/models/activity.py's append-only event log.

ActivityEvent already carries every individual token-usage reading a caller
logs (type="token"), but answering "what did this story's dev phase cost in
total" from that log means scanning and summing every row for a run — fine
for a handful of events, wasteful once a dev loop has run for many turns
across many retries. StoryUsage instead accumulates one row per
(run_id, phase, model, provider): each phase invocation's provider-reported
(or config-estimated, via elstin/llm/pricing.py) usage/cost/duration is added
onto the existing row via an upsert, so the UI's per-story and per-level
cost view is a handful of indexed reads instead of a table scan.

run_id — like ActivityEvent.run_id — is deliberately NOT a foreign key to
stories.jira_key: it's the story's thread_id/JIRA key in production, but
elstin/orchestration/graph.py's tests exercise the graph against synthetic
thread_ids with no backing story row, and this table follows the same
convention as activity.py rather than special-casing that.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from elstin.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoryUsage(Base):
    __tablename__ = "story_usage"
    __table_args__ = (
        UniqueConstraint("run_id", "phase", "model", "provider", name="uq_story_usage_run_phase_model_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # "planner" | "dev" | "verifier" | "qa" | "po"
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # elstin/llm/base.py's LlmProvider.name — "openai" today. Part of the
    # uniqueness key alongside model: two providers can serve a
    # same-named model at different rates/behavior.
    provider: Mapped[str] = mapped_column(String(32), default="openai", nullable=False)

    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Nullable: a phase invocation whose cost couldn't be determined (neither
    # provider-reported nor estimable from elstin/llm/pricing.py's table)
    # leaves this column untouched rather than asserting a false $0.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    # "sdk" (the provider adapter reported an authoritative cost directly —
    # no wired adapter does today, but the field stays for one that does) |
    # "estimated" (computed from elstin/llm/pricing.py's static table) |
    # "unknown" (neither available; cost_usd stays null).
    cost_source: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)

    # Cumulative wall-clock time across every invocation of this phase for
    # this run, not just the LLM sub-call inside it — e.g. the verifier
    # phase's deterministic build/test gate is included, not only its LLM
    # review step.
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # Cumulative SDK-reported API-only time, when the SDK exposes it.
    # Narrower than duration_ms (excludes local tool/test execution) — kept
    # separate so the UI can show "time waiting on the model" distinctly
    # from "wall time for this phase".
    api_duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    invocation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
