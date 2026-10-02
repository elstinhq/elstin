"""Token/cost/timing usage — the query-time counterpart to
forge/db/models/story_usage.py's accumulator table.

Lives in services/ and touches forge.db directly, following the precedent
forge/services/run_activity.py already set: run telemetry (activity log,
usage rollups) isn't "epics/stories/PRs/projects" state, so it sits outside
AGENTS.md's StateManager-only rule for forge.db.models. get_epic_usage still
goes through StateManager for story metadata (wave, kind, key) — that part
IS core state and stays on the normal path.

record_run_outcome() is the same idea for the *shape* of a run rather
than its cost — gate failures, verifier rejections, dev passes and turns
(see forge/db/models/run_outcome.py). It accumulates in place as the run
progresses, called from forge/orchestration/graph.py's nodes, so a run
that fails or is abandoned still leaves the counters it reached. Those are
the runs worth studying, so recording only on success would bias the data
in exactly the wrong direction.

record_phase_usage() is called once per phase *invocation* — once per
run_planner() call, once per run_dev() call, once per run_verifier()
call, once per run_epic_qa() call, and once per chat_completion_json() call
— never once per SDK message/step, since the SDK's own per-call usage
figures are already a running total for that call; summing per-step numbers
on top of that would double-count.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from forge.config.store import get_float_config
from forge.db.engine import session_scope
from forge.db.models import StoryRunOutcome, StoryUsage
from forge.llm.pricing import estimate_cost_usd
from forge.llm.types import Usage

logger = logging.getLogger("forge.usage")

PHASES = ("planner", "dev", "verifier", "qa")


def turn_usage_meta(usage: Usage) -> dict[str, Any]:
    """The `meta` of one agent-loop turn's "token" activity event.

    Carries the cache split, not just prompt/completion totals: story_usage
    only keeps a phase's sums, and a sum cannot show *where* a run stopped
    reading from the cache. That is how a dev run reading 4% of its input
    from the cache went unexplained — its per-turn events had no cache
    field to look at."""
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cost_usd": usage.cost_usd,
    }


def record_phase_usage(
    run_id: str | None,
    *,
    phase: str,
    model: str | None,
    provider: str = "openai",
    input_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = None,
    cost_source: str | None = None,
    duration_ms: int = 0,
    api_duration_ms: int | None = None,
) -> None:
    if not run_id:
        return

    model_key = model or "unknown"
    resolved_cost = cost_usd
    resolved_source = cost_source
    if resolved_cost is None:
        resolved_cost = estimate_cost_usd(
            provider,
            model,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        resolved_source = "estimated" if resolved_cost is not None else "unknown"
    elif resolved_source is None:
        resolved_source = "sdk"

    with session_scope() as session:
        row = session.execute(
            select(StoryUsage).where(
                StoryUsage.run_id == run_id,
                StoryUsage.phase == phase,
                StoryUsage.model == model_key,
                StoryUsage.provider == provider,
            )
        ).scalar_one_or_none()
        if row is None:
            # Explicit zeros rather than relying on the column defaults —
            # those only apply at flush/INSERT time, and the += below runs
            # against the in-memory attribute before that happens.
            row = StoryUsage(
                run_id=run_id, phase=phase, model=model_key, provider=provider,
                input_tokens=0, cache_creation_tokens=0, cache_read_tokens=0, output_tokens=0,
                duration_ms=0, invocation_count=0,
            )
            session.add(row)

        row.input_tokens += input_tokens
        row.cache_creation_tokens += cache_creation_tokens
        row.cache_read_tokens += cache_read_tokens
        row.output_tokens += output_tokens
        row.duration_ms += duration_ms
        if api_duration_ms is not None:
            row.api_duration_ms = (row.api_duration_ms or 0) + api_duration_ms
        row.invocation_count += 1
        if resolved_cost is not None:
            row.cost_usd = float(row.cost_usd or 0) + resolved_cost
            # "sdk" outranks "estimated" outranks "unknown" — keep the
            # strongest source seen for this row rather than letting a
            # later, weaker-sourced invocation downgrade it.
            if row.cost_source != "sdk":
                row.cost_source = resolved_source or row.cost_source

    logger.info(
        "usage run_id=%s phase=%s model=%s tokens=%d cost_source=%s",
        run_id, phase, model_key, input_tokens + output_tokens, resolved_source,
    )


# Every accumulating counter on StoryRunOutcome. Kept as one list so a
# new column is added in a single place rather than in the writer, the
# reader and the empty-shape helper separately.
_OUTCOME_COUNTERS = (
    "planner_turns",
    "explorer_count",
    "dev_passes",
    "dev_turns",
    "dev_soft_stops",
    "gate_runs",
    "gate_failures",
    "verifier_runs",
    "verifier_rejections",
)


def record_run_outcome(
    run_id: str | None,
    *,
    outcome: str | None = None,
    dev_terminal_reason: str | None = None,
    **counters: int,
) -> None:
    """Add to this run's work counters. Counters accumulate; `outcome` and
    `dev_terminal_reason` overwrite, since the latest value of each is the
    one that describes the run.

    Best-effort on purpose. This is telemetry for tuning, so a failure to
    record it must never fail a run that is otherwise fine — an unwritable
    counter is worth a log line, not a dead story."""
    if not run_id:
        return

    unknown = set(counters) - set(_OUTCOME_COUNTERS)
    if unknown:
        raise ValueError(f"unknown run-outcome counter(s): {sorted(unknown)}")

    try:
        with session_scope() as session:
            row = session.execute(
                select(StoryRunOutcome).where(StoryRunOutcome.run_id == run_id)
            ).scalar_one_or_none()
            if row is None:
                # Explicit zeros, not column defaults: those only apply at
                # INSERT time and the += below runs first, in memory.
                row = StoryRunOutcome(run_id=run_id, **{name: 0 for name in _OUTCOME_COUNTERS})
                session.add(row)
            for name, amount in counters.items():
                setattr(row, name, getattr(row, name) + amount)
            if outcome is not None:
                row.outcome = outcome
            if dev_terminal_reason is not None:
                row.dev_terminal_reason = dev_terminal_reason
    except Exception as exc:  # noqa: BLE001 — telemetry must not fail a run
        logger.warning("could not record run outcome for run_id=%s: %s", run_id, exc)
        return

    logger.info("run outcome run_id=%s %s", run_id, counters)


def record_run_peak_memory(run_id: str | None, *, peak_memory_mb: int, command: str) -> None:
    """Keep the highest memory any command of this run has reached. A maximum,
    not a counter: a smaller figure never replaces a larger one. Best-effort like
    the counters beside it — telemetry never fails a run."""
    if not run_id or peak_memory_mb <= 0:
        return
    try:
        with session_scope() as session:
            row = session.execute(select(StoryRunOutcome).where(StoryRunOutcome.run_id == run_id)).scalar_one_or_none()
            if row is None:
                row = StoryRunOutcome(run_id=run_id, **{name: 0 for name in _OUTCOME_COUNTERS})
                session.add(row)
            if row.peak_memory_mb is None or peak_memory_mb > row.peak_memory_mb:
                row.peak_memory_mb = peak_memory_mb
                row.peak_memory_command = command[:200]
    except Exception as exc:  # noqa: BLE001 — telemetry must not fail a run
        logger.warning("could not record peak memory for run_id=%s: %s", run_id, exc)


def get_run_outcome(run_id: str) -> dict[str, Any] | None:
    """This run's work counters, or None when nothing was ever recorded.

    None is deliberately distinct from a dict of zeros: it means "this run
    predates the counters, or never dispatched", which is not the same
    claim as "it ran and needed no retries". Callers must render the two
    differently or the first real measurement will look like a regression
    against a fictional baseline of zero."""
    with session_scope() as session:
        row = session.execute(
            select(StoryRunOutcome).where(StoryRunOutcome.run_id == run_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "run_id": row.run_id,
            **{name: getattr(row, name) for name in _OUTCOME_COUNTERS},
            "dev_terminal_reason": row.dev_terminal_reason,
            "outcome": row.outcome,
            # None: not measured (see StoryRunOutcome.peak_memory_mb).
            "peak_memory_mb": row.peak_memory_mb,
            "peak_memory_command": row.peak_memory_command,
        }


# --- the per-story spend ceiling ---------------------------------------
# Every cap before this one was per *loop*: the planner had one, dev had
# one, and each explorer carved a slice out of the planner's. Nothing
# bounded a single story end to end, so planning plus exploration plus
# however many dev fix rounds plus a review could each stay inside their
# own limit while the story as a whole cost an unbounded multiple of any
# of them. This is the outer bound, enforced at every point a story can
# reach a model.


def story_spend_usd(run_id: str) -> float:
    """What this story has cost so far, from the accumulated usage rows.

    IMPORTANT: a (provider, model) pair with no row in
    forge/llm/pricing.py contributes nothing, because there is no rate to
    multiply by — so a ceiling cannot constrain a model forge has no price
    for. That is why the pricing table has a test pinning every shipped
    model default against it."""
    with session_scope() as session:
        rows = session.execute(select(StoryUsage.cost_usd).where(StoryUsage.run_id == run_id)).scalars().all()
    return float(sum(cost for cost in rows if cost is not None))


def remaining_story_budget_usd(run_id: str | None) -> float | None:
    """What is left of this story's ceiling, or None when unbounded.

    Zero and negative are distinct from None on purpose: None means "no
    ceiling was configured", whereas 0.0 means "the ceiling exists and is
    used up", and only the second should stop a phase."""
    ceiling = get_float_config("story_max_budget_usd", 0.0)
    if ceiling <= 0 or not run_id:
        return None
    return ceiling - story_spend_usd(run_id)


def effective_budget_usd(run_id: str | None, phase_cap_usd: float) -> tuple[float, bool]:
    """The spend cap a phase should actually run with, and whether the
    story's ceiling is already exhausted.

    Returns (cap, exhausted). `cap` follows forge's convention that zero
    means unbounded, and is the tighter of the phase's own cap and what
    the story has left. `exhausted` is True only when a ceiling exists and
    nothing remains — the caller should then refuse to start rather than
    run one more turn, because a phase given a cap of zero would read that
    as unbounded and do the opposite of what was asked."""
    remaining = remaining_story_budget_usd(run_id)
    if remaining is None:
        return phase_cap_usd, False
    if remaining <= 0:
        logger.warning("story %s has exhausted its spend ceiling", run_id)
        return 0.0, True
    if phase_cap_usd <= 0:
        return remaining, False
    return min(phase_cap_usd, remaining), False


def _row_to_dict(row: StoryUsage) -> dict[str, Any]:
    return {
        "phase": row.phase,
        "model": row.model,
        "provider": row.provider,
        "input_tokens": row.input_tokens,
        "cache_creation_tokens": row.cache_creation_tokens,
        "cache_read_tokens": row.cache_read_tokens,
        "output_tokens": row.output_tokens,
        "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
        "cost_source": row.cost_source,
        "duration_ms": row.duration_ms,
        "api_duration_ms": row.api_duration_ms,
        "invocation_count": row.invocation_count,
    }


def _empty_totals() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "duration_ms": 0,
        "api_duration_ms": None,
        "invocation_count": 0,
        # Surfaced so the UI can caveat a total that isn't purely
        # SDK-reported, rather than presenting an estimate as fact.
        "has_estimated_cost": False,
        "has_unknown_cost": False,
    }


def _add_into_totals(totals: dict[str, Any], part: dict[str, Any]) -> None:
    totals["input_tokens"] += part["input_tokens"]
    totals["cache_creation_tokens"] += part["cache_creation_tokens"]
    totals["cache_read_tokens"] += part["cache_read_tokens"]
    totals["output_tokens"] += part["output_tokens"]
    totals["duration_ms"] += part["duration_ms"]
    if part.get("api_duration_ms") is not None:
        totals["api_duration_ms"] = (totals["api_duration_ms"] or 0) + part["api_duration_ms"]
    totals["invocation_count"] += part["invocation_count"]
    if part.get("cost_usd") is not None:
        totals["cost_usd"] = (totals["cost_usd"] or 0) + part["cost_usd"]
    if part.get("cost_source") == "estimated" or part.get("has_estimated_cost"):
        totals["has_estimated_cost"] = True
    if part.get("cost_source") == "unknown" or part.get("has_unknown_cost"):
        totals["has_unknown_cost"] = True


def _sum_totals(totals_list: list[dict[str, Any]]) -> dict[str, Any]:
    combined = _empty_totals()
    for totals in totals_list:
        _add_into_totals(combined, totals)
    return combined


def _group_by_phase(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_phase: dict[str, dict[str, Any]] = {}
    for row in rows:
        phase = row["phase"]
        bucket = by_phase.setdefault(phase, {"phase": phase, "models": [], "totals": _empty_totals()})
        bucket["models"].append(row)
        _add_into_totals(bucket["totals"], row)
    ordered_keys = [p for p in PHASES if p in by_phase] + [p for p in by_phase if p not in PHASES]
    return [by_phase[key] for key in ordered_keys]


def _usage_for_run(run_id: str) -> dict[str, Any]:
    with session_scope() as session:
        rows = session.execute(select(StoryUsage).where(StoryUsage.run_id == run_id)).scalars().all()
        row_dicts = [_row_to_dict(row) for row in rows]
    phases = _group_by_phase(row_dicts)
    totals = _sum_totals([phase["totals"] for phase in phases])
    return {"run_id": run_id, "phases": phases, "totals": totals}


def get_story_usage(story_key: str) -> dict[str, Any]:
    """Per-phase token/cache/cost/time breakdown for one story, plus its
    totals. Empty phases/zeroed totals for a story with no recorded usage
    (either it hasn't run yet, or it ran before this feature shipped) —
    callers should render that as "no usage data", not "$0 run"."""
    from forge.state.manager import StateManager

    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        return {"story_key": story_key, "run_id": None, "phases": [], "totals": _empty_totals()}

    run_id = story.get("thread_id") or story_key
    usage = _usage_for_run(run_id)
    # None when this run predates the counters — see get_run_outcome.
    return {"story_key": story_key, **usage, "run_outcome": get_run_outcome(run_id)}


def get_epic_usage(epic_key: str) -> dict[str, Any]:
    """Per-story usage for every top-level (non-sub-task) story in the
    epic, grouped by wave (Story.wave — the same "level" the UI already
    groups the story table by), with per-wave and epic-wide totals.
    Sub-tasks are excluded: they're never dispatched, so they have no
    run_id/usage of their own (same exclusion Epic.levels already applies)."""
    from forge.state.manager import StateManager

    sm = StateManager()
    stories = [s for s in sm.list_stories_for_epic(epic_key) if not s.get("parent_key")]
    stories.sort(key=lambda s: (s["wave"], s["jira_key"]))

    story_summaries: list[dict[str, Any]] = []
    by_wave: dict[int, list[dict[str, Any]]] = {}
    for story in stories:
        run_id = story.get("thread_id") or story["jira_key"]
        usage = _usage_for_run(run_id)
        summary = {
            "story_key": story["jira_key"],
            "wave": story["wave"],
            "kind": story["kind"],
            "status": story["status"],
            **usage,
        }
        story_summaries.append(summary)
        by_wave.setdefault(story["wave"], []).append(summary)

    levels = [
        {
            "wave": wave,
            "stories": by_wave[wave],
            "totals": _sum_totals([s["totals"] for s in by_wave[wave]]),
        }
        for wave in sorted(by_wave)
    ]
    epic_totals = _sum_totals([s["totals"] for s in story_summaries])

    return {"epic_key": epic_key, "levels": levels, "stories": story_summaries, "totals": epic_totals}
