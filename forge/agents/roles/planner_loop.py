"""LLM-driven planner, running through forge/agents/loop.py's
run_agent_loop against forge/llm/registry.py's configured provider. The
agent explores the target project live via forge/tools/planner_tools.py's
own Read/Grep/Glob-equivalent tools and produces the plan in the same
turn, schema-validated by the provider's native structured output
(forge/llm/types.py:ResponseFormat).

Tool surface: forge/tools/planner_tools.py's make_planner_tools() —
forge's own read-only tools, since no provider ships built-in ones the way
the old Claude Agent SDK did. Planning is read-only by nature (no
path_guard-enforced *write* scope needed the way forge/agents/roles/
dev_loop.py's dev loop needs one), and forge/agents/loop.py's tool
dispatch is itself the permission boundary — see that module's docstring
— so there is no separate can_use_tool layer to maintain here anymore.

**Not yet built**: the RAG context pre-seeding (`discover_context`) and
prior-work branch discovery (`discover_prior_work`/`select_base_ref`). Both
pull in a genuinely separate subsystem — Qdrant-backed embeddings, a
multi-provider LLM "analyst" for RAG summarization, and a SQLite
ticket/run-index store — worth building as its own piece of work, not a
side effect of getting the planner loop running. Their absence doesn't
break planning: the agent still explores the codebase live via its own
tools every run, just without a pre-computed head start (search terms,
grep hits, manifest summaries) or awareness of prior branches for this
ticket. `context_bundle`/`prior_work` below are the same empty shape
`discover_context`/`discover_prior_work` would return when they find
nothing, so nothing downstream needs to special-case "not built yet"
versus "built but found nothing".
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from forge.agents.loop import LoopConfig, run_agent_loop, tool_defs_from_schemas
from forge.agents.roles.planner import _format_prior_work_block, build_planner_system
from forge.config.store import get_config, get_float_config, get_int_config
from forge.llm.errors import LlmError
from forge.llm.registry import LlmNotConfiguredError, get_provider
from forge.llm.types import ImageBlock, LlmResponse, Message, ResponseFormat, TextBlock
from forge.services.run_activity import append_activity
from forge.services.usage import record_phase_usage
from forge.tools.planner_tools import make_planner_tools

logger = logging.getLogger("forge.planner_loop")

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
        "feature_summary", "reasoning", "questions", "files_to_create", "files_to_modify",
        "context_files", "architecture", "acceptance_criteria", "discovery_evidence", "plan_markdown",
    ],
    "additionalProperties": False,
}


class PlannerLoopError(RuntimeError):
    """A hard failure from the planner agent loop. Always hard: there is
    no partial-plan artifact worth proceeding with."""


def _build_prompt_text(
    *,
    user_request: str,
    project_path: str,
    context_bundle: dict[str, Any],
    linked_issues_context: str | None,
    acceptance_criteria_hint: list[str] | None,
    prior_work: dict[str, Any],
    clarification_answers: list[dict[str, Any]] | None,
) -> str:
    guide = context_bundle.get("project_guide") or {}
    text = f"User request:\n{user_request}\n\nProject path: {project_path}\n\n"

    if guide.get("found") and guide.get("content"):
        text += (
            "=== PROJECT ARCHITECTURE GUIDE (AGENTS.md) — authoritative reference ===\n"
            "Read this before making any decisions about folder layout, naming, state management, "
            "dependency injection, routing, or which services/packages to use. "
            "All plan decisions MUST conform to the conventions described here.\n\n"
            + guide["content"]
            + "\n=== END AGENTS.md ===\n\n"
        )

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

    text += _format_prior_work_block(prior_work)

    if clarification_answers:
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
    sdk_model: str | None = None,
    sdk_effort: str | None = None,
) -> dict[str, Any]:
    if run_id:
        append_activity(run_id, type="status", phase="planner", title="Planning started", detail=user_request[:200])

    try:
        provider = get_provider()
    except LlmNotConfiguredError as exc:
        raise PlannerLoopError(str(exc)) from exc

    phase_started_at = time.monotonic()
    resolved_model = sdk_model or get_config("planner_sdk_model")
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

    try:
        # See module docstring: RAG pre-seeding and prior-work discovery
        # aren't ported yet — the agent still explores live via its own
        # tools below.
        context_bundle: dict[str, Any] = dict(_EMPTY_CONTEXT_BUNDLE)
        prior_work: dict[str, Any] = dict(_EMPTY_PRIOR_WORK)
        prior_work["selected_branch"] = None
        context_bundle["prior_work"] = prior_work

        text = _build_prompt_text(
            user_request=user_request, project_path=project_path, context_bundle=context_bundle,
            linked_issues_context=linked_issues_context, acceptance_criteria_hint=acceptance_criteria_hint,
            prior_work=prior_work, clarification_answers=clarification_answers,
        )

        content_blocks: list[Any] = [TextBlock(text=text)]
        for img_path in attachment_paths or []:
            block = _image_content_block(img_path)
            if block:
                content_blocks.append(block)

        tool_fns, tool_schemas = make_planner_tools(project_path=project_path)

        config = LoopConfig(
            provider=provider,
            model=resolved_model,
            system=build_planner_system(project_path),
            tools=tool_defs_from_schemas(tool_schemas),
            tool_fns=tool_fns,
            response_format=ResponseFormat(name="dev_plan", schema=_PLAN_JSON_SCHEMA),
            effort=sdk_effort or get_config("planner_sdk_effort"),
            max_turns=get_int_config("planner_sdk_max_turns", 40),
            max_budget_usd=get_float_config("planner_sdk_max_budget_usd", 0.0),
            compaction_token_threshold=get_int_config("agent_compaction_token_threshold", 120_000),
            on_block=_on_block,
            on_turn=_on_turn,
        )

        max_attempts = max(1, get_int_config("planner_sdk_max_retries", 2) + 1)
        last_detail: str | None = None
        plan: dict[str, Any] | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = await run_agent_loop(config, [Message(role="user", content=content_blocks)])
            except LlmError as exc:
                raise PlannerLoopError(f"Planner agent loop failed: {exc}") from exc

            api_duration_ms += result.api_duration_ms
            logger.info(
                "Planner result run_id=%s attempt=%d/%d terminal_reason=%s is_error=%s has_plan=%s",
                run_id, attempt, max_attempts, result.terminal_reason, result.is_error, result.structured_output is not None,
            )
            if not result.is_error and isinstance(result.structured_output, dict):
                plan = result.structured_output
                break

            # A loop that ended in error already exhausted forge/agents/
            # loop.py's own retries for anything retryable, so whatever
            # killed it (bad request, auth, a refusal, a truncated turn)
            # will kill an identical retry too — raise now instead of
            # paying for the same failure max_attempts times. Retrying is
            # only worth it for the other case: the run completed fine but
            # the model just didn't emit a valid plan object.
            last_detail = result.error_message or f"no structured plan produced (terminal_reason={result.terminal_reason!r})"
            logger.warning("Planner attempt %d/%d failed run_id=%s: %s", attempt, max_attempts, run_id, last_detail)
            if result.is_error:
                raise PlannerLoopError(f"Planner agent loop failed: {last_detail}")
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
