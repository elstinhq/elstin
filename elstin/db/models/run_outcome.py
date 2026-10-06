"""The *shape* of a run, as opposed to its cost.

elstin/db/models/story_usage.py already answers "what did this story spend".
It cannot answer "how much work did it take to get there", and that is the
question every tuning decision in this codebase turns on. How often did the
Tier 1 gate catch something and send dev back? How often did the LLM
reviewer reject a build that had already passed the gate? Did the dev loop
finish on its own or hit a cap? Those numbers decide whether the gate is
earning its keep, whether the turn budget is set sanely, and which model
belongs in which role — and until this table existed they lived only inside
a LangGraph checkpoint blob keyed by thread_id, and as prose in the activity
feed. Neither is queryable, so nobody could answer any of it from data.

One row per run_id, accumulated in place by
elstin/services/usage.py:record_run_outcome as the run progresses, rather
than written once at the end. That matters because the interesting runs are
the ones that failed or were abandoned mid-way: a row that only appears on
success would systematically exclude exactly the cases worth studying.

The two columns to look at first are `gate_failures` and
`verifier_rejections`. Tier 1 exists to catch a broken build before the
expensive reviewer sees it (elstin/orchestration/graph.py's gate_node), so
that mechanism is working when the first number is high and the second is
low. If the reviewer keeps rejecting builds the gate passed, the gate is
checking the wrong things; if neither ever fires, the whole retry apparatus
is dead weight for this repo and model.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from elstin.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StoryRunOutcome(Base):
    __tablename__ = "story_run_outcomes"

    # thread_id/story key — the same identifier story_usage.run_id uses, so
    # the two tables join directly.
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # --- planner ---
    planner_turns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Read-only recon sub-agents dispatched for this run's plan
    # (elstin/agents/roles/explorer.py). Zero means the scoping step decided
    # none were worth it, or the feature is off.
    explorer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- dev ---
    # dev_passes counts entries into the dev node (one per fix round);
    # dev_turns is the total model turns across all of them, which is the
    # figure to compare against dev_sdk_max_turns.
    dev_passes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dev_turns: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # elstin/agents/loop.py's terminal_reason from the LAST dev pass:
    # end_turn (finished by itself), max_turns / max_budget_usd /
    # no_progress (a cap stopped it), or a provider stop reason.
    dev_terminal_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # How many dev passes ended on a cap rather than by finishing. A run
    # that always soft-stops is a run whose budget is set too low, and the
    # code it produces was never the model's own idea of finished.
    dev_soft_stops: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Tier 1 deterministic gate ---
    gate_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gate_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- LLM review ---
    verifier_runs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Reviews that sent the story back to dev, i.e. a build that cleared
    # Tier 1 and the reviewer still judged incomplete.
    verifier_rejections: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- machine load ---
    # The most memory one command of this run's reached — the child and every
    # process it spawned, resident memory summed — and the command that reached
    # it (elstin/tools/process_monitor.py, elstin/services/memory_usage.py). What
    # `max_parallel_stories` should be set from: three runs peaking at 3 GB do not
    # fit on a 8 GB machine. NULL is "not measured", never "zero" — a run from before
    # this existed, or one that ran no command.
    peak_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_memory_command: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # The last RunStatus this run reported (elstin/orchestration/state.py).
    # Nullable because a run abandoned mid-flight never reports one, and
    # that absence is itself a signal worth keeping.
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
