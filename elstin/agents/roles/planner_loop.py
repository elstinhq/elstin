"""LLM-driven planner, running through elstin/agents/loop.py's
run_agent_loop against elstin/llm/registry.py's configured provider. The
agent explores the target project live via elstin/tools/planner_tools.py's
own Read/Grep/Glob-equivalent tools and produces the plan in the same
turn, schema-validated by the provider's native structured output
(elstin/llm/types.py:ResponseFormat).

Before that loop starts, `gather_exploration` (elstin/agents/roles/
explorer.py) may fan out a handful of read-only recon sub-agents in
parallel and fold their findings into the prompt. That is a pure context
optimization: the planner could read those files itself, but each one
would then sit in its history and be re-sent on every later turn, whereas
a sub-agent's reads stay in its own throwaway context and only a short
report comes back. It is opt-out (`planner_explorers_enabled`) and
skipped entirely on a clarification round-trip, where the codebase has
already been explored once and the only new input is the human's answer.

Tool surface: elstin/tools/planner_tools.py's make_planner_tools() —
elstin's own read-only tools, since no provider ships built-in ones the way
the old Claude Agent SDK did. Planning is read-only by nature (no
path_guard-enforced *write* scope needed the way elstin/agents/roles/
dev_loop.py's dev loop needs one), and elstin/agents/loop.py's tool
dispatch is itself the permission boundary — see that module's docstring
— so there is no separate can_use_tool layer to maintain here anymore.

Prior work — branches that already carry this ticket's work — is looked up
by the daemon before the story's branch exists (elstin/services/prior_work.py,
elstin/daemon/prior_work_lookup.py) and arrives here as `prior_work`; the
worktree already starts from the branch it selected. With nothing found (or
no caller supplying it) it is the empty shape, which the prompt omits.

**Not yet built**: the RAG context pre-seeding (`discover_context`). It
pulls in a genuinely separate subsystem — Qdrant-backed embeddings, a
multi-provider LLM "analyst" for RAG summarization, and a SQLite
ticket/run-index store — worth building as its own piece of work. Its
absence doesn't break planning: the agent still explores the codebase live
via its own tools every run, just without a pre-computed head start (search
terms, grep hits, manifest summaries). `context_bundle` below is the same
empty shape `discover_context` would return when it finds nothing.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from elstin.agents.loop import LoopConfig, run_agent_loop, tool_defs_from_schemas
from elstin.agents.roles import explorer as explorer_defaults
from elstin.agents.roles.explorer import gather_exploration
from elstin.agents.roles.plan_gate import (
    blocking, check_plan, check_plan_integrity, check_questions, describe_plan_shape, format_plan_feedback, normalize_plan,
    plan_shape,
)
from elstin.agents.roles.planner import _format_prior_work_block, build_planner_system
from elstin.config.store import get_bool_config, get_config, get_float_config, get_int_config, resolve_text
from elstin.llm.errors import LlmError, ProviderLimit, provider_limit_of
from elstin.llm.registry import LlmNotConfiguredError, get_provider
from elstin.llm.types import ImageBlock, LlmResponse, Message, ResponseFormat, TextBlock
from elstin.services.guidance import take_guidance
from elstin.services.lessons import format_lessons_block
from elstin.services.run_activity import append_activity
from elstin.services.usage import effective_budget_usd, record_phase_usage, record_run_outcome, turn_usage_meta
from elstin.tools.planner_tools import READ_ONLY_TOOLS, make_planner_tools
from elstin.tools.workspace import Workspace

logger = logging.getLogger("elstin.planner_loop")

_EMPTY_CONTEXT_BUNDLE: dict[str, Any] = {
    "project_guide": None,
    "search_terms": [],
    "grep_matches": [],
    "symbols": [],
    "lsp_hits": [],
    "files": [],
    "file_summaries": [],
    "manifest_summaries": [],
}
_EMPTY_PRIOR_WORK: dict[str, Any] = {"found": False, "candidates": []}

# A nullable-string leaf, for a field that's semantically optional but
# must still be listed in `required` — OpenAI's strict json_schema mode
# requires every property at every level to be in `required`; the way to
# express "the model may skip this" under that constraint is a
# `["string", "null"]` type, not omission from `required`. See
# _PLAN_JSON_SCHEMA below.
_NULLABLE_STRING = {"type": ["string", "null"]}

_FILE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "purpose": _NULLABLE_STRING},
    "required": ["path", "purpose"],
    "additionalProperties": False,
}

# A thing the ticket needs that no agent can do (see plan_gate.person_actions_of),
# as opposed to a file elstin may not change (read_only_changes).
_PERSON_ACTION_SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string"}, "reason": _NULLABLE_STRING},
    "required": ["action", "reason"],
    "additionalProperties": False,
}

_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "feature_summary": {"type": "string"},
        "reasoning": _NULLABLE_STRING,
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "context": _NULLABLE_STRING,
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}, "label": {"type": "string"}, "description": _NULLABLE_STRING},
                            "required": ["id", "label", "description"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "question", "context", "options"],
                "additionalProperties": False,
            },
        },
        "files_to_create": {"type": "array", "items": _FILE_ITEM_SCHEMA},
        "files_to_modify": {"type": "array", "items": _FILE_ITEM_SCHEMA},
        "read_only_changes": {"type": "array", "items": _FILE_ITEM_SCHEMA},
        "person_actions": {"type": "array", "items": _PERSON_ACTION_SCHEMA},
        "context_files": {"type": "array", "items": {"type": "string"}},
        "architecture": {
            "type": "object",
            "properties": {"layers": _NULLABLE_STRING, "state_management": _NULLABLE_STRING, "routing": _NULLABLE_STRING},
            "required": ["layers", "state_management", "routing"],
            "additionalProperties": False,
        },
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "discovery_evidence": {"type": "array", "items": {"type": "string"}},
        "plan_markdown": {"type": "string"},
    },
    "required": [
        "feature_summary", "reasoning", "questions", "files_to_create", "files_to_modify", "read_only_changes",
        "person_actions", "context_files", "architecture", "acceptance_criteria", "discovery_evidence", "plan_markdown",
    ],
    "additionalProperties": False,
}


class PlannerLoopError(RuntimeError):
    """A hard failure from the planner agent loop. Always hard: there is
    no partial-plan artifact worth proceeding with.

    `limit` is set when the cause was a provider limit (rate or quota); see
    elstin/agents/roles/dev_loop.py's DevLoopError."""

    def __init__(self, message: str = "", *, limit: ProviderLimit | None = None) -> None:
        super().__init__(message)
        self.limit = limit


def _build_prompt_text(
    *,
    user_request: str,
    project_path: str,
    context_bundle: dict[str, Any],
    linked_issues_context: str | None,
    acceptance_criteria_hint: list[str] | None,
    prior_work: dict[str, Any],
    clarification_answers: list[dict[str, Any]] | None,
    exploration: str = "",
    answers_from_earlier_run: bool = False,
) -> str:
    guide = context_bundle.get("project_guide") or {}
    text = f"User request:\n{user_request}\n\nProject path: {project_path}\n\n"

    if guide.get("found") and guide.get("content"):
        text += (
            "=== PROJECT ARCHITECTURE GUIDE (AGENTS.md) — authoritative reference ===\n"
            "Read this before making any decisions about folder layout, naming, state management, "
            "dependency injection, routing, or which services/packages to use. "
            "All plan decisions MUST conform to the conventions described here.\n\n"
            + (
                # A generated guide is a summary of what elstin could see, not
                # something a person wrote and stands behind.
                "Note: this guide was generated by elstin from the repository's manifests, README and CI files. "
                "Where it disagrees with what you read in the repository, the repository is right.\n\n"
                if guide.get("generated") else ""
            )
            + guide["content"]
            + "\n=== END AGENTS.md ===\n\n"
        )
    # The planner does not yet know which files it will touch, so it gets
    # every accepted lesson; dev is shown only the ones its plan's files match.
    text += format_lessons_block(guide.get("lessons"))

    if linked_issues_context:
        text += f"Linked Jira issues:\n{linked_issues_context}\n\n"
    if acceptance_criteria_hint:
        text += "Jira acceptance criteria (use as starting point):\n"
        text += "\n".join(f"- {ac}" for ac in acceptance_criteria_hint)
        text += "\n\n"

    manifest_summaries = context_bundle.get("manifest_summaries") or []
    if manifest_summaries:
        text += "Declared dependencies (do not propose anything not listed here — see the dependency-hygiene rules above):\n" + json.dumps(manifest_summaries, indent=2) + "\n\n"
    search_terms = context_bundle.get("search_terms") or []
    if search_terms:
        text += "Starting search terms for your own exploration (not exhaustive — use your grep/find/read tools to actually locate the files this ticket needs):\n" + ", ".join(search_terms) + "\n\n"

    if exploration:
        # Before the prior-work block and the closing instruction, so the
        # findings read as context the planner already has rather than as
        # an afterthought.
        text += exploration + "\n"

    text += _format_prior_work_block(prior_work)

    if clarification_answers and answers_from_earlier_run:
        # A fresh plan (a retry, a re-plan) for a ticket a person already
        # answered questions on: the decisions stand, but this is not the
        # round trip — a question about something they do not settle is
        # still allowed.
        text += "Decisions a person already made on this ticket (follow them; do not ask these again):\n"
        for ans in clarification_answers:
            text += f"- Q: {ans.get('question', '')}\n  A: {ans.get('option_label', '')}"
            if ans.get("option_description"):
                text += f" — {ans['option_description']}"
            text += "\n"
        text += "\nExplore the codebase yourself using your read/grep/find tools, then produce an implementation plan JSON.\n"
    elif clarification_answers:
        text += "Clarification answers from the developer (incorporate these into the plan, do NOT emit questions):\n"
        for ans in clarification_answers:
            q = ans.get("question", "")
            label = ans.get("option_label", "")
            desc = ans.get("option_description", "")
            text += f"- Q: {q}\n  A: {label}"
            if desc:
                text += f" — {desc}"
            text += "\n"
        text += "\nExplore the codebase yourself using your read/grep/find tools, then produce a full implementation plan JSON (no questions field needed).\n"
    else:
        text += "Explore the codebase yourself using your read/grep/find tools, then produce an implementation plan JSON.\n"

    return text


def _image_content_block(file_path: str) -> ImageBlock | None:
    path = Path(file_path)
    if not path.exists():
        return None
    suffix = path.suffix.lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(suffix, "image/png")
    data = base64.b64encode(path.read_bytes()).decode()
    return ImageBlock(media_type=mime, data=data)


async def run_planner(
    *,
    user_request: str,
    project_path: str,
    run_id: str | None = None,
    ticket_id: int | None = None,
    attachment_paths: list[str] | None = None,
    linked_issues_context: str | None = None,
    acceptance_criteria_hint: list[str] | None = None,
    clarification_answers: list[dict[str, Any]] | None = None,
    repo_roots: dict[str, str] | None = None,
    default_label: str = "",
    project_guide: dict[str, Any] | None = None,
    prior_work: dict[str, Any] | None = None,
    read_only_labels: list[str] | None = None,
    sdk_model: str | None = None,
    sdk_effort: str | None = None,
    answers_from_earlier_run: bool = False,
    project_id: int | None = None,
) -> dict[str, Any]:
    if run_id:
        append_activity(run_id, type="status", phase="planner", title="Planning started", detail=user_request[:200])

    try:
        provider = get_provider(project_id)
    except LlmNotConfiguredError as exc:
        raise PlannerLoopError(str(exc)) from exc

    # See the equivalent in elstin/agents/roles/dev_loop.py. Planning is
    # normally the first spend on a story, so this usually only bites on a
    # re-plan after a clarification round-trip or a retry.
    planner_budget, budget_exhausted = effective_budget_usd(run_id, get_float_config("planner_sdk_max_budget_usd", 0.0))
    if budget_exhausted:
        raise PlannerLoopError(
            "Story spend ceiling (story_max_budget_usd) is exhausted — refusing to start planning. "
            "Raise the ceiling at /settings and retry the story to continue."
        )

    phase_started_at = time.monotonic()
    # The project's own choice, else the global setting (resolve_text).
    resolved_model = sdk_model or resolve_text("planner_sdk_model", project_id)
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0}
    usage_cost_usd: float | None = None
    # Summed across every attempt below, so a retried plan's provider time
    # is reported in full; survives an early raise for the finally block.
    api_duration_ms = 0

    def _on_block(block_type: str, block: Any, step: int) -> None:
        if not run_id:
            return
        if block_type == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                append_activity(run_id, type="llm", phase="planner", title="Planner reasoning", detail=text[:4000])
        elif block_type == "thinking":
            thinking = (getattr(block, "text", "") or "").strip()
            if thinking:
                append_activity(run_id, type="thinking", phase="planner", title="Planner thinking", detail=thinking[:4000])

    def _on_retry(attempt: int, error: LlmError, delay: float) -> None:
        if run_id:
            append_activity(
                run_id, type="status", phase="planner", title="Model call failed — retrying",
                detail=f"Attempt {attempt} failed: {error}. Trying again in {delay:.0f}s.", meta={"attempt": attempt},
            )

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
        # Per turn, like dev's: the phase row keeps only sums. Explorer
        # sub-agents share this hook, so their turns are logged here too.
        total = usage.input_tokens + usage.output_tokens
        if run_id and total > 0:
            append_activity(
                run_id, type="token", phase="planner", title=f"Token usage (planner turn {turn})", detail=f"{total} tokens",
                meta=turn_usage_meta(usage),
            )

    try:
        # See module docstring: RAG pre-seeding isn't ported yet — the
        # agent still explores live via its own tools below.
        context_bundle: dict[str, Any] = dict(_EMPTY_CONTEXT_BUNDLE)
        # Carried in the bundle so dev, which reads it from state after
        # planning, sees the same guide without a second lookup.
        context_bundle["project_guide"] = project_guide
        prior_work = dict(prior_work) if prior_work and prior_work.get("found") else dict(_EMPTY_PRIOR_WORK)
        context_bundle["prior_work"] = prior_work

        # Parallel read-only recon before planning (elstin/agents/roles/
        # explorer.py). Deliberately ahead of the prompt build: the whole
        # point is that these agents' large file reads happen in their own
        # throwaway contexts and only their compact findings reach the
        # planner's history. Best-effort by construction — gather_exploration
        # returns an empty block when scoping finds nothing worth a reader,
        # when it fails, or when every explorer comes back empty, and the
        # planner then behaves exactly as it did before this existed.
        exploration = ""
        # Whether the fan-out could have spent anything. gather_exploration
        # pays for a scoping call even when it then decides no explorer is
        # warranted, so this flags "money may have gone out", not "explorers
        # ran" — the plan loop's share below is reduced either way, because
        # erring conservative is the right direction for a spend cap.
        explored = False
        max_agents = get_int_config("planner_explorer_max_agents", explorer_defaults.DEFAULT_MAX_AGENTS)
        # The round trip after a question skips it: this run explored before
        # asking. Answers carried into a fresh run (a retry) do not.
        round_trip = bool(clarification_answers) and not answers_from_earlier_run
        if not round_trip and get_bool_config("planner_explorers_enabled") and max_agents > 0:
            explored = True
            exploration, explore_api_ms = await gather_exploration(
                provider=provider,
                # A cheaper model is usually enough to read code and report
                # back; falls back to the planner's own model when unset.
                model=resolve_text("planner_explorer_model", project_id) or resolved_model,
                project_path=project_path,
                user_request=user_request,
                repo_roots=repo_roots,
            default_label=default_label,
                max_agents=max_agents,
                max_turns=get_int_config("planner_explorer_max_turns", explorer_defaults.DEFAULT_MAX_TURNS),
                # Already clamped to what the story has left, so the
                # explorers' own slices come out of the real remainder.
                planner_budget_usd=planner_budget,
                effort=sdk_effort or get_config("planner_sdk_effort"),
                # Same hook the plan loop uses, so explorer tokens land in
                # this phase's story_usage row instead of going unbilled.
                on_turn=_on_turn,
                run_id=run_id,
            )
            api_duration_ms += explore_api_ms

        text = _build_prompt_text(
            user_request=user_request, project_path=project_path, context_bundle=context_bundle,
            linked_issues_context=linked_issues_context, acceptance_criteria_hint=acceptance_criteria_hint,
            prior_work=prior_work, clarification_answers=clarification_answers,
            exploration=exploration, answers_from_earlier_run=answers_from_earlier_run,
        )

        content_blocks: list[Any] = [TextBlock(text=text)]
        for img_path in attachment_paths or []:
            block = _image_content_block(img_path)
            if block:
                content_blocks.append(block)

        tool_fns, tool_schemas = make_planner_tools(
            project_path=project_path, repo_roots=repo_roots, default_label=default_label,
            read_only_labels=read_only_labels,
        )
        # The same roots make_planner_tools resolves reads through, so the
        # gate judges a path exactly as the planner's own tools would have
        # — a path the planner could have read is never called missing. And
        # the same read-only repos, which the gate refuses as targets.
        gate_workspace = Workspace(
            default_label=default_label, roots=dict(repo_roots or {default_label: project_path}),
            read_only=frozenset(read_only_labels or ()),
        )

        config = LoopConfig(
            provider=provider,
            model=resolved_model,
            system=build_planner_system(project_path),
            tools=tool_defs_from_schemas(tool_schemas),
            tool_fns=tool_fns,
            response_format=ResponseFormat(name="dev_plan", schema=_PLAN_JSON_SCHEMA),
            effort=sdk_effort or get_config("planner_sdk_effort"),
            max_turns=get_int_config("planner_sdk_max_turns", 40),
            # The other half of the cap. explorer._explorer_budget funds the
            # fan-out from half of planner_budget and reserves the rest for
            # this loop, but that reservation only holds if it is applied
            # here: handing over the full cap again would let the phase spend
            # about 1.5x its configured limit, and breach the story ceiling
            # by the same margin. This phase's usage only reaches the DB in
            # the finally block below, so effective_budget_usd cannot see the
            # explorers' spend and the split has to be arithmetic.
            max_budget_usd=(planner_budget / 2 if explored and planner_budget > 0 else planner_budget),
            compaction_token_threshold=get_int_config("agent_compaction_token_threshold", 120_000),
            # Every planner tool is read-only, so a whole turn's calls run
            # concurrently. No progress_tools/max_idle_turns: planning
            # never modifies files, so an idle-turn budget is meaningless.
            read_only_tools=READ_ONLY_TOOLS,
            # A person's messages to this run, read at the start of the next
            # turn (elstin/services/guidance.py).
            pending_messages=(lambda: take_guidance(run_id, phase="planner")) if run_id else None,
            on_block=_on_block,
            on_turn=_on_turn,
            on_retry=_on_retry,
        )

        max_attempts = max(1, get_int_config("planner_sdk_max_retries", 2) + 1)
        last_detail: str | None = None
        plan: dict[str, Any] | None = None
        correction: str | None = None

        for attempt in range(1, max_attempts + 1):
            blocks = content_blocks if correction is None else [*content_blocks, TextBlock(text=correction)]
            try:
                result = await run_agent_loop(config, [Message(role="user", content=blocks)])
            except LlmError as exc:
                raise PlannerLoopError(f"Planner agent loop failed: {exc}", limit=provider_limit_of(exc)) from exc

            api_duration_ms += result.api_duration_ms
            record_run_outcome(run_id, planner_turns=result.turns_used)
            logger.info(
                "Planner result run_id=%s attempt=%d/%d terminal_reason=%s is_error=%s has_plan=%s",
                run_id, attempt, max_attempts, result.terminal_reason, result.is_error, result.structured_output is not None,
            )
            if not result.is_error and isinstance(result.structured_output, dict):
                candidate = raw_plan = result.structured_output
                questions_field = candidate.get("questions") or []
                # Whether this is a plan object at all, judged on the answer as
                # it arrived and ahead of normalize_plan, which would read a
                # string where a list belongs one character at a time. A
                # mangled or stub answer is sent back for being that, instead
                # of for the empty file lists it leaves behind
                # (plan_gate.check_plan_integrity).
                integrity = check_plan_integrity(candidate, clarifying=bool(questions_field))
                if not questions_field and not integrity:
                    # A missing `@`, or a change in a read-only repo, is fixed
                    # here rather than spent a retry on (plan_gate.normalize_plan).
                    candidate, fixes = normalize_plan(candidate, gate_workspace)
                    if fixes and run_id:
                        append_activity(
                            run_id, type="status", phase="planner", title=f"Plan paths: {len(fixes)} note(s)",
                            detail="\n".join(f"- {fix}" for fix in fixes)[:4000],
                        )
                # A clarification round-trip's plan fields (files, criteria)
                # are allowed to be thin: the plan is a placeholder until the
                # human answers, and planner_node parks the run at
                # awaiting_clarification instead of handing it to dev. The
                # prompt says so too (PLANNER_SYSTEM_CORE). But the questions
                # themselves are what reaches the human, so they get their
                # own check instead of no check at all — see
                # plan_gate.check_questions's docstring for the failure this
                # closes.
                defects = integrity or (
                    check_questions(questions_field) if questions_field else check_plan(candidate, gate_workspace)
                )
                blockers = blocking(defects)
                if not blockers:
                    plan = candidate
                    # A warning does not cost a retry, but it is exactly what
                    # the human at the approval gate wants to know: this plan
                    # cites files that are not where it says they are.
                    if defects and run_id:
                        append_activity(
                            run_id, type="status", phase="planner",
                            title=f"Plan gate: {len(defects)} warning(s) — the plan is usable but check its evidence",
                            detail="\n".join(f"- {d['reason']}" for d in defects)[:4000],
                        )
                    break
                last_detail = "plan gate rejected the plan: " + "; ".join(str(d["reason"]) for d in blockers[:4])
                # The warnings ride along in the correction: the attempt is
                # being re-run anyway, so it may as well fix them too.
                correction = format_plan_feedback(defects, attempt=attempt, max_attempts=max_attempts)
                if run_id:
                    # What arrived, beside why it was refused: the feed cuts
                    # the plan's own text before its file lists, so without
                    # this a rejection cannot say whether they were empty or
                    # never made it into the answer (plan_gate.plan_shape).
                    shape = plan_shape(raw_plan)
                    append_activity(
                        run_id, type="status", phase="planner",
                        title=f"Plan gate rejected the plan: {len(blockers)} problem(s)",
                        detail=("\n".join(f"- {d['reason']}" for d in defects) + "\n" + describe_plan_shape(shape))[:4000],
                        meta={"kinds": sorted({str(d["kind"]) for d in blockers}), "plan_shape": shape},
                    )
            else:
                # A loop that ended in error already exhausted elstin/agents/
                # loop.py's own retries for anything retryable, so whatever
                # killed it (bad request, auth, a refusal, a truncated turn)
                # will kill an identical retry too — raise now instead of
                # paying for the same failure max_attempts times. Retrying is
                # only worth it for the other case: the run completed fine but
                # the model just didn't emit a valid plan object.
                last_detail = result.error_message or f"no structured plan produced (terminal_reason={result.terminal_reason!r})"
                if result.is_error:
                    logger.warning("Planner attempt %d/%d failed run_id=%s: %s", attempt, max_attempts, run_id, last_detail)
                    raise PlannerLoopError(f"Planner agent loop failed: {last_detail}", limit=result.limit)

            logger.warning("Planner attempt %d/%d failed run_id=%s: %s", attempt, max_attempts, run_id, last_detail)
            if attempt >= max_attempts:
                raise PlannerLoopError(f"Planner agent loop failed after {max_attempts} attempt(s): {last_detail}")
            if run_id:
                append_activity(run_id, type="status", phase="planner", title=f"Retrying planner (attempt {attempt + 1}/{max_attempts})", detail=last_detail[:1000])

        if plan is None:  # pragma: no cover — loop above raises first
            raise PlannerLoopError(f"Planner agent loop failed after {max_attempts} attempt(s): {last_detail or 'no error detail reported'}")
    finally:
        if run_id:
            record_phase_usage(
                run_id, phase="planner", model=resolved_model, provider=provider.name,
                duration_ms=int((time.monotonic() - phase_started_at) * 1000),
                api_duration_ms=api_duration_ms or None,
                cost_usd=usage_cost_usd, **usage_totals,
            )

    reasoning = plan.get("reasoning")
    if run_id and reasoning:
        append_activity(run_id, type="thinking", phase="planner", title="Planner reasoning", detail=str(reasoning)[:4000])

    questions = plan.get("questions") or []
    if not isinstance(questions, list):
        questions = []

    acceptance = plan.get("acceptance_criteria") or []
    if isinstance(acceptance, str):
        acceptance = [acceptance]

    if run_id:
        if questions:
            append_activity(run_id, type="status", phase="planner", title="Planner has clarification questions", detail=f"{len(questions)} question(s) before finalising plan")
        else:
            append_activity(run_id, type="status", phase="planner", title="Plan ready for approval", detail=plan.get("feature_summary", "Plan created."))

    return {
        "context_bundle": context_bundle,
        "plan": plan,
        "questions": questions,
        "acceptance_criteria": acceptance,
        "messages": [{"role": "planner", "content": plan.get("feature_summary", "Plan created.")}],
    }
