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
deliberately emits the same terminal_reason vocabulary for), and its own
history compaction.

Everything here is written to be model-agnostic — the same loop drives
GPT, DeepSeek, or a local vLLM/Ollama endpoint through forge/llm/'s
OpenAI-compatible adapter — and four mechanisms exist specifically
because weaker or cheaper models need more scaffolding than a frontier
model does:

1. **Cache-aware compaction by tombstoning** (`_compact_history`). The
   old compaction summarized the older half of the history with an extra
   LLM call and spliced the summary in as a new first message. That
   rewrote the prompt prefix on every compaction, which destroys prefix
   caching — the single biggest cost lever on a long run — and cost a
   billable call each time. Now compaction never removes or reorders a
   message: it walks turns older than the last `compaction_keep_recent_turns`
   and replaces each *large* ToolResultBlock's content with a fixed
   tombstone string, in place. Roles and tool_call_ids are preserved, so
   the OpenAI-compatible message structure stays valid, and because a
   tombstoned block never changes again the prefix stays byte-stable
   across turns. No provider call, no failure mode.

2. **Parallel dispatch of read-only tools** (`_dispatch_tool_calls`). A
   turn that reads five files used to pay five sequential thread hops. A
   maximal run of consecutive read-only calls (LoopConfig.read_only_tools)
   now runs under asyncio.gather; anything not declared read-only runs
   strictly sequentially, in call order, and never overlaps a read — so
   two edits to one file, or an edit racing a read of the same file, are
   impossible by construction.

3. **Repeat-call guard**. Non-frontier models get stuck re-issuing the
   exact same tool call turn after turn. Each executed call is hashed on
   (name, arguments); an identical call in the *consecutive* turn (or a
   duplicate within one turn) is intercepted before execution and answered
   with an is_error tool_result carrying REPEAT_CALL_NUDGE instead. The
   guard resets whenever a non-read-only tool runs — after an edit, an
   identical analyze call is a legitimate re-check, not a loop. Only
   consecutive repeats are caught on purpose: compaction keeps the most
   recent turns intact, so a consecutive repeat's original output is
   always still in the history, whereas an older re-read may be answering
   a tombstone and must be allowed.

4. **Idle-turn budget** (LoopConfig.max_idle_turns / progress_tools). A
   run that has gone `max_idle_turns` consecutive tool-calling turns
   without executing any file-modifying tool ends early with
   TERMINAL_NO_PROGRESS — a soft stop, like a turn cap, since whatever was
   done so far is real. Opt-in per role (the planner never modifies files,
   so it leaves both unset). In the last IDLE_WARNING_WINDOW turns before
   the stop the trailing message also carries a warning saying how many are
   left, so the cut-off is never a surprise.

5. **A trailing reminder** (LoopConfig.trailing_reminder). A callable
   re-evaluated every turn whose text is appended to the *request* as a
   final user message and deliberately NOT stored in `history`. That
   distinction is the whole trick: a rule stated once in the system prompt
   stops being load-bearing forty turns later, but appending a reminder
   *into* the history would both bloat it with stale copies and, worse,
   change bytes that the prefix cache depends on. Kept out of history, it
   sits after every cached byte, so it costs one short message per turn and
   nothing else. forge/agents/roles/dev_loop.py uses it to re-show the dev
   agent its own progress checklist, which is what turns the idle-turn
   budget above from a silent stop into a correction the model can act on.

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
import hashlib
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
)

logger = logging.getLogger("forge.agents.loop")

# Soft-stop terminal reasons — a cap ended the run, but real work
# happened, so the caller proceeds to verification/approval rather than
# treating this as a failure. Kept as a small, stable vocabulary
# (forge/agents/roles/dev_loop.py:classify_loop_result checks membership
# in exactly this set) so callers never need to know the loop's internals.
TERMINAL_MAX_TURNS = "max_turns"
TERMINAL_MAX_BUDGET = "max_budget_usd"
TERMINAL_NO_PROGRESS = "no_progress"
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
_DEFAULT_COMPACTION_KEEP_RECENT_TURNS = 4

# What a pruned tool result is replaced with. Fixed text on purpose: a
# tombstone that never varies is what keeps the compacted prefix
# byte-identical from one turn to the next (see module docstring, point 1).
#
# The wording is actionable rather than merely descriptive, which is
# borrowed from Aster's context-eviction stub
# (github.com/Zfinix/aster, Apache-2.0). "This is gone" invites the model
# to re-read the whole file and evict itself again next turn; naming the
# narrower re-read is what stops that loop.
TOMBSTONE = (
    "[Output dropped to save context. Re-run this tool only if you still need it, "
    "and ask for the specific range or pattern you are missing rather than the whole file. "
    "Your applied changes are unaffected.]"
)

# Tool results at or below this size are left alone by compaction. They
# are the cheap, load-bearing ones ("Successfully wrote file to x",
# "Applied 3/3 edit(s).", an analyzer's "passed": true) — replacing a
# 40-char confirmation with a 75-char tombstone saves nothing and loses
# the one line that tells the model which edits already landed.
_TOMBSTONE_MIN_CHARS = 200

REPEAT_CALL_NUDGE = (
    "Repeated tool call detected with identical parameters. Do not repeat this call; "
    "evaluate why the previous output was insufficient."
)

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
    # run made — narrower than the caller's own wall-clock for the phase,
    # which also covers local tool execution. Recorded as
    # story_usage.api_duration_ms so the UI can separate "slow model"
    # from "slow dart analyze".
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
    # Turns (assistant messages, counted from the end) whose tool results
    # compaction leaves untouched. Everything older is eligible for
    # tombstoning; the opening user message (the plan/brief) never is.
    compaction_keep_recent_turns: int = _DEFAULT_COMPACTION_KEEP_RECENT_TURNS
    # Tool names that neither write to the worktree nor run anything
    # that does. Consecutive calls to these run concurrently; any other
    # tool runs alone, in order. An undeclared tool is treated as
    # mutating — the safe default.
    read_only_tools: frozenset[str] = frozenset()
    # Tool names whose execution counts as "progress" for the idle-turn
    # budget — the file-modifying tools, for the dev role. Empty disables
    # the budget regardless of max_idle_turns.
    progress_tools: frozenset[str] = frozenset()
    # Tools that change no observable state: they mutate only the caller's
    # own in-memory bookkeeping, so an identical read issued after one is
    # still a repeat. They run alone like any non-read-only tool (two
    # concurrent updates could race on the same structure) but they do NOT
    # clear the repeat-call guard. Without this, a tool the dev prompt
    # tells the model to call constantly would switch the guard off.
    bookkeeping_tools: frozenset[str] = frozenset()
    # End the loop (TERMINAL_NO_PROGRESS) after this many consecutive
    # tool-calling turns that executed no progress_tools call. 0 = off.
    max_idle_turns: int = 0
    # Re-evaluated every turn; its text is appended to the request as a
    # final user message and never kept in `history`. Return "" to append
    # nothing this turn. See point 5 in the module docstring.
    trailing_reminder: Callable[[], str] | None = None
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
    raises."""
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


# --- compaction ----------------------------------------------------------


def _compaction_cut_index(history: list[Message], keep_recent_turns: int) -> int:
    """Index of the first message of the `keep_recent_turns`-th most recent
    assistant turn. Messages at indexes [1, cut) are old enough to prune;
    index 0 (the opening user message — the plan/brief every later turn
    refers back to) is never pruned, whatever the count. With fewer than
    `keep_recent_turns` assistant turns in the history nothing is old
    enough, so the cut is 1 (an empty prune range)."""
    seen = 0
    for idx in range(len(history) - 1, 0, -1):
        if history[idx].role == "assistant":
            seen += 1
            if seen >= max(keep_recent_turns, 1):
                return idx
    return 1


def _compact_history(history: list[Message], keep_recent_turns: int) -> tuple[list[Message], int]:
    """Tombstone large tool results older than the last `keep_recent_turns`
    turns. Returns (new_history, number_of_blocks_tombstoned).

    Nothing is removed or reordered — every message keeps its role and
    every ToolResultBlock keeps its tool_call_id, so a provider that
    matches tool messages to prior tool_calls by id (all OpenAI-compatible
    ones) still sees a valid conversation. Idempotent: an already-
    tombstoned block, or one small enough to keep, is left exactly as it
    is, which is what makes the compacted prefix stable for caching."""
    cut = _compaction_cut_index(history, keep_recent_turns)
    compacted = list(history)
    pruned = 0
    for idx in range(1, cut):
        message = compacted[idx]
        if message.role != "tool":
            continue
        new_blocks: list[ContentBlock] = []
        changed = False
        for block in message.content:
            if (
                isinstance(block, ToolResultBlock)
                and len(block.content) > _TOMBSTONE_MIN_CHARS
                and block.content != TOMBSTONE
            ):
                new_blocks.append(ToolResultBlock(tool_call_id=block.tool_call_id, content=TOMBSTONE, is_error=block.is_error))
                changed = True
                pruned += 1
            else:
                new_blocks.append(block)
        if changed:
            compacted[idx] = Message(role=message.role, content=new_blocks)
    return compacted, pruned


# --- tool dispatch -------------------------------------------------------


def call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Stable hash of one tool call — the key the repeat-call guard
    compares. Argument order is normalized so `{a, b}` and `{b, a}` (which
    a model does emit interchangeably) count as the same call."""
    payload = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class _DispatchOutcome:
    results: list[ToolResultBlock]
    # Signatures of calls issued since the last state-changing call — what
    # the *next* turn's calls are checked against.
    live_signatures: set[str]
    progressed: bool
    intercepted: int


async def _dispatch_tool_calls(config: LoopConfig, tool_calls: list[ToolCallBlock], previous_signatures: set[str]) -> _DispatchOutcome:
    """Run one turn's tool calls: read-only runs concurrently, everything
    else sequentially in call order, repeats intercepted (see module
    docstring, points 2 and 3). Results come back in call order."""
    results: list[ToolResultBlock | None] = [None] * len(tool_calls)
    pending_reads: list[int] = []
    live_signatures = set(previous_signatures)
    progressed = False
    intercepted = 0

    async def _flush_reads() -> None:
        if not pending_reads:
            return
        outcomes = await asyncio.gather(
            *(_call_tool(config.tool_fns, tool_calls[i].name, tool_calls[i].arguments) for i in pending_reads)
        )
        for idx, (text, is_error) in zip(pending_reads, outcomes):
            results[idx] = ToolResultBlock(tool_call_id=tool_calls[idx].id, content=text, is_error=is_error)
        pending_reads.clear()

    for idx, call in enumerate(tool_calls):
        signature = call_signature(call.name, call.arguments)
        if signature in live_signatures:
            logger.warning("Repeated tool call intercepted: %s args=%s", call.name, json.dumps(call.arguments, default=str)[:200])
            results[idx] = ToolResultBlock(tool_call_id=call.id, content=REPEAT_CALL_NUDGE, is_error=True)
            intercepted += 1
            continue
        live_signatures.add(signature)

        if call.name in config.read_only_tools:
            pending_reads.append(idx)
            continue

        # A state-changing call: drain any reads queued before it first,
        # so it never overlaps them, then run it alone.
        await _flush_reads()
        text, is_error = await _call_tool(config.tool_fns, call.name, call.arguments)
        results[idx] = ToolResultBlock(tool_call_id=call.id, content=text, is_error=is_error)
        if call.name not in config.bookkeeping_tools:
            # The worktree may have changed: an identical read/analyze after
            # this point is a fresh observation, not a repeat. Only this call
            # itself stays live, so an immediately re-issued identical edit is
            # still caught. A bookkeeping call changes nothing on disk, so it
            # leaves the guard's memory alone.
            live_signatures = {signature}
        if call.name in config.progress_tools:
            progressed = True

    await _flush_reads()
    return _DispatchOutcome(
        results=[r for r in results if r is not None],
        live_signatures=live_signatures,
        progressed=progressed,
        intercepted=intercepted,
    )


# --- the loop ------------------------------------------------------------


# How many turns before the idle budget runs out the model starts being told.
IDLE_WARNING_WINDOW = 2


def _idle_warning(idle_turns: int, config: LoopConfig) -> str:
    """A nudge for a run that is close to being stopped for not writing.

    The idle-turn budget used to end a run silently: the model, mid-way
    through reading a large codebase, was simply cut off, and never had the
    chance to act on it. In the last IDLE_WARNING_WINDOW turns before the stop
    this says how many are left and what counts, so a run that has explored
    enough can start writing instead. Empty when the budget is off, when the
    last turn made progress, or while there is still plenty of room."""
    if not (config.max_idle_turns and config.progress_tools) or idle_turns <= 0:
        return ""
    remaining = config.max_idle_turns - idle_turns
    if remaining > IDLE_WARNING_WINDOW:
        return ""
    writers = ", ".join(sorted(config.progress_tools))
    return (
        f"IDLE WARNING: {idle_turns} turn(s) in a row without writing a file — reading and searching do not count. "
        f"The run stops after {remaining} more such turn(s). You have explored enough: write the planned files now "
        f"({writers}), and go back to reading only when a specific edit needs it."
    )


async def run_agent_loop(config: LoopConfig, initial_messages: list[Message]) -> LoopResult:
    history: list[Message] = list(initial_messages)
    usage_total = Usage()
    budget_spent = 0.0
    api_duration_ms = 0
    turn = 0
    step = 0
    idle_turns = 0
    # Signatures of the previous turn's tool calls (since its last
    # state-changing call) — see _dispatch_tool_calls. Reset each turn.
    previous_signatures: set[str] = set()
    # Only forge compacts, and only when the provider doesn't do it
    # server-side — otherwise a provider that compacts for us would get
    # both.
    own_compaction = not config.provider.capabilities.supports_server_compaction
    compaction_char_threshold = config.compaction_token_threshold * _CHARS_PER_TOKEN_ESTIMATE

    while True:
        if own_compaction and _estimate_message_chars(history) > compaction_char_threshold:
            history, pruned = _compact_history(history, config.compaction_keep_recent_turns)
            if pruned:
                logger.info("Compacted history: tombstoned %d tool result(s) older than the last %d turn(s)", pruned, config.compaction_keep_recent_turns)

        # Appended to the request only. Everything before it is unchanged
        # from last turn, so the cached prefix survives.
        turn_messages = list(history)
        reminder = ""
        if config.trailing_reminder:
            try:
                reminder = config.trailing_reminder()
            except Exception:  # noqa: BLE001 — a caller's reminder is never worth the run
                # Same reasoning as _call_tool's catch-all: an exception from
                # caller-supplied code escaping here would leave run_dev,
                # bypass dev_node (which catches only DevLoopError) and reach
                # the daemon's poll loop, whose only handler abandons every
                # remaining epic for the cycle.
                logger.warning("trailing_reminder raised — continuing without it this turn", exc_info=True)
                reminder = ""
        # Rides in the same trailing message, so it is just as cheap and just
        # as absent from history — see _idle_warning.
        warning = _idle_warning(idle_turns, config)
        if warning:
            reminder = f"{reminder}\n\n{warning}" if reminder else warning
        if reminder:
            turn_messages.append(Message(role="user", content=[TextBlock(text=reminder)]))

        request = LlmRequest(
            model=config.model,
            # A snapshot copy, not `history` itself: `history` is mutated
            # (assistant/tool messages appended) immediately after this
            # request is built, and LlmRequest is otherwise frozen — a
            # caller holding onto a returned/logged request object should
            # see exactly what was sent, not a view that silently changes
            # underneath it once the loop moves on.
            messages=turn_messages,
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

        outcome = await _dispatch_tool_calls(config, tool_calls, previous_signatures)
        previous_signatures = outcome.live_signatures
        for result_block in outcome.results:
            step += 1
            if config.on_block:
                config.on_block("tool_result", result_block, step)
        history.append(Message(role="tool", content=list(outcome.results)))

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

        idle_turns = 0 if outcome.progressed else idle_turns + 1
        if config.max_idle_turns and config.progress_tools and idle_turns >= config.max_idle_turns:
            logger.warning(
                "Loop ending: %d consecutive turn(s) without a file-modifying tool call (last turn: %d call(s), %d intercepted as repeats)",
                idle_turns, len(tool_calls), outcome.intercepted,
            )
            return LoopResult(
                history=history, usage=usage_total, terminal_reason=TERMINAL_NO_PROGRESS, is_error=False,
                turns_used=turn, api_duration_ms=api_duration_ms, final_text=response.text(),
                error_message=f"No file-modifying tool call in the last {idle_turns} turn(s).",
            )
