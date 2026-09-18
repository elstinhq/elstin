"""LLM-driven dev loop, running through forge/agents/loop.py's
run_agent_loop against forge/llm/registry.py's configured provider. This
module adapts the loop's result into the same activity-log and
return-value contract forge/orchestration/graph.py's dev_node expects —
unchanged from before this migration, so the graph node itself needed no
changes.

Tool surface: forge/tools/dev_tools.py's make_dev_tools() functions and
TOOL_SCHEMAS are handed to the loop directly as `tool_fns`/`tools` — the
ONLY tools the model can call, since the loop only ever offers what's in
`tool_fns` (see forge/agents/loop.py's docstring: there is no separate
permission-callback layer the way the old Claude Agent SDK needed one,
because there are no provider-builtin tools to deny in the first place).

Soft stop vs hard failure: a turn/budget cap, or the loop's idle-turn
budget (no file-modifying tool call for N consecutive turns — see
forge/agents/loop.py's TERMINAL_NO_PROGRESS), ends the loop with a
terminal_reason in _SOFT_STOP_REASONS — real work may have happened, so
the caller proceeds to the deterministic gate/verification, and the next retry
resumes from this run's persisted `dev_history` (forge/orchestration/
state.py's FeatureRunState field, durably checkpointed by LangGraph's
AsyncPostgresSaver alongside every other piece of run state — see
forge/orchestration/graph.py's checkpointer_from_url) instead of
restarting cold. This is what replaced the old CLI's `resume=<session_id>`:
forge now owns the conversation history instead of the provider, and gets
it persisted for free through the graph's existing checkpoint mechanism
rather than a bespoke table. Anything else that ends in error raises
DevLoopError.

Model/effort/turn-cap/budget-cap are read from forge's config store
(get_config), not env-var settings — see forge/config/store.py's
dev_sdk_* fields, editable at /settings. ("_sdk_" in these key names
predates this migration and now refers to forge's own agent loop rather
than any specific provider SDK — renaming them would mean a data
migration for no behavioral gain, so they're kept as-is.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal

from forge.agents.loop import (
    TERMINAL_MAX_BUDGET,
    TERMINAL_MAX_TURNS,
    TERMINAL_NO_PROGRESS,
    LoopConfig,
    run_agent_loop,
    tool_defs_from_schemas,
)
from forge.agents.roles.dev import (
    _build_iteration_brief,
    _collect_required_fixes,
    _finalize_dev_changes,
    _format_skills_block,
    _plan_path_set,
    build_dev_system,
)
from forge.config.store import get_config, get_float_config, get_int_config
from forge.llm.errors import LlmError
from forge.llm.registry import LlmNotConfiguredError, get_provider
from forge.llm.types import ContentBlock, LlmResponse, history_from_json, history_to_json, text_message
from forge.services.run_activity import append_activity
from forge.services.usage import effective_budget_usd, record_phase_usage, record_run_outcome
from forge.tools import language_hooks
from forge.tools.dev_tools import BOOKKEEPING_TOOLS, PROGRESS_TOOLS, READ_ONLY_TOOLS, make_dev_tools
from forge.tools.progress import render_progress, seed_progress_items
from forge.tools.worktree_sync import sync_planned_files_from_project

logger = logging.getLogger("forge.dev_loop")

DevLoopOutcome = Literal["success", "soft_stop", "hard_failure"]

_SOFT_STOP_REASONS = frozenset({TERMINAL_MAX_TURNS, TERMINAL_MAX_BUDGET, TERMINAL_NO_PROGRESS})


class DevLoopError(RuntimeError):
    """A hard failure from the dev agent loop — missing/invalid provider
    config, a provider rejection, or a run that ended in error. Never
    raised for a turn/budget cap."""


def classify_loop_result(terminal_reason: str | None, is_error: bool) -> DevLoopOutcome:
    """Classify how one dev agent loop run ended. Pure and loop-independent
    so it's testable without a provider configured."""
    if terminal_reason in _SOFT_STOP_REASONS:
        return "soft_stop"
    if is_error:
        return "hard_failure"
    return "success"


def _build_prompt(*, continuing: bool, plan: dict[str, Any], context_bundle: dict[str, Any], brief: str) -> str:
    if continuing:
        return brief
    guide = context_bundle.get("project_guide") or {}
    project_guide_text = f"Project guide ({guide.get('path', 'AGENTS.md')}):\n{guide.get('content', '')}" if guide.get("found") else ""
    project_context_text = f"Project context:\n{context_bundle.get('project_context', '')}\n\n" if context_bundle.get("project_context") else ""
    dev_skills_text = _format_skills_block(context_bundle.get("dev_skills"))
    prior_work = context_bundle.get("prior_work") or {}
    prior_work_text = (
        "Prior implementation attempts exist for this ticket — see the plan's discovery "
        "notes for what's already covered. Read the relevant existing files before writing "
        "new ones to avoid duplicating completed work.\n\n"
        if prior_work.get("found") else ""
    )
    dev_plan = {k: v for k, v in plan.items() if k != "plan_markdown"}
    return (
        f"Approved plan:\n{json.dumps(dev_plan, indent=2)}\n\n"
        + (f"{project_guide_text}\n\n" if project_guide_text else "")
        + project_context_text
        + dev_skills_text
        + prior_work_text
        + brief
    )


async def run_dev(
    *,
    plan: dict[str, Any],
    context_bundle: dict[str, Any],
    worktree_path: str,
    project_path: str,
    verifier_report: dict[str, Any] | None = None,
    run_id: str | None = None,
    prior_history: list[dict[str, Any]] | None = None,
    prior_progress: list[dict[str, str]] | None = None,
    reference_roots: dict[str, str] | None = None,
    gate_feedback: str | None = None,
    sdk_model: str | None = None,
    sdk_effort: str | None = None,
) -> dict[str, Any]:
    """gate_feedback: the Tier 1 deterministic gate's own fix message
    (forge/orchestration/graph.py's gate_node, formatted by
    forge/agents/roles/verifier.py's format_gate_feedback). When set it IS
    this iteration's brief — a ready-made "tests failed with N errors in
    X, fix these" user message appended onto `prior_history`, so the model
    keeps every file it already read (and the prompt prefix stays cached)
    instead of re-deriving the task from a fresh FIX MODE brief.

    prior_progress: the checklist this story's earlier dev pass left behind
    (forge/tools/progress.py). Carried forward so a fix round resumes the
    same bookkeeping rather than re-deriving it from the plan and
    reporting every already-written file as pending."""
    continuing = bool(prior_history)
    resolved_model = sdk_model or get_config("dev_sdk_model")
    logger.info("Dev starting worktree=%s model=%s resume=%s", worktree_path, resolved_model, continuing)
    if run_id:
        append_activity(run_id, type="status", phase="dev", title="Development started", meta={"resumed": continuing})

    try:
        provider = get_provider()
    except LlmNotConfiguredError as exc:
        raise DevLoopError(str(exc)) from exc

    # The story-wide ceiling (forge/services/usage.py) bounds planning +
    # investigation + every dev fix round + review together, so dev's own
    # cap is only the inner of the two. Refuse to start rather than run one
    # more turn when nothing is left: a cap of zero means "unbounded"
    # everywhere in forge, so passing the exhausted remainder through would
    # do the exact opposite of what was asked.
    dev_budget, budget_exhausted = effective_budget_usd(run_id, get_float_config("dev_sdk_max_budget_usd", 0.0))
    if budget_exhausted:
        raise DevLoopError(
            "Story spend ceiling (story_max_budget_usd) is exhausted — refusing to start another dev pass. "
            "Raise the ceiling at /settings and retry the story to continue."
        )

    phase_started_at = time.monotonic()
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0}
    usage_cost_usd: float | None = None
    # Set once the loop returns; the finally block below records usage even
    # when this run ends up raising, so it must survive an early exit.
    api_duration_ms: int | None = None
    dev_summary_holder = {"text": "Development finished."}

    def _on_block(block_type: str, block: ContentBlock, step: int) -> None:
        if not run_id:
            return
        if block_type == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                dev_summary_holder["text"] = text
                append_activity(run_id, type="llm", phase="dev", title=f"Reasoning (step {step})", detail=text[:1000])
        elif block_type == "thinking":
            thinking = (getattr(block, "text", "") or "").strip()
            if thinking:
                append_activity(run_id, type="thinking", phase="dev", title=f"Thinking (step {step})", detail=thinking[:1000])
        elif block_type == "tool_use":
            name = getattr(block, "name", "unknown")
            tool_input = getattr(block, "arguments", {}) or {}
            logger.info("Dev tool call: %s args=%s", name, str(tool_input)[:200])
            append_activity(run_id, type="tool", phase="dev", title=f"Called tool: {name}", detail=f"Arguments: {json.dumps(tool_input, default=str)[:500]}", meta={"tool": name})
        elif block_type == "tool_result":
            is_error = bool(getattr(block, "is_error", False))
            append_activity(run_id, type="tool", phase="dev", title="Tool result" + (" (error)" if is_error else ""), detail=str(getattr(block, "content", ""))[:1000], meta={"is_error": is_error})

    def _on_turn(turn: int, response: LlmResponse) -> None:
        nonlocal usage_cost_usd
        usage = response.usage
        for key, value in (
            ("input_tokens", usage.input_tokens), ("output_tokens", usage.output_tokens),
            ("cache_creation_tokens", usage.cache_creation_tokens), ("cache_read_tokens", usage.cache_read_tokens),
        ):
            usage_totals[key] += value
        if usage.cost_usd is not None:
            usage_cost_usd = (usage_cost_usd or 0.0) + usage.cost_usd
        total = usage.input_tokens + usage.output_tokens
        if run_id and total > 0:
            append_activity(
                run_id, type="token", phase="dev", title=f"Token usage (dev step {turn})", detail=f"{total} tokens",
                meta={"prompt_tokens": usage.input_tokens, "completion_tokens": usage.output_tokens, "total_tokens": total, "cost_usd": usage.cost_usd},
            )

    try:
        synced = await asyncio.to_thread(sync_planned_files_from_project, project_path, worktree_path, plan)
        if synced and run_id:
            append_activity(run_id, type="tool", phase="dev", title=f"Synced {len(synced)} file(s) from project", files=synced, meta={"tool": "sync_planned_files"})

        allowed_paths = _plan_path_set(plan)
        analyze_targets, planned_test_files = language_hooks.classify_targets(worktree_path, allowed_paths)
        allow_test_run = bool(planned_test_files)

        if gate_feedback:
            brief = gate_feedback
            if run_id:
                append_activity(run_id, type="status", phase="dev", title="Fix mode: deterministic gate feedback carried into dev loop", detail=gate_feedback[:1000])
        else:
            required_fixes, analyze_errors = _collect_required_fixes(verifier_report)
            brief = _build_iteration_brief(worktree_path, allowed_paths, required_fixes, analyze_errors, continuing=continuing)
            if run_id and (required_fixes or analyze_errors):
                append_activity(run_id, type="status", phase="dev", title=f"Fix mode: {len(required_fixes)} required fix(es), {len(analyze_errors)} analyzer error(s) carried into dev loop")

        prompt = _build_prompt(continuing=continuing, plan=plan, context_bundle=context_bundle, brief=brief)

        data_dir = get_config("data_dir") or "/data"
        baseline_dir = f"{data_dir}/baselines/{run_id}" if run_id else None
        # Seeded from the plan on a first pass; carried forward verbatim on
        # a fix round. Mutated in place by update_progress_tool, read back
        # out below for the next round.
        progress_items = [dict(item) for item in prior_progress] if prior_progress else seed_progress_items(plan)

        tool_fns, tool_schemas = make_dev_tools(
            worktree_path=worktree_path,
            allowed_paths=allowed_paths,
            analyze_targets=analyze_targets,
            allow_test_run=allow_test_run,
            planned_test_files=planned_test_files,
            baseline_dir=baseline_dir,
            progress_items=progress_items,
            # Reads may span the project's other repos; writes stay in this
            # worktree (forge/tools/workspace.py).
            reference_roots=reference_roots,
        )

        initial_messages = (history_from_json(prior_history) if prior_history else []) + [text_message("user", prompt)]

        config = LoopConfig(
            provider=provider,
            model=resolved_model,
            system=build_dev_system(worktree_path),
            tools=tool_defs_from_schemas(tool_schemas),
            tool_fns=tool_fns,
            effort=sdk_effort or get_config("dev_sdk_effort"),
            max_turns=get_int_config("dev_sdk_max_turns", 80),
            max_budget_usd=dev_budget,
            compaction_token_threshold=get_int_config("agent_compaction_token_threshold", 120_000),
            read_only_tools=READ_ONLY_TOOLS,
            progress_tools=PROGRESS_TOOLS,
            bookkeeping_tools=BOOKKEEPING_TOOLS,
            max_idle_turns=get_int_config("dev_sdk_max_idle_turns", 4),
            # Re-shown at the end of every request, never stored in
            # history — see forge/agents/loop.py's trailing_reminder.
            trailing_reminder=lambda: render_progress(progress_items),
            on_block=_on_block,
            on_turn=_on_turn,
        )

        try:
            result = await run_agent_loop(config, initial_messages)
        except LlmError as exc:
            raise DevLoopError(f"Dev agent loop failed: {exc}") from exc

        api_duration_ms = result.api_duration_ms
        outcome = classify_loop_result(result.terminal_reason, result.is_error)
        # Recorded before the hard-failure raise below: a run that died is
        # precisely the run whose shape is worth having (see
        # forge/db/models/run_outcome.py).
        record_run_outcome(
            run_id,
            dev_passes=1,
            dev_turns=result.turns_used,
            dev_soft_stops=1 if outcome == "soft_stop" else 0,
            dev_terminal_reason=result.terminal_reason,
        )
        logger.info(
            "Dev result run_id=%s terminal_reason=%s outcome=%s turns=%d",
            run_id, result.terminal_reason, outcome, result.turns_used,
        )
        if result.is_error and run_id:
            append_activity(run_id, type="status", phase="dev", title="Dev agent loop reported an error", detail=(result.error_message or "")[:1000], meta={"terminal_reason": result.terminal_reason})

        if outcome == "hard_failure":
            raise DevLoopError(
                f"Dev agent loop ended in an unrecoverable error: {result.error_message or 'no error detail reported'} (terminal_reason={result.terminal_reason!r})."
            )

        truncated = outcome == "soft_stop"
        if truncated:
            if not dev_summary_holder["text"] or dev_summary_holder["text"] == "Development finished.":
                dev_summary_holder["text"] = "Development paused at the turn limit — will resume on retry."
            logger.warning("Dev soft stop (%s) run_id=%s turns=%d", result.terminal_reason, run_id, result.turns_used)
            if run_id:
                title = (
                    "No file changes for several turns — stopping early; the deterministic gate decides what happens next"
                    if result.terminal_reason == TERMINAL_NO_PROGRESS
                    else "Turn limit reached — resuming this session on the next retry"
                )
                append_activity(run_id, type="status", phase="dev", title=title, meta={"truncated": True, "soft_stop": True, "terminal_reason": result.terminal_reason})

        logger.info("Dev loop finished run_id=%s truncated=%s turns=%d", run_id, truncated, result.turns_used)

        file_changes, dev_verify_state = await asyncio.to_thread(_finalize_dev_changes, worktree_path, plan)
    finally:
        if run_id:
            record_phase_usage(
                run_id, phase="dev", model=resolved_model, provider=provider.name,
                duration_ms=int((time.monotonic() - phase_started_at) * 1000),
                api_duration_ms=api_duration_ms,
                cost_usd=usage_cost_usd, **usage_totals,
            )

    return {
        "file_changes": file_changes,
        "summary": dev_summary_holder["text"],
        "truncated": truncated,
        "synced_from_project": synced,
        "messages": [{"role": "dev", "content": dev_summary_holder["text"]}],
        "dev_history": history_to_json(result.history),
        "dev_progress": progress_items,
        # {profile_key: state} — e.g. Dart's build_runner signature. Fed
        # back into the verifier as VerifyContext.prior_state per profile.
        "dev_build_runner": dev_verify_state,
    }
