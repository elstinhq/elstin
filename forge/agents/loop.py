"""The shared agentic loop for every tool-driven forge role (dev,
planner; verifier/QA use forge/services/generation.py's simpler
single-shot `chat_completion_json` instead, since they need no tools or
multi-turn state). This is what AGENTS.md's hard constraint #9 points at:
a role never constructs an LlmProvider or calls `.complete()` itself, it
builds a LoopConfig and calls `run_agent_loop`.

Replaces the Claude Agent SDK's `query()` — this module owns everything
that SDK used to: the turn loop itself, per-turn tool dispatch (off-thread
so a slow tool like `dart analyze` never stalls the daemon's event loop),
turn/budget caps with forge-specific *soft-stop* semantics (a turn/budget
cap is real work done, not a failure — see LoopResult.terminal_reason and
forge/agents/roles/dev_loop.py's classify_loop_result, which this module
deliberately emits the same terminal_reason vocabulary for), and — because
no currently-wired provider offers server-side compaction the way the old
CLI did — its own naive summarize-and-splice compaction so a long dev run
doesn't grow its history without bound.

Tool permission gating is NOT a separate layer here (the old code had two:
an MCP server allowlist plus a `can_use_tool` callback). A tool the caller
didn't include in `tool_fns` simply isn't callable — the model never even
sees its schema, since `tools` and `tool_fns` are supplied together by the
same caller. Any finer-grained gating (forge/tools/path_guard.py's
write-scope checks) lives inside the tool function itself, exactly as it
does today; a PathGuardError there is caught here and turned into a
normal `is_error` tool_result, not a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from forge.llm.base import LlmProvider
from forge.llm.errors import LlmError, is_retryable
from forge.llm.pricing import estimate_cost_usd
from forge.llm.types import (
    ContentBlock,
    ImageBlock,
    LlmRequest,
    LlmResponse,
    Message,
    ResponseFormat,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolDef,
    ToolResultBlock,
    Usage,
    text_message,
)

logger = logging.getLogger("forge.agents.loop")

# Soft-stop terminal reasons — a turn/budget cap ended the run, but real
# work happened, so the caller proceeds to verification/approval rather
# than treating this as a failure. Kept as a small, stable vocabulary
# (forge/agents/roles/dev_loop.py:classify_loop_result checks membership
# in exactly this set) so callers never need to know the loop's internals.
TERMINAL_MAX_TURNS = "max_turns"
TERMINAL_MAX_BUDGET = "max_budget_usd"
TERMINAL_END_TURN = "end_turn"
TERMINAL_ERROR = "error"

# Provider stop reasons (forge/llm/types.py's StopReason) that make a turn
# UNUSABLE rather than complete, each ending the loop with is_error=True.
#
# Checked *before* any tool call in the same response is dispatched, which
# is the part that matters: a turn cut off at the provider's own
# output-token cap can carry a half-serialized tool call — `write_file_tool`
# with two thirds of a file in its `content` argument — and writing that
# into the worktree is far worse than failing the run. An empty-looking
# refusal is nearly as bad: left unchecked it reads as "the model decided
# nothing needed changing", which is exactly how a filtered turn used to
# sail through to a PR-open attempt.
_UNUSABLE_STOP_REASONS: dict[str, str] = {
    "max_tokens": (
        "the provider truncated this turn at its own output-token cap, so the response "
        "(including any tool call in it) may be cut off mid-way and is not safe to act on"
    ),
    "content_filter": "the provider refused or filtered this turn",
    "error": "the provider ended this turn with an unrecognized finish reason",
}

# Rough chars-per-token estimate for the compaction trigger — not exact
# (no tokenizer call here), just conservative enough that compaction fires
# comfortably before a real context-window error would.
_CHARS_PER_TOKEN_ESTIMATE = 4
_DEFAULT_COMPACTION_TOKEN_THRESHOLD = 120_000
_DEFAULT_COMPACTION_KEEP_RECENT = 8

# Retries for a transient provider error (rate limit, connection, timeout,
# 5xx — forge/llm/errors.py:RETRYABLE_ERRORS) within a single turn. A
# non-retryable error (bad request, auth, content filter) is never retried
# here — it terminates the loop immediately.
_MAX_TRANSIENT_RETRIES = 3
_RETRY_BASE_DELAY_SECONDS = 2.0
_RETRY_CEILING_SECONDS = 30.0

# A sync tool function, matching the shape forge/tools/dev_tools.py's
# make_dev_tools() already returns — kwargs in, string result out. Always
# dispatched via asyncio.to_thread (see _call_tool) so a slow one (an
# external compiler/analyzer invocation) never blocks the event loop.
ToolFn = Callable[..., str]

# (block_type, block, step) -> None. Called synchronously for every
# content block as it's produced, so a caller (dev_loop.py, planner_loop.py)
# can append_activity() without the loop knowing anything about run_id or
# the activity log's shape.
ActivityCallback = Callable[[str, ContentBlock, int], None]

# Invoked once per LLM turn (not per block) with the 1-based turn number
# and that turn's raw response — the hook for per-turn token-usage
# logging (append_activity's type="token" entries), which needs the whole
# response's Usage, not any individual content block.
TurnCallback = Callable[[int, LlmResponse], None]


def retry_backoff_seconds(attempt: int) -> float:
    return min(_RETRY_BASE_DELAY_SECONDS * (2 ** max(attempt - 1, 0)), _RETRY_CEILING_SECONDS)


def tool_defs_from_schemas(schemas: list[dict[str, Any]]) -> list[ToolDef]:
    """Adapt the {"type": "function", "function": {name, description,
    parameters}} shape forge/tools/dev_tools.py and forge/tools/
    planner_tools.py already return (OpenAI's own tool-schema shape,
    predating this migration) into forge/llm/types.py:ToolDef — shared by
    every role that builds a LoopConfig.tools list."""
    return [ToolDef(name=s["function"]["name"], description=s["function"]["description"], parameters=s["function"]["parameters"]) for s in schemas]


@dataclass
class LoopResult:
    history: list[Message]
    usage: Usage
    terminal_reason: str
    is_error: bool
    turns_used: int
    error_message: str | None = None
    structured_output: dict[str, Any] | None = None
    final_text: str = ""
    # Time spent waiting on the provider, summed across every call this
    # run made (turns plus any compaction call) — narrower than the
    # caller's own wall-clock for the phase, which also covers local tool
    # execution. Recorded as story_usage.api_duration_ms so the UI can
    # separate "slow model" from "slow dart analyze".
    api_duration_ms: int = 0


@dataclass
class LoopConfig:
    provider: LlmProvider
    model: str
    system: str
    tools: list[ToolDef] = field(default_factory=list)
    tool_fns: dict[str, ToolFn] = field(default_factory=dict)
    response_format: ResponseFormat | None = None
    effort: str | None = None
    max_turns: int = 0  # 0 = unbounded
    max_budget_usd: float = 0.0  # 0 = unbounded
    compaction_token_threshold: int = _DEFAULT_COMPACTION_TOKEN_THRESHOLD
    compaction_keep_recent: int = _DEFAULT_COMPACTION_KEEP_RECENT
    on_block: ActivityCallback | None = None
    on_turn: TurnCallback | None = None


async def _call_tool(tool_fns: dict[str, ToolFn], name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Returns (result_text, is_error). Never raises: any exception from
    the tool function (including forge/tools/path_guard.py's
    PathGuardError) becomes an is_error tool_result the model can react
    to, matching how a real tool failure looks to a human developer."""
    fn = tool_fns.get(name)
    if fn is None:
        return f"Unknown tool: {name!r}. Available tools: {sorted(tool_fns)}", True
    try:
        result = await asyncio.to_thread(fn, **arguments)
        return str(result), False
    except TypeError as exc:
        return f"Invalid arguments for tool {name!r}: {exc}", True
    except Exception as exc:  # noqa: BLE001 - a tool's own failure is data, not a loop crash
        logger.warning("Tool %s raised: %s", name, exc)
        return f"Tool {name!r} failed: {exc}", True


def _estimate_message_chars(messages: list[Message]) -> int:
    """Rough size of a history, for the compaction trigger only.

    ImageBlocks are counted too (as their base64 payload length): a
    planner run with a JIRA screenshot attached carries megabytes in one
    message, and ignoring that used to make this estimate report ~0 for
    the single largest thing in the request — under-counting in exactly
    the case where compaction matters most. The base64:token ratio is
    nothing like text's, so this is deliberately an over-estimate for
    images; over-compacting is the safe direction to be wrong in."""
    total = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, (TextBlock, ThinkingBlock)):
                total += len(block.text)
            elif isinstance(block, ToolResultBlock):
                total += len(block.content)
            elif isinstance(block, ToolCallBlock):
                total += len(json.dumps(block.arguments, default=str))
            elif isinstance(block, ImageBlock):
                total += len(block.data)
    return total


async def _complete_with_retry(config: LoopConfig, request: LlmRequest) -> tuple[LlmResponse | None, LlmError | None, int]:
    """One provider call, retrying only what forge/llm/errors.py marks
    retryable. Returns (response, last_error, api_duration_ms) — never
    raises, so both the turn loop and compaction share one resilience
    policy (compaction used to have none: a rate limit while summarizing
    hard-failed the whole run instead of backing off like a turn does).
    """
    last_error: LlmError | None = None
    api_duration_ms = 0
    for attempt in range(1, _MAX_TRANSIENT_RETRIES + 1):
        started = time.monotonic()
        try:
            response = await config.provider.complete(request)
            return response, None, api_duration_ms + int((time.monotonic() - started) * 1000)
        except LlmError as exc:
            api_duration_ms += int((time.monotonic() - started) * 1000)
            last_error = exc
            if attempt >= _MAX_TRANSIENT_RETRIES or not is_retryable(exc):
                break
            delay = retry_backoff_seconds(attempt)
            logger.warning("Transient LLM error (attempt %d/%d): %s — retrying in %.0fs", attempt, _MAX_TRANSIENT_RETRIES, exc, delay)
            await asyncio.sleep(delay)
    return None, last_error, api_duration_ms


def _split_for_compaction(history: list[Message], keep_recent: int) -> tuple[list[Message], list[Message]]:
    """Split history into (to_summarize, to_keep) without ever separating
    an assistant message's tool_calls from the tool-result message that
    answers them — OpenAI (and every OpenAI-compatible endpoint) rejects a
    request where a tool message's tool_call_id has no matching prior
    tool_calls entry."""
    if len(history) <= keep_recent:
        return [], list(history)
    split_at = len(history) - keep_recent
    # Never let split_at land right after a tool_calls-bearing assistant
    # message — walk it back to before that message instead.
    while split_at > 0:
        candidate = history[split_at - 1]
        has_pending_tool_calls = candidate.role == "assistant" and any(
            isinstance(b, ToolCallBlock) for b in candidate.content
        )
        if not has_pending_tool_calls:
            break
        split_at -= 1
    return history[:split_at], history[split_at:]


async def _compact_history(config: LoopConfig, history: list[Message], turn: int) -> tuple[list[Message], Usage, int]:
    """Summarize the older part of `history` into one message.

    Returns (new_history, usage, api_duration_ms). The summarization call
    is reported through `config.on_turn` exactly like a real turn is —
    it's a billable provider call, and routing it through the same hook
    is what keeps the caller's own usage accounting (which is what lands
    in story_usage) from silently under-reporting a long run by the cost
    of every compaction it needed. It reports under the current turn
    number, so an activity log shows two token entries for that turn.

    A failed summarization is non-fatal: the history is returned
    unchanged and the loop proceeds. The next turn will simply be large
    (and may hit the provider's context limit, which surfaces as a normal
    provider error) rather than the run dying because a *housekeeping*
    call failed.
    """
    to_summarize, to_keep = _split_for_compaction(history, config.compaction_keep_recent)
    if not to_summarize:
        return history, Usage(), 0

    transcript_parts: list[str] = []
    for message in to_summarize:
        for block in message.content:
            if isinstance(block, TextBlock):
                transcript_parts.append(f"[{message.role}] {block.text}")
            elif isinstance(block, ToolCallBlock):
                transcript_parts.append(f"[{message.role} tool_call] {block.name}({json.dumps(block.arguments, default=str)[:300]})")
            elif isinstance(block, ToolResultBlock):
                transcript_parts.append(f"[tool_result] {block.content[:500]}")
            elif isinstance(block, ImageBlock):
                # An image can't survive text summarization. Leave a
                # marker so the model knows one *was* here rather than
                # silently losing it (e.g. the planner's JIRA screenshot).
                transcript_parts.append(f"[{message.role} image dropped in compaction: {block.media_type}]")
    transcript = "\n".join(transcript_parts)

    summary_request = LlmRequest(
        model=config.model,
        system=(
            "Summarize the following agent transcript concisely but completely. "
            "Preserve every concrete fact that later work depends on: file paths "
            "touched, decisions made, values/APIs discovered, and any errors "
            "encountered and how they were resolved. Drop tool-call plumbing and "
            "restated context. Plain prose, no preamble."
        ),
        messages=[text_message("user", transcript)],
    )
    response, error, api_duration_ms = await _complete_with_retry(config, summary_request)
    if response is None:
        logger.warning("Compaction call failed (%s) — proceeding with the uncompacted history", error)
        return history, Usage(), api_duration_ms

    if config.on_turn:
        config.on_turn(turn, response)
    summary_text = response.text() or "(summary generation produced no text)"
    logger.info("Compacted %d message(s) into a summary (%d chars -> %d chars)", len(to_summarize), len(transcript), len(summary_text))

    summary_message = text_message(
        "user", f"Summary of earlier conversation (compacted to save context):\n{summary_text}"
    )
    return [summary_message, *to_keep], response.usage, api_duration_ms


async def run_agent_loop(config: LoopConfig, initial_messages: list[Message]) -> LoopResult:
    history: list[Message] = list(initial_messages)
    usage_total = Usage()
    budget_spent = 0.0
    api_duration_ms = 0
    turn = 0
    step = 0
    # Only forge compacts, and only when the provider doesn't do it
    # server-side — otherwise a provider that compacts for us would get
    # both, summarizing an already-summarized history.
    own_compaction = not config.provider.capabilities.supports_server_compaction

    while True:
        if own_compaction and _estimate_message_chars(history) > config.compaction_token_threshold * _CHARS_PER_TOKEN_ESTIMATE:
            history, compaction_usage, compaction_api_ms = await _compact_history(config, history, turn)
            usage_total = usage_total + compaction_usage
            api_duration_ms += compaction_api_ms

        request = LlmRequest(
            model=config.model,
            # A snapshot copy, not `history` itself: `history` is mutated
            # (assistant/tool messages appended) immediately after this
            # request is built, and LlmRequest is otherwise frozen — a
            # caller holding onto a returned/logged request object should
            # see exactly what was sent, not a view that silently changes
            # underneath it once the loop moves on.
            messages=list(history),
            system=config.system,
            tools=config.tools,
            response_format=config.response_format,
            effort=config.effort,
        )

        response, last_error, turn_api_ms = await _complete_with_retry(config, request)
        api_duration_ms += turn_api_ms

        if response is None:
            return LoopResult(
                history=history, usage=usage_total, terminal_reason=TERMINAL_ERROR, is_error=True,
                turns_used=turn, api_duration_ms=api_duration_ms,
                error_message=str(last_error) if last_error else "unknown provider error",
            )

        turn += 1
        if config.on_turn:
            config.on_turn(turn, response)
        usage_total = usage_total + response.usage
        cost = estimate_cost_usd(
            config.provider.name, response.model,
            input_tokens=response.usage.input_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            output_tokens=response.usage.output_tokens,
        )
        if cost is not None:
            budget_spent += cost

        assistant_message = Message(role="assistant", content=response.content)
        history.append(assistant_message)

        if config.on_block:
            for block in response.content:
                block_type = {
                    TextBlock: "text", ThinkingBlock: "thinking", ToolCallBlock: "tool_use",
                }.get(type(block), "unknown")
                config.on_block(block_type, block, step + 1)

        # Before anything acts on this turn — see _UNUSABLE_STOP_REASONS.
        if response.stop_reason in _UNUSABLE_STOP_REASONS:
            detail = _UNUSABLE_STOP_REASONS[response.stop_reason]
            logger.warning("Loop ending on unusable stop_reason=%s: %s", response.stop_reason, detail)
            return LoopResult(
                history=history, usage=usage_total, terminal_reason=response.stop_reason, is_error=True,
                turns_used=turn, api_duration_ms=api_duration_ms, final_text=response.text(),
                error_message=f"Provider stopped with {response.stop_reason!r}: {detail}.",
            )

        tool_calls = response.tool_calls()
        if not tool_calls:
            return LoopResult(
                history=history, usage=usage_total, terminal_reason=TERMINAL_END_TURN, is_error=False,
                turns_used=turn, api_duration_ms=api_duration_ms,
                structured_output=response.structured_output, final_text=response.text(),
            )

        tool_results: list[ContentBlock] = []
        for call in tool_calls:
            step += 1
            result_text, is_error = await _call_tool(config.tool_fns, call.name, call.arguments)
            result_block = ToolResultBlock(tool_call_id=call.id, content=result_text, is_error=is_error)
            tool_results.append(result_block)
            if config.on_block:
                config.on_block("tool_result", result_block, step)
        history.append(Message(role="tool", content=tool_results))

        if config.max_turns and turn >= config.max_turns:
            return LoopResult(
                history=history, usage=usage_total, terminal_reason=TERMINAL_MAX_TURNS, is_error=False,
                turns_used=turn, api_duration_ms=api_duration_ms, final_text=response.text(),
            )
        if config.max_budget_usd and budget_spent >= config.max_budget_usd:
            return LoopResult(
                history=history, usage=usage_total, terminal_reason=TERMINAL_MAX_BUDGET, is_error=False,
                turns_used=turn, api_duration_ms=api_duration_ms, final_text=response.text(),
            )
