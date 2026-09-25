"""Parallel read-only recon sub-agents for the planner.

The planner's most expensive phase is not producing the plan, it is finding
out what the repo already does. Done in one context that cost is quadratic
in a hidden way: every file it reads stays in the history and is re-sent on
every later turn, so ten exploratory turns make turn eleven enormous. The
fix is context isolation, not speed. Each explorer runs its own throwaway
`run_agent_loop` with its own turn cap, reads whatever it needs, and hands
back a few hundred tokens of findings. The bulky reads never enter the
planner's context at all.

Three stages, run from forge/agents/roles/planner_loop.py before the plan
loop starts:

1. `scope_investigation` — one structured call that reads the ticket (which
   already carries the codebase skeleton, see forge/daemon/main.py's
   `_build_story_brief`) and returns a short, closed list of areas worth a
   dedicated reader. It may return none, which is the point: a one-line
   ticket should skip the whole mechanism rather than pay for three agents
   to confirm it is simple.
2. `run_explorer` — one sub-agent per area, all concurrent.
3. the planner loop runs as before, with the findings folded into its
   prompt and its own read tools still available for anything it wants to
   check itself.

Why this shape rather than a `spawn_agent` tool the planner calls
mid-loop: forge/agents/loop.py's ToolFn is a *sync* callable dispatched to
a thread, so a tool that needs to await provider calls does not fit it,
and making it fit would introduce recursion that then has to be bounded.
Deciding the fan-out up front costs one extra call, keeps the total spend
knowable before any sub-agent starts, and leaves the graph, the activity
phases and the run state completely untouched — this module is internal to
the planner step.

Sub-agents get forge/tools/planner_tools.py's read-only trio and nothing
else, so there is no write path to guard and no way for an explorer to
spawn another one. Hard constraint #9 holds: these are `run_agent_loop`
calls like every other planner/dev model interaction, not direct provider
access.

The explorer system prompt below is adapted from the `scout` agent in
Aster (github.com/Zfinix/aster, Apache-2.0), whose wording for this exact
job — ground every claim in a file you actually read, cite `path:line`,
search before reading, say plainly when the answer is not in the repo — is
better tuned than anything worth re-deriving here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from forge.agents.loop import (
    TERMINAL_MAX_BUDGET,
    TERMINAL_MAX_TURNS,
    LoopConfig,
    run_agent_loop,
    tool_defs_from_schemas,
)
from forge.llm.base import LlmProvider
from forge.llm.types import ResponseFormat, text_message
from forge.services.run_activity import append_activity
from forge.services.usage import record_run_outcome
from forge.tools.planner_tools import READ_ONLY_TOOLS, make_planner_tools

logger = logging.getLogger("forge.explorer")

# Defaults mirrored by the config fields in forge/config/store.py
# (planner_explorer_*), so a deployment can tune these live without a code
# change. The turn cap matches the recon agent this prompt came from.
DEFAULT_MAX_AGENTS = 3
DEFAULT_MAX_TURNS = 8

# Per-explorer findings cap. An explorer that ignores "compact" and writes
# an essay would otherwise defeat the whole purpose, which is to keep the
# planner's context small.
_MAX_FINDINGS_CHARS = 6_000

# Stops that mean "out of room", not "answered". Neither is an error
# (forge/agents/loop.py returns is_error=False for both), which is exactly
# why they need naming here: nothing else distinguishes an explorer that
# finished from one that was cut off mid-read.
_CUT_SHORT_REASONS = frozenset({TERMINAL_MAX_BUDGET, TERMINAL_MAX_TURNS})

_WRAP_UP_PROMPT = """You are out of turns. Stop investigating and report now, from what you have already read.

Report only what the files you actually read support, with `path:line` citations. If you could not
answer the task, say exactly that and name what you would have had to read next — a plan built on a
guess of yours is worse than one that knows this question is still open."""

EXPLORER_SYSTEM = """You are a recon specialist on a software planning run: first in, travels light, reports what is actually there.

You receive ONE self-contained investigation task. Answer it from the repository itself, not from assumption.

- Ground every claim in a file you actually read. Cite locations as `path:line`.
- Search broadly first, then read only the few files that matter. Never read a whole large file when a range answers the question — your read tool serves at most 250 lines per call, so use grep to find the line and offset/limit to read around it.
- You are read-only. You cannot write, edit or run anything, and you are not planning the work — another agent does that with your report in hand.
- If the task cannot be answered from the repository, say so plainly and state what is missing. A confident wrong answer is far worse than "this is not in the repo".
- Answer with a compact report: the direct answer first, then the evidence, then anything an implementation plan must account for. Short headings and bullets. No preamble, no restating the task.
"""

_SCOPE_SYSTEM = """You are scoping the investigation for a software implementation plan.

You get a ticket and (usually) a pre-computed map of the repository's source files. Decide which parts of the codebase a planner must understand before it can plan this work, and turn each into ONE self-contained question for a read-only agent that will investigate it in parallel with the others.

Rules:
- Each area must be answerable by reading code, independent of the other areas, and worth a dedicated reader. Split by part of the system (the UI layer, the API client, the data model, the routing, the test setup), never by step of the work.
- Each question must stand alone. The agent answering it sees only the question, not the ticket, the plan, or the other areas.
- path_hints are optional starting points from the file map, not instructions — leave the list empty when you have none.
- Return FEWER areas than the maximum when the ticket is small, and an EMPTY list when the ticket names its own files or is obviously a one-file change. An unnecessary investigation costs real money and teaches the planner nothing.
"""

_AREA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Two to four words, e.g. 'cart UI layer'"},
        "question": {"type": "string", "description": "The self-contained question this agent must answer"},
        "path_hints": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "question", "path_hints"],
    "additionalProperties": False,
}

# Strict json_schema rules, same as the plan schema in planner_loop.py:
# every property listed in `required` at every level, additionalProperties
# false throughout. See that module's _PLAN_JSON_SCHEMA.
_SCOPE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"areas": {"type": "array", "items": _AREA_SCHEMA}},
    "required": ["areas"],
    "additionalProperties": False,
}


def _explorer_budget(planner_budget_usd: float, max_agents: int) -> float:
    """Split the planner's spend cap across the fan-out.

    Without this each explorer would carry the planner's *whole* allowance,
    so three of them could spend three times the cap the operator set and
    the plan loop itself would still be owed its share. Zero stays zero,
    since zero means unbounded everywhere else in forge."""
    if planner_budget_usd <= 0:
        return 0.0
    # Half the cap is reserved for the plan loop, which is the part that
    # actually has to produce something.
    return (planner_budget_usd / 2) / max(max_agents, 1)


async def scope_investigation(
    *,
    provider: LlmProvider,
    model: str,
    user_request: str,
    max_agents: int = DEFAULT_MAX_AGENTS,
    effort: str | None = None,
    on_turn: Any = None,
    run_id: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Pick the investigation areas. Returns (areas, api_duration_ms).

    Runs through `run_agent_loop` with no tools rather than
    forge/services/generation.py, for two reasons: generation.py hardcodes
    the verifier/QA model, and hard constraint #9 puts planner-side model
    calls on the loop. With an empty tool list the loop makes exactly one
    provider call and returns its structured output.

    A failure here is never fatal. Exploration is a head start, so a scope
    call that errors or returns nothing usable degrades to "no explorers"
    and the planner works the way it did before this module existed.
    """
    prompt = (
        f"Ticket and repository map:\n{user_request}\n\n"
        f"Return at most {max_agents} investigation area(s). Fewer is better. "
        "Return an empty list if this ticket does not need any."
    )
    config = LoopConfig(
        provider=provider,
        model=model,
        system=_SCOPE_SYSTEM,
        response_format=ResponseFormat(name="investigation_scope", schema=_SCOPE_JSON_SCHEMA),
        effort=effort,
        max_turns=1,
        on_turn=on_turn,
    )
    result = await run_agent_loop(config, [text_message("user", prompt)])

    if result.is_error or not isinstance(result.structured_output, dict):
        logger.warning(
            "Explorer scoping produced no areas (terminal_reason=%s error=%s) — planning without explorers",
            result.terminal_reason, result.error_message,
        )
        return [], result.api_duration_ms

    areas: list[dict[str, Any]] = []
    for raw in result.structured_output.get("areas") or []:
        # Checked before appending, not after: max_agents=0 means "off"
        # (the config field says so), and a post-append check would keep
        # the first area anyway.
        if len(areas) >= max_agents:
            break
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question") or "").strip()
        if not question:
            continue
        areas.append({
            "name": str(raw.get("name") or "investigation").strip(),
            "question": question,
            "path_hints": [str(h) for h in (raw.get("path_hints") or []) if h],
        })

    if run_id:
        append_activity(
            run_id, type="status", phase="planner",
            title=f"Scoped {len(areas)} parallel investigation(s)" if areas else "No parallel investigation needed",
            detail="; ".join(a["name"] for a in areas) or None,
        )
    return areas, result.api_duration_ms


async def run_explorer(
    *,
    provider: LlmProvider,
    model: str,
    project_path: str,
    area: dict[str, Any],
    repo_roots: dict[str, str] | None = None,
    default_label: str = "",
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = 0.0,
    effort: str | None = None,
    on_turn: Any = None,
    run_id: str | None = None,
) -> str:
    """One read-only sub-agent against one area. Returns (findings,
    complete) — an empty findings string if it produced nothing usable, and
    complete=False if it was stopped by a cap rather than by answering, so
    the caller can label a partial report instead of passing it off as a
    finished one.

    Never raises: an explorer is an optimization, so a sub-agent that fails
    costs the planner a head start and nothing else. Its own loop already
    retried anything retryable."""
    tool_fns, tool_schemas = make_planner_tools(project_path=project_path, repo_roots=repo_roots, default_label=default_label)
    hints = "\n".join(f"- {h}" for h in area.get("path_hints") or [])
    prompt = (
        f"Investigation task: {area['question']}\n\n"
        + (f"Possible starting points (verify before trusting them):\n{hints}\n\n" if hints else "")
        + "Investigate and report."
    )

    config = LoopConfig(
        provider=provider,
        model=model,
        system=EXPLORER_SYSTEM,
        tools=tool_defs_from_schemas(tool_schemas),
        tool_fns=tool_fns,
        effort=effort,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        # Every planner tool is read-only, so a whole turn's calls run
        # concurrently. No progress_tools: an explorer never writes, so an
        # idle-turn budget would fire on correct behavior.
        read_only_tools=READ_ONLY_TOOLS,
        on_turn=on_turn,
    )

    try:
        result = await run_agent_loop(config, [text_message("user", prompt)])
    except Exception as exc:  # noqa: BLE001 — a failed head start must not fail the plan
        logger.warning("Explorer %r raised: %s", area["name"], exc)
        return "", False

    complete = result.terminal_reason not in _CUT_SHORT_REASONS
    findings = (result.final_text or "").strip()
    if not complete:
        # An explorer stopped by a cap has NOT answered: its final_text is
        # whatever prose sat in the turn where it also called a tool, which
        # is an announcement of the read it was about to do ("Let me check
        # the schema in openapi.json") and not a finding at all. Shipping
        # that to the planner as one — under a heading promising `path:line`
        # citations — is how a planner ends up reasoning from a sentence
        # that was never finished. So the reads already paid for are
        # salvaged with one tool-free turn instead: same history, no tools,
        # "report what you have". Cheap, and the alternative is throwing the
        # whole investigation away.
        salvaged = await _wrap_up(config, result, area=area, run_id=run_id)
        if salvaged:
            findings, complete = salvaged, True
        else:
            logger.warning(
                "Explorer %r was cut short (%s) and could not summarise — reporting it as incomplete",
                area["name"], result.terminal_reason,
            )
    if result.is_error and not findings:
        logger.warning("Explorer %r ended in error: %s", area["name"], result.error_message)
        return "", False
    if len(findings) > _MAX_FINDINGS_CHARS:
        findings = findings[:_MAX_FINDINGS_CHARS] + "\n...[report truncated]"

    logger.info(
        "Explorer %r finished turns=%d chars=%d terminal_reason=%s complete=%s",
        area["name"], result.turns_used, len(findings), result.terminal_reason, complete,
    )
    if run_id:
        append_activity(
            run_id, type="llm", phase="planner",
            title=f"Investigation: {area['name']}" + ("" if complete else " (incomplete)"),
            detail=findings[:4000] or "(no findings)",
            meta={"explorer": area["name"], "turns": result.turns_used, "complete": complete},
        )
    return findings, complete


async def _wrap_up(config: LoopConfig, result: Any, *, area: dict[str, Any], run_id: str | None) -> str:
    """One tool-free turn asking a capped explorer to report what it has.

    Safe to build on `result.history`: forge/agents/loop.py appends the
    turn's tool results before it checks either cap, so a capped run's
    history ends on an answered tool message and every call in it has its
    result. `tools=[]` is what forces prose — the adapter omits the tools
    field entirely when the list is empty, so the model cannot spend this
    turn announcing another read."""
    wrap_config = LoopConfig(
        provider=config.provider,
        model=config.model,
        system=config.system,
        effort=config.effort,
        max_turns=1,
        on_turn=config.on_turn,
    )
    try:
        wrapped = await run_agent_loop(wrap_config, [*result.history, text_message("user", _WRAP_UP_PROMPT)])
    except Exception as exc:  # noqa: BLE001 — same rule as the explorer itself
        logger.warning("Explorer %r wrap-up raised: %s", area["name"], exc)
        return ""
    text = (wrapped.final_text or "").strip()
    logger.info("Explorer %r wrap-up chars=%d terminal_reason=%s", area["name"], len(text), wrapped.terminal_reason)
    return text


async def gather_exploration(
    *,
    provider: LlmProvider,
    model: str,
    project_path: str,
    user_request: str,
    repo_roots: dict[str, str] | None = None,
    default_label: str = "",
    max_agents: int = DEFAULT_MAX_AGENTS,
    max_turns: int = DEFAULT_MAX_TURNS,
    planner_budget_usd: float = 0.0,
    effort: str | None = None,
    on_turn: Any = None,
    run_id: str | None = None,
) -> tuple[str, int]:
    """Scope, then fan out. Returns (findings_block, api_duration_ms).

    The findings block is ready to paste into the planner's prompt, and is
    empty when scoping found nothing worth investigating or every explorer
    came back empty — so the caller can append it unconditionally."""
    areas, api_duration_ms = await scope_investigation(
        provider=provider, model=model, user_request=user_request,
        max_agents=max_agents, effort=effort, on_turn=on_turn, run_id=run_id,
    )
    if not areas:
        return "", api_duration_ms

    record_run_outcome(run_id, explorer_count=len(areas))
    budget = _explorer_budget(planner_budget_usd, len(areas))
    logger.info("Dispatching %d explorer(s) model=%s max_turns=%d budget_each=%.4f", len(areas), model, max_turns, budget)

    reports = await asyncio.gather(*(
        run_explorer(
            provider=provider, model=model, project_path=project_path, area=area,
            repo_roots=repo_roots,
            default_label=default_label,
            max_turns=max_turns, max_budget_usd=budget, effort=effort,
            on_turn=on_turn, run_id=run_id,
        )
        for area in areas
    ))

    sections: list[str] = []
    incomplete = 0
    for area, (report, complete) in zip(areas, reports):
        if not report:
            continue
        if not complete:
            incomplete += 1
        marker = "" if complete else "  [INCOMPLETE — this agent ran out of turns; treat it as a lead, not an answer]"
        sections.append(f"### {area['name']}{marker}\nQuestion: {area['question']}\n\n{report}")
    if not sections:
        return "", api_duration_ms

    block = (
        "=== REPOSITORY INVESTIGATION (read-only agents, run in parallel before planning) ===\n"
        "Each section answers one question about the existing code, with `path:line` citations. "
        "These are findings from this repository, not requirements — treat them as what IS, and "
        "verify anything you intend to rely on heavily with your own read/grep tools.\n"
        # Said explicitly because the cost of getting this wrong is not a
        # thinner plan but a confident, wrong one: a partial report reads
        # exactly like a complete one, and a planner that treats it as
        # settled invents the rest of the file it was never told about.
        "A section marked INCOMPLETE was cut off before it finished. It may name a file without "
        "saying where it is, or raise a question without answering it. Anything you take from such "
        "a section you must confirm yourself with read/grep/find before the plan relies on it.\n\n"
        + "\n\n".join(sections)
        + "\n=== END INVESTIGATION ===\n"
    )
    if incomplete:
        logger.warning("%d of %d explorer report(s) are incomplete", incomplete, len(sections))
    return block, api_duration_ms

