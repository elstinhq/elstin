"""Token/cost/timing usage — the query-time counterpart to
forge/db/models/story_usage.py's accumulator table.

Lives in services/ and touches forge.db directly, following the precedent
forge/services/run_activity.py already set: run telemetry (activity log,
usage rollups) isn't "epics/stories/PRs/projects" state, so it sits outside
AGENTS.md's StateManager-only rule for forge.db.models. get_epic_usage still
goes through StateManager for story metadata (wave, kind, key) — that part
IS core state and stays on the normal path.

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

from forge.db.engine import session_scope
from forge.db.models import StoryUsage
from forge.llm.pricing import estimate_cost_usd

logger = logging.getLogger("forge.usage")

PHASES = ("planner", "dev", "verifier", "qa")


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
    return {"story_key": story_key, **usage}


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
