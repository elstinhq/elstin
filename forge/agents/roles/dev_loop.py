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
    _norm_rel,
    _plan_path_set,
    build_dev_system,
)
from forge.config.store import get_bool_config, get_config, get_float_config, get_int_config, resolve_text
from forge.llm.errors import LlmError, ProviderLimit, provider_limit_of
from forge.llm.registry import LlmNotConfiguredError, get_provider
from forge.llm.types import ContentBlock, LlmResponse, history_from_json, history_to_json, text_message
from forge.services.lessons import format_lessons_block, plan_paths
from forge.services.run_activity import append_activity
from forge.services.usage import effective_budget_usd, record_phase_usage, record_run_outcome, turn_usage_meta
from forge.tools import language_hooks
from forge.tools.dev_tools import BOOKKEEPING_TOOLS, PROGRESS_TOOLS, READ_ONLY_TOOLS, WEB_TOOLS, make_dev_tools
from forge.tools.git_tools import list_changed_files
from forge.tools.progress import render_progress, seed_progress_items
from forge.tools.workspace import REFERENCE_SIGIL, Workspace, labels_in_plan
from forge.tools.worktree import create_worktree
from forge.tools.worktree_sync import sync_planned_files_from_project

logger = logging.getLogger("forge.dev_loop")

DevLoopOutcome = Literal["success", "soft_stop", "hard_failure"]

_SOFT_STOP_REASONS = frozenset({TERMINAL_MAX_TURNS, TERMINAL_MAX_BUDGET, TERMINAL_NO_PROGRESS})


class DevLoopError(RuntimeError):
    """A hard failure from the dev agent loop — missing/invalid provider
    config, a provider rejection, or a run that ended in error. Never
    raised for a turn/budget cap.

    `limit` is set when the cause was a provider limit (rate or quota), with the
    instant it lifts when the provider said so: the story is then parked until
    then rather than left failed (forge/daemon/main.py)."""

    def __init__(self, message: str = "", *, limit: ProviderLimit | None = None) -> None:
        super().__init__(message)
        self.limit = limit


def classify_loop_result(terminal_reason: str | None, is_error: bool) -> DevLoopOutcome:
    """Classify how one dev agent loop run ended. Pure and loop-independent
    so it's testable without a provider configured."""
    if terminal_reason in _SOFT_STOP_REASONS:
        return "soft_stop"
    if is_error:
        return "hard_failure"
    return "success"


# The idle-turn budget (turns in a row without writing a file before the run
# is stopped) is configured as a flat number, but how much reading a run needs
# before its first write grows with the plan: a fifteen-file change spanning an
# API and a UI cannot be understood in the four turns that suit a one-file
# fix, and a run that read 35 files in four turns and was then cut off before
# writing any of them failed three fix rounds in a row. So the configured
# value is the base for a small plan and grows by one turn per two further
# planned files, up to _IDLE_BUDGET_CAP_FACTOR times the base — enough room to
# explore a big change, still a bound on a model that is only looping.
_IDLE_TURNS_PER_EXTRA_FILES = 2
_IDLE_BUDGET_CAP_FACTOR = 4


# Activity-event details are capped by the caller's reader; this is enough for
# a reviewer's fix list, which the reviewer already keeps to one sentence each.
_FIX_MODE_DETAIL_CHARS = 4_000


def _fix_mode_detail(required_fixes: list[str], analyze_errors: list[str]) -> str:
    """The text of a fix round's activity event: the reviewer's required fixes,
    numbered as dev is given them, then the analyzer's errors."""
    parts: list[str] = []
    if required_fixes:
        parts.append("Required fixes:\n" + "\n".join(f"{i}. {fix}" for i, fix in enumerate(required_fixes, 1)))
    if analyze_errors:
        parts.append("Analyzer errors:\n" + "\n".join(f"- {line}" for line in analyze_errors[:25]))
    return "\n\n".join(parts)[:_FIX_MODE_DETAIL_CHARS]


def idle_budget_for_plan(base: int, planned_files: int) -> int:
    """The idle-turn budget for a plan of this many files. 0 (off) stays off."""
    if base <= 0:
        return 0
    extra = max(0, planned_files - 1) // _IDLE_TURNS_PER_EXTRA_FILES
    return min(base + extra, base * _IDLE_BUDGET_CAP_FACTOR)


def _build_prompt(*, continuing: bool, plan: dict[str, Any], context_bundle: dict[str, Any], brief: str) -> str:
    if continuing:
        return brief
    guide = context_bundle.get("project_guide") or {}
    project_guide_text = f"Project guide ({guide.get('path', 'AGENTS.md')}):\n{guide.get('content', '')}" if guide.get("found") else ""
    # Narrowed to the plan's own files: dev writes nothing else, so a lesson
    # scoped to other paths would only be noise in a prompt sent every turn.
    lessons_text = format_lessons_block(guide.get("lessons"), plan_paths(plan)).rstrip("\n")
    if lessons_text:
        project_guide_text = f"{project_guide_text}\n\n{lessons_text}" if project_guide_text else lessons_text
    project_context_text = f"Project context:\n{context_bundle.get('project_context', '')}\n\n" if context_bundle.get("project_context") else ""
    dev_skills_text = _format_skills_block(context_bundle.get("dev_skills"))
    prior_work = context_bundle.get("prior_work") or {}
    prior_work_text = (
        "Prior implementation attempts exist for this ticket — see the plan's discovery "
        "notes for what's already covered. Read the relevant existing files before writing "
        "new ones to avoid duplicating completed work.\n\n"
        if prior_work.get("found") else ""
    )
    dev_plan = {k: v for k, v in plan.items() if k not in ("plan_markdown", "read_only_changes")}
    # Changes left to a person — read-only repos, or files the project says
    # agents must not change (plan_gate.normalize_plan):
    # named so dev neither attempts them nor re-creates them where it can write.
    read_only_changes = plan.get("read_only_changes") or []
    read_only_text = (
        "Not yours to make — a person makes these changes; code against them as described, and don't "
        "make or copy them in a repo you can write:\n"
        + "\n".join(f"- {e.get('path')}: {e.get('purpose') or ''}".rstrip(": ") for e in read_only_changes if isinstance(e, dict))
        + "\n\n"
        if read_only_changes else ""
    )
    return (
        f"Approved plan:\n{json.dumps(dev_plan, indent=2)}\n\n"
        + read_only_text
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
    prior_declared: dict[str, str] | None = None,
    repo_roots: dict[str, str] | None = None,
    read_roots: dict[str, str] | None = None,
    story_base_refs: dict[str, str] | None = None,
    read_only_labels: list[str] | None = None,
    default_label: str = "",
    story_worktrees: dict[str, dict[str, str]] | None = None,
    gate_feedback: str | None = None,
    integration_paths: list[str] | None = None,
    sdk_model: str | None = None,
    sdk_effort: str | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """gate_feedback: the Tier 1 deterministic gate's own fix message
    (forge/orchestration/graph.py's gate_node, formatted by
    forge/agents/roles/verifier.py's format_gate_feedback). When set it IS
    this iteration's brief — a ready-made "tests failed with N errors in
    X, fix these" user message appended onto `prior_history`, so the model
    keeps every file it already read (and the prompt prefix stays cached)
    instead of re-deriving the task from a fresh FIX MODE brief.

    integration_paths: files outside the plan this pass may also write — set
    when the merge queue sends a finished story back to land it on the epic
    branch (a conflict to resolve, or the combined code failing its checks;
    forge/daemon/main.py's _send_back_to_dev). Plan scope stays the rule; this
    names the exceptions that landing needs.

    prior_declared: the planned files an earlier pass declared already satisfy the
    ticket (dev_tools' declare_already_satisfied_tool), carried so a fix round
    does not have to re-argue them.

    prior_progress: the checklist this story's earlier dev pass left behind
    (forge/tools/progress.py). Carried forward so a fix round resumes the
    same bookkeeping rather than re-deriving it from the plan and
    reporting every already-written file as pending.

    repo_roots / default_label / story_worktrees: the workspace
    (forge/tools/workspace.py). `repo_roots` is every repo on the project
    at its checkout; `story_worktrees` is the worktrees this story already
    has, keyed by label. Which repos this story WRITES comes from the
    approved plan, not from either of those — a prefixed path in
    files_to_create/files_to_modify names its repo, and a worktree is
    created here for any such repo that does not have one yet. That is why
    the set can be one repo or several and is discovered per task.

    read_roots / story_base_refs: where a repo this story does not write is
    read (the epic branch's worktree there, not the clone's stale checkout),
    and what a worktree created here for a newly named repo branches from
    (the epic branch in that repo, so it starts with the stories already
    merged there). Both from the daemon; see FeatureRunState."""
    continuing = bool(prior_history)
    # The project's own choice, else the global setting (resolve_text).
    resolved_model = sdk_model or resolve_text("dev_sdk_model", project_id)
    logger.info("Dev starting worktree=%s model=%s resume=%s", worktree_path, resolved_model, continuing)
    if run_id:
        append_activity(run_id, type="status", phase="dev", title="Development started", meta={"resumed": continuing})

    try:
        provider = get_provider(project_id)
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
                meta=turn_usage_meta(usage),
            )

    def _on_retry(attempt: int, error: LlmError, delay: float) -> None:
        if run_id:
            append_activity(
                run_id, type="status", phase="dev", title="Model call failed — retrying",
                detail=f"Attempt {attempt} failed: {error}. Trying again in {delay:.0f}s.", meta={"attempt": attempt},
            )

    try:
        synced = await asyncio.to_thread(sync_planned_files_from_project, project_path, worktree_path, plan)
        if synced and run_id:
            append_activity(run_id, type="tool", phase="dev", title=f"Synced {len(synced)} file(s) from project", files=synced, meta={"tool": "sync_planned_files"})

        allowed_paths = _plan_path_set(plan) | {_norm_rel(p) for p in integration_paths or []}

        # --- which repos does this plan write? ---------------------------
        roots = dict(repo_roots or {default_label: worktree_path})
        existing = {label: entry["worktree_path"] for label, entry in (story_worktrees or {}).items()}
        existing.setdefault(default_label, worktree_path)

        # The branch the daemon already created for this story in its
        # default repo. Every other repo the plan names gets the same one.
        story_branch = ((story_worktrees or {}).get(default_label) or {}).get("branch") or ""

        write_roots: dict[str, str] = {}
        created: dict[str, dict[str, str]] = {}
        for label in labels_in_plan(plan, default_label):
            if label in (read_only_labels or ()):
                # The plan gate refuses these; this is the backstop. No
                # worktree means no write root, so any write there fails.
                logger.warning("plan names read-only repo %r — not creating a worktree there", label)
                continue
            if label in existing:
                write_roots[label] = existing[label]
                continue
            repo_path = roots.get(label)
            if repo_path is None:
                # The plan named a repo this project does not have. Skip it
                # rather than fail: plan scope will refuse the writes, and
                # the gate will report the planned files as missing, which
                # is a far more legible failure than a worktree error.
                logger.warning("plan references unknown repo %r — skipping worktree creation", label)
                continue
            wt = await asyncio.to_thread(
                create_worktree,
                repo_path,
                story_key=run_id or "story",
                summary=plan.get("feature_summary") or (run_id or "story"),
                worktrees_base_dir=get_config("worktrees_base_dir") or "/data/repos",
                repo_label=label,
                # One story, one branch name — in every repo it writes. This
                # worktree is for an extra repo the plan named, created after
                # the default repo already has its branch, so recomputing the
                # name here (from the plan's summary rather than the story's,
                # and without the ticket's type) would give the same story a
                # different branch per repo.
                branch=story_branch or None,
                base_ref=(story_base_refs or {}).get(label),
            )
            write_roots[label] = wt["worktree_path"]
            created[label] = {"repo_path": repo_path, "worktree_path": wt["worktree_path"], "branch": wt["branch"]}
            logger.info("created worktree for repo %s at %s", label, wt["worktree_path"])
            if run_id:
                append_activity(
                    run_id, type="status", phase="dev",
                    title=f"Created a worktree in repo {label}",
                    detail=f"The plan writes {label}; branch {wt['branch']}",
                )
        write_roots.setdefault(default_label, worktree_path)

        # This story's own work from an earlier run — a Retry keeps its
        # worktrees (forge/daemon/main.py): writable, and its files editable,
        # even where the new plan takes them as done and does not list them,
        # so a fix the gate or the review asks for there is still possible.
        for label, path in existing.items():
            if label in (read_only_labels or ()):
                continue
            changed = await asyncio.to_thread(list_changed_files, path)
            if not changed:
                continue
            write_roots.setdefault(label, path)
            allowed_paths |= {
                _norm_rel(rel if label == default_label else f"{REFERENCE_SIGIL}{label}/{rel}") for rel in changed
            }

        # Readable everywhere: a repo with a worktree is read there (so dev
        # sees what it just wrote), any other repo at the epic branch's
        # worktree the daemon supplied, else at its own checkout.
        read_roots = {**roots, **(read_roots or {}), **existing, **write_roots}

        # Analyze/test targets are classified inside the repo that owns
        # them — a TypeScript UI and a Python API have different profiles,
        # and classifying both against one worktree would mislabel one.
        workspace = Workspace(default_label=default_label, roots=write_roots)
        analyze_targets: list[str] = []
        planned_test_files: list[str] = []
        by_label: dict[str, list[str]] = {}
        for display in allowed_paths:
            try:
                label, relative = workspace.split(display)
            except Exception:  # noqa: BLE001 — an unknown repo is not fatal here
                continue
            by_label.setdefault(label, []).append(relative)
        for label, relatives in by_label.items():
            root = write_roots.get(label)
            if not root:
                continue
            repo_analyze, repo_tests = language_hooks.classify_targets(root, set(relatives))
            analyze_targets.extend(workspace.display_path(label, t) for t in repo_analyze)
            planned_test_files.extend(workspace.display_path(label, t) for t in repo_tests)
        allow_test_run = bool(planned_test_files)

        if gate_feedback:
            brief = gate_feedback
            if run_id:
                append_activity(run_id, type="status", phase="dev", title="Fix mode: deterministic gate feedback carried into dev loop", detail=gate_feedback[:8000])
        else:
            required_fixes, analyze_errors = _collect_required_fixes(verifier_report)
            brief = _build_iteration_brief(worktree_path, allowed_paths, required_fixes, analyze_errors, continuing=continuing)
            if run_id and (required_fixes or analyze_errors):
                # The fixes themselves, not just how many: they are what the
                # reviewer found wrong, so they are what the activity feed
                # and forge/services/lessons.py need to say why a run was
                # sent back.
                append_activity(
                    run_id, type="status", phase="dev",
                    title=f"Fix mode: {len(required_fixes)} required fix(es), {len(analyze_errors)} analyzer error(s) carried into dev loop",
                    detail=_fix_mode_detail(required_fixes, analyze_errors) or None,
                )

        prompt = _build_prompt(continuing=continuing, plan=plan, context_bundle=context_bundle, brief=brief)

        data_dir = get_config("data_dir") or "/data"
        baseline_dir = f"{data_dir}/baselines/{run_id}" if run_id else None
        # Seeded from the plan on a first pass; carried forward verbatim on
        # a fix round. Mutated in place by update_progress_tool, read back
        # out below for the next round.
        progress_items = [dict(item) for item in prior_progress] if prior_progress else seed_progress_items(plan)

        declared_unchanged = dict(prior_declared or {})
        tool_fns, tool_schemas = make_dev_tools(
            worktree_path=worktree_path,
            allowed_paths=allowed_paths,
            analyze_targets=analyze_targets,
            allow_test_run=allow_test_run,
            planned_test_files=planned_test_files,
            baseline_dir=baseline_dir,
            progress_items=progress_items,
            declared_unchanged=declared_unchanged,
            default_label=default_label,
            write_roots=write_roots,
            read_roots=read_roots,
            web_access=get_bool_config("dev_web_access_enabled"),
            search_api_key=get_config("brave_search_api_key"),
        )

        initial_messages = (history_from_json(prior_history) if prior_history else []) + [text_message("user", prompt)]

        idle_budget = idle_budget_for_plan(get_int_config("dev_sdk_max_idle_turns", 4), len(progress_items))
        logger.info("Dev idle budget run_id=%s planned_files=%d turns=%d", run_id, len(progress_items), idle_budget)

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
            read_only_tools=READ_ONLY_TOOLS | WEB_TOOLS,
            progress_tools=PROGRESS_TOOLS,
            bookkeeping_tools=BOOKKEEPING_TOOLS,
            max_idle_turns=idle_budget,
            # Re-shown at the end of every request, never stored in
            # history — see forge/agents/loop.py's trailing_reminder.
            trailing_reminder=lambda: render_progress(progress_items),
            on_block=_on_block,
            on_turn=_on_turn,
            on_retry=_on_retry,
        )

        try:
            result = await run_agent_loop(config, initial_messages)
        except LlmError as exc:
            raise DevLoopError(f"Dev agent loop failed: {exc}", limit=provider_limit_of(exc)) from exc

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
                f"Dev agent loop ended in an unrecoverable error: {result.error_message or 'no error detail reported'} (terminal_reason={result.terminal_reason!r}).",
                limit=result.limit,
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

        file_changes, dev_verify_state = await asyncio.to_thread(
            _finalize_dev_changes, write_roots, plan, default_label
        )
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
        # Planned files dev declared already do what the ticket needs; the gate
        # excuses them and the reviewer is shown why.
        "declared_unchanged": declared_unchanged,
        # Every repo this story now has a worktree in, for the daemon to
        # record and to commit/push/PR per repo afterwards.
        "story_worktrees": {
            **(story_worktrees or {}),
            **created,
            default_label: (story_worktrees or {}).get(default_label)
            or {"repo_path": roots.get(default_label, ""), "worktree_path": worktree_path, "branch": ""},
        },
        # {profile_key: state} — e.g. Dart's build_runner signature. Fed
        # back into the verifier as VerifyContext.prior_state per profile.
        "dev_build_runner": dev_verify_state,
    }
