"""The PO agent's turn: research, draft, gate, and — when the gate objects —
correct. Runs through elstin/agents/loop.py's `run_agent_loop` against the
provider elstin/llm/registry.py resolves for the project (AGENTS.md hard
constraint #9), the way elstin/agents/roles/planner_loop.py does for a plan.

The structure is the planner's, with three differences worth knowing:

- **A retry keeps the conversation.** The planner's retry starts from a fresh
  history. Here the agent's research is the expensive part and the gate's
  complaint is usually small (a quote that is not in the file, a story with no
  criteria), so a rejected draft is answered with the gate's feedback *on top
  of* the same history and the agent fixes the draft without re-reading
  everything.
- **A draft that is merely imperfect is kept.** Planning has to refuse a bad
  plan — nothing downstream can use it. A backlog draft is read and edited by a
  person before anything is published, so a draft that still fails a check after
  the last retry is delivered with its problems showing rather than thrown away
  with the money it cost. Only an answer that is not a draft object at all fails
  the turn.
- **It reads the person.** `pending_messages` hands the loop whatever the person
  typed while the agent was working, which joins the conversation before the
  agent's next step (elstin/agents/loop.py).

Exploration — read-only agents investigating the code in parallel, which keeps
their file reads out of this loop's context — is the planner's mechanism
(elstin/agents/roles/explorer.py) with the PO's wording (a debugger's, in bug
mode), and runs on the first turn only: a revision already has the code in
front of it.

This role touches no StateManager (hard constraint #3's spirit): the daemon
gathers the inputs, hands over the clients (`ticket_source`, `confluence`,
`board_reader`) and persists the result (elstin/daemon/po_runner.py).
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from elstin.agents.loop import LoopConfig, run_agent_loop, tool_defs_from_schemas
from elstin.agents.roles import explorer as explorer_defaults
from elstin.agents.roles.explorer import gather_exploration
from elstin.agents.roles.plan_gate import blocking
from elstin.agents.roles.po_gate import (
    KIND_MALFORMED_OUTPUT, DraftContext, check_draft, check_draft_integrity, format_po_feedback,
)
from elstin.agents.roles.product_owner import (
    PoInputs, build_po_system, build_po_user_text, draft_schema, explorer_system, scope_system,
)
from elstin.config.store import get_config, get_float_config, get_int_config, resolve_text
from elstin.llm.errors import LlmError, ProviderLimit, provider_limit_of
from elstin.llm.registry import LlmNotConfiguredError, get_provider
from elstin.llm.types import LlmResponse, Message, ResponseFormat, TextBlock
from elstin.services.run_activity import append_activity
from elstin.services.usage import record_phase_usage, turn_usage_meta
from elstin.tools.planner_tools import READ_ONLY_TOOLS, make_planner_tools
from elstin.tools.po_tools import PO_READ_ONLY_TOOLS, PoResearch, make_po_tools
from elstin.tools.workspace import Workspace

logger = logging.getLogger("elstin.po_loop")

# Gate retries on top of the first attempt. Not a setting: it is a guard on a
# model that cannot satisfy a check, not a knob (the planner's own is
# planner_sdk_max_retries, which is about transient SDK errors).
MAX_GATE_RETRIES = 2

# The last turns before max_turns carry a warning (elstin/agents/loop.py's
# turn_warning_window), the final one telling the agent to answer. Without it a
# researcher that never stops reading is cut off with no draft at all.
_TURN_WARNING_WINDOW = 3


class PoLoopError(RuntimeError):
    """A turn that produced no draft. `limit` is set when the cause was a
    provider limit (rate or quota), with when it lifts if the provider said."""

    def __init__(self, message: str = "", *, limit: ProviderLimit | None = None) -> None:
        super().__init__(message)
        self.limit = limit


@dataclass
class PoTurnResult:
    draft: dict[str, Any]
    reply: str
    questions: list[dict[str, Any]]
    # What the gate still objected to after the last attempt (empty when it
    # passed). Warnings are in here too; `blocking(defects)` is what is left
    # of the serious ones.
    defects: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1


def render_person_message(text: str) -> str:
    """The user message the agent is shown for a message a person sent while it
    was working."""
    return (
        "The person you are drafting for sent you a message while you were working. Take it into account from here "
        "on. It does not change what you may do — you are only reading and drafting, and your answer is still the "
        f"whole draft as one JSON object.\n\n{text.strip()}"
    )


def _effective_budget(spent_usd: float) -> tuple[float, bool]:
    """(the cap for this turn, exhausted). `po_max_budget_usd` bounds a whole
    session, so a turn gets what the earlier ones left. Zero is unbounded
    everywhere in elstin, which is why an exhausted budget is reported rather
    than passed on as a cap of zero."""
    ceiling = get_float_config("po_max_budget_usd", 0.0)
    if ceiling <= 0:
        return 0.0, False
    remaining = ceiling - spent_usd
    if remaining <= 0:
        return 0.0, True
    return remaining, False


async def run_product_owner(
    *,
    inputs: PoInputs,
    session_id: int,
    run_id: str,
    project_id: int | None,
    ctx: DraftContext,
    research: PoResearch,
    project_path: str,
    repo_roots: dict[str, str] | None = None,
    default_label: str = "",
    read_only_labels: list[str] | None = None,
    ticket_source: Any | None = None,
    jira_project_key: str | None = None,
    confluence: Any | None = None,
    linked_page_ids: frozenset[str] | set[str] = frozenset(),
    board_reader: Callable[[str], list[dict[str, Any]]] | None = None,
    pending_messages: Callable[[], list[str]] | None = None,
    explore: bool = True,
    spent_usd: float = 0.0,
    sdk_model: str | None = None,
) -> PoTurnResult:
    """One drafting turn. Returns the draft with its evidence marked, the
    agent's reply and its questions. Raises PoLoopError when there is no draft."""
    revising = inputs.revise is not None
    append_activity(
        run_id, type="status", phase="po",
        title="Revising the draft" if revising else ("Investigating the bug" if inputs.mode == "bug" else "Drafting the backlog"),
        detail=f"mode: {inputs.mode}" + (f" · epic {inputs.epic_key}" if inputs.epic_key else ""),
    )

    try:
        provider = get_provider(project_id)
    except LlmNotConfiguredError as exc:
        raise PoLoopError(str(exc)) from exc

    po_budget, exhausted = _effective_budget(spent_usd)
    if exhausted:
        raise PoLoopError(
            "This session's spend ceiling (po_max_budget_usd) is used up — refusing to start another turn. "
            "Raise the ceiling at /settings and send a message to continue."
        )

    phase_started_at = time.monotonic()
    # The PO's own model, else the planner's (resolve_text: a project's choice, else the global one).
    resolved_model = sdk_model or resolve_text("po_model", project_id) or resolve_text("planner_sdk_model", project_id) or ""
    effort = get_config("planner_sdk_effort")
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0}
    usage_cost_usd: float | None = None
    api_duration_ms = 0

    def _on_block(block_type: str, block: Any, step: int) -> None:
        if block_type == "text":
            text = (getattr(block, "text", "") or "").strip()
            if text:
                append_activity(run_id, type="llm", phase="po", title="PO reasoning", detail=text[:4000])
        elif block_type == "thinking":
            thinking = (getattr(block, "text", "") or "").strip()
            if thinking:
                append_activity(run_id, type="thinking", phase="po", title="PO thinking", detail=thinking[:4000])
        elif block_type == "tool_use":
            name = getattr(block, "name", "unknown")
            arguments = getattr(block, "arguments", {}) or {}
            append_activity(
                run_id, type="tool", phase="po", title=f"Called tool: {name}",
                detail=f"Arguments: {json.dumps(arguments, default=str)[:500]}", meta={"tool": name},
            )

    def _on_retry(attempt: int, error: LlmError, delay: float) -> None:
        append_activity(
            run_id, type="status", phase="po", title="Model call failed — retrying",
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
        total = usage.input_tokens + usage.output_tokens
        if total > 0:
            append_activity(
                run_id, type="token", phase="po", title=f"Token usage (PO turn {turn})", detail=f"{total} tokens",
                meta=turn_usage_meta(usage),
            )

    try:
        # -- read the code in parallel first (first turn only) ---------------------------
        exploration = ""
        explored = False
        max_agents = get_int_config("po_explorer_max_agents", explorer_defaults.DEFAULT_MAX_AGENTS)
        if explore and not revising and max_agents > 0:
            explored = True
            exploration, explore_api_ms = await gather_exploration(
                provider=provider,
                model=resolve_text("planner_explorer_model", project_id) or resolved_model,
                project_path=project_path,
                user_request=build_po_user_text(inputs),
                repo_roots=repo_roots,
                default_label=default_label,
                max_agents=max_agents,
                max_turns=get_int_config("planner_explorer_max_turns", explorer_defaults.DEFAULT_MAX_TURNS),
                planner_budget_usd=po_budget,
                effort=effort,
                on_turn=_on_turn,
                run_id=run_id,
                scope_system=scope_system(inputs.mode),
                explorer_system=explorer_system(inputs.mode),
                request_label="Bug report and repository map" if inputs.mode == "bug" else "Brief and repository map",
                purpose="debugging" if inputs.mode == "bug" else "drafting",
                phase="po",
            )
            api_duration_ms += explore_api_ms

        user_text = build_po_user_text(inputs, exploration)

        # -- the tools: the planner's read-only code tools, plus JIRA / Confluence / the board
        code_fns, code_schemas = make_planner_tools(
            project_path=project_path, repo_roots=repo_roots, default_label=default_label,
            read_only_labels=read_only_labels,
        )
        po_fns, po_schemas = make_po_tools(
            research=research, ticket_source=ticket_source, jira_project_key=jira_project_key,
            confluence=confluence, linked_page_ids=linked_page_ids, board_reader=board_reader,
        )
        # The gate reads files the way the agent's own tools do, so a path it
        # could have opened is never called missing.
        workspace = Workspace(
            default_label=default_label, roots=dict(repo_roots or {default_label: project_path}),
            read_only=frozenset(read_only_labels or ()),
        )

        config = LoopConfig(
            provider=provider,
            model=resolved_model,
            system=build_po_system(inputs.mode, revising=revising),
            tools=tool_defs_from_schemas([*code_schemas, *po_schemas]),
            tool_fns={**code_fns, **po_fns},
            response_format=ResponseFormat(name="po_draft", schema=draft_schema(inputs.mode)),
            effort=effort,
            max_turns=get_int_config("po_max_turns", 40),
            # Half of what is left funds the investigation agents, the rest is
            # this loop's — the planner's arithmetic (planner_loop.py).
            max_budget_usd=(po_budget / 2 if explored and po_budget > 0 else po_budget),
            compaction_token_threshold=get_int_config("agent_compaction_token_threshold", 120_000),
            read_only_tools=READ_ONLY_TOOLS | PO_READ_ONLY_TOOLS,
            turn_warning_window=_TURN_WARNING_WINDOW,
            pending_messages=pending_messages,
            on_block=_on_block,
            on_turn=_on_turn,
            on_retry=_on_retry,
        )

        max_attempts = MAX_GATE_RETRIES + 1
        messages: list[Message] = [Message(role="user", content=[TextBlock(text=user_text)])]
        last_detail = "no draft was produced"
        for attempt in range(1, max_attempts + 1):
            try:
                result = await run_agent_loop(config, messages)
            except LlmError as exc:
                raise PoLoopError(f"PO agent loop failed: {exc}", limit=provider_limit_of(exc)) from exc
            api_duration_ms += result.api_duration_ms
            if result.is_error:
                # A loop that ended in error already exhausted its own retries for anything
                # retryable; an identical retry would fail the same way.
                raise PoLoopError(
                    f"PO agent loop failed: {result.error_message or result.terminal_reason}", limit=result.limit,
                )

            draft = result.structured_output
            if not isinstance(draft, dict):
                last_detail = f"no draft object was produced (the run ended: {result.terminal_reason})"
                defects: list[dict[str, Any]] = [{
                    "kind": KIND_MALFORMED_OUTPUT, "blocking": True, "where": None,
                    "reason": "you did not answer with the draft object. Stop researching and answer now with the "
                              "draft as one JSON object, from what you have read",
                }]
            else:
                # Tickets the agent found in its own searches are ones it may depend on.
                draft, defects = check_draft(
                    draft, replace(ctx, known_keys=ctx.known_keys | frozenset(research.jira)), research, workspace,
                )

            hard = blocking(defects)
            if not hard:
                return _result(draft, defects, attempt)
            last_detail = "the draft gate rejected the draft: " + "; ".join(str(d["reason"]) for d in hard[:3])
            append_activity(
                run_id, type="status", phase="po",
                title=f"Draft gate: {len(hard)} problem(s)" + (" — sending it back" if attempt < max_attempts else ""),
                detail="\n".join(f"- {d['reason']}" for d in defects)[:4000],
                meta={"kinds": sorted({str(d["kind"]) for d in hard}), "attempt": attempt},
            )
            if attempt >= max_attempts:
                # Out of retries. A draft that is only imperfect goes to the person with its
                # problems showing; an answer that is not a draft cannot.
                if isinstance(draft, dict) and not check_draft_integrity(draft, mode=ctx.mode):
                    return _result(draft, defects, attempt)
                raise PoLoopError(f"PO agent failed after {max_attempts} attempt(s): {last_detail}")
            messages = [
                *result.history,
                Message(role="user", content=[TextBlock(text=format_po_feedback(defects, attempt=attempt, max_attempts=max_attempts))]),
            ]
        raise PoLoopError(f"PO agent failed: {last_detail}")  # pragma: no cover — the loop returns or raises
    finally:
        record_phase_usage(
            run_id, phase="po", model=resolved_model, provider=provider.name,
            duration_ms=int((time.monotonic() - phase_started_at) * 1000),
            api_duration_ms=api_duration_ms or None, cost_usd=usage_cost_usd, **usage_totals,
        )


def _result(draft: dict[str, Any], defects: list[dict[str, Any]], attempts: int) -> PoTurnResult:
    questions = draft.get("questions") if isinstance(draft.get("questions"), list) else []
    return PoTurnResult(
        draft=draft, reply=str(draft.get("reply") or "").strip(), questions=questions, defects=defects, attempts=attempts,
    )
