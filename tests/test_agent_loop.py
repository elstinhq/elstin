"""elstin/agents/loop.py — the shared agentic loop every LLM-driven role
(dev, planner) runs through. Exercised entirely against a fake
LlmProvider — no network, no real provider SDK — since the loop only ever
touches elstin/llm/types.py's normalized vocabulary and elstin/llm/base.py's
Protocol, never a concrete adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from elstin.agents.loop import (
    ARGUMENT_TOMBSTONE,
    REMINDER_TOMBSTONE,
    TERMINAL_END_TURN,
    TERMINAL_ERROR,
    TERMINAL_MAX_BUDGET,
    TERMINAL_MAX_TURNS,
    LoopConfig,
    run_agent_loop,
)
from elstin.llm.base import Capabilities
from elstin.llm.errors import LlmContextOverflowError, LlmRateLimitError
from elstin.llm.types import (
    LlmRequest,
    LlmResponse,
    TextBlock,
    ToolCallBlock,
    ToolDef,
    ToolResultBlock,
    Usage,
    text_message,
)


@dataclass
class FakeProvider:
    """A scripted LlmProvider: each call to `complete` pops the next
    canned response (or raises the next canned exception) off `script`."""

    script: list[LlmResponse | Exception]
    name: str = "fake"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list[LlmRequest] = field(default_factory=list)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text_response(text: str, *, model="fake-model") -> LlmResponse:
    return LlmResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model=model)


def _tool_call_response(name: str, arguments: dict, call_id: str = "call_1", *, model="fake-model") -> LlmResponse:
    return LlmResponse(
        content=[ToolCallBlock(id=call_id, name=name, arguments=arguments)],
        stop_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=5), model=model,
    )


def _config(provider, **overrides) -> LoopConfig:
    defaults = dict(provider=provider, model="fake-model", system="be helpful")
    defaults.update(overrides)
    return LoopConfig(**defaults)


async def test_single_turn_end_turn():
    provider = FakeProvider(script=[_text_response("all done")])
    result = await run_agent_loop(_config(provider), [text_message("user", "do the thing")])

    assert result.terminal_reason == TERMINAL_END_TURN
    assert result.is_error is False
    assert result.final_text == "all done"
    assert result.usage.input_tokens == 10
    assert result.turns_used == 1


async def test_tool_call_then_end_turn_dispatches_tool_and_continues():
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a.py"}),
        _text_response("read the file, done"),
    ])
    calls = []

    def fake_read_file_tool(path: str) -> str:
        calls.append(path)
        return "file contents"

    result = await run_agent_loop(
        _config(provider, tools=[ToolDef(name="read_file_tool", description="", parameters={})], tool_fns={"read_file_tool": fake_read_file_tool}),
        [text_message("user", "read a.py")],
    )

    assert calls == ["a.py"]
    assert result.terminal_reason == TERMINAL_END_TURN
    assert result.final_text == "read the file, done"
    # history: user, assistant(tool_call), tool(result), assistant(final)
    assert result.history[2].role == "tool"
    tool_result = result.history[2].content[0]
    assert isinstance(tool_result, ToolResultBlock)
    assert tool_result.content == "file contents"
    assert tool_result.is_error is False


async def test_unknown_tool_call_becomes_error_tool_result_not_a_crash():
    provider = FakeProvider(script=[
        _tool_call_response("nonexistent_tool", {}),
        _text_response("recovered"),
    ])
    result = await run_agent_loop(_config(provider, tool_fns={}), [text_message("user", "go")])

    tool_result = result.history[2].content[0]
    assert tool_result.is_error is True
    assert "Unknown tool" in tool_result.content
    assert result.terminal_reason == TERMINAL_END_TURN


async def test_tool_raising_becomes_error_tool_result():
    provider = FakeProvider(script=[
        _tool_call_response("boom_tool", {}),
        _text_response("recovered"),
    ])

    def boom_tool() -> str:
        raise RuntimeError("disk on fire")

    result = await run_agent_loop(_config(provider, tool_fns={"boom_tool": boom_tool}), [text_message("user", "go")])

    tool_result = result.history[2].content[0]
    assert tool_result.is_error is True
    assert "disk on fire" in tool_result.content


async def test_tool_call_wrong_arguments_becomes_error_tool_result():
    provider = FakeProvider(script=[
        _tool_call_response("needs_arg_tool", {"wrong_kwarg": 1}),
        _text_response("recovered"),
    ])

    def needs_arg_tool(path: str) -> str:
        return path

    result = await run_agent_loop(_config(provider, tool_fns={"needs_arg_tool": needs_arg_tool}), [text_message("user", "go")])

    tool_result = result.history[2].content[0]
    assert tool_result.is_error is True
    assert "Invalid arguments" in tool_result.content


async def test_parallel_tool_calls_bundled_into_one_tool_message():
    response = LlmResponse(
        content=[
            ToolCallBlock(id="c1", name="t", arguments={"x": 1}),
            ToolCallBlock(id="c2", name="t", arguments={"x": 2}),
        ],
        stop_reason="tool_calls", usage=Usage(), model="fake-model",
    )
    provider = FakeProvider(script=[response, _text_response("done")])

    def t(x: int) -> str:
        return f"got {x}"

    result = await run_agent_loop(_config(provider, tool_fns={"t": t}), [text_message("user", "go")])

    tool_message = result.history[2]
    assert tool_message.role == "tool"
    assert len(tool_message.content) == 2
    assert {b.tool_call_id for b in tool_message.content} == {"c1", "c2"}


async def test_max_turns_reached_is_a_soft_stop_not_an_error():
    provider = FakeProvider(script=[
        _tool_call_response("t", {}),
        _tool_call_response("t", {}),
    ])
    result = await run_agent_loop(
        _config(provider, max_turns=1, tool_fns={"t": lambda: "ok"}), [text_message("user", "go")]
    )

    assert result.terminal_reason == TERMINAL_MAX_TURNS
    assert result.is_error is False
    assert result.turns_used == 1


async def test_max_budget_reached_is_a_soft_stop():
    provider = FakeProvider(name="openai", script=[
        _tool_call_response("t", {}, model="gpt-5.1"),
        _tool_call_response("t", {}, model="gpt-5.1"),
    ])
    # gpt-5.1 costs $1.25/mtok input in elstin/llm/pricing.py; a single
    # 10-input-token call is far too cheap to hit a real budget, so use an
    # artificially tiny cap to force the trip on the first turn.
    result = await run_agent_loop(
        _config(provider, max_budget_usd=0.0000001, tool_fns={"t": lambda: "ok"}), [text_message("user", "go")]
    )

    assert result.terminal_reason == TERMINAL_MAX_BUDGET
    assert result.is_error is False


async def test_transient_error_is_retried_then_succeeds(monkeypatch):
    import elstin.agents.loop as loop_mod

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmRateLimitError("slow down"), _text_response("ok now")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is False
    assert result.final_text == "ok now"
    assert len(provider.calls) == 2


async def test_non_retryable_error_ends_loop_as_error(monkeypatch):
    import elstin.agents.loop as loop_mod
    from elstin.llm.errors import LlmBadRequestError

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmBadRequestError("bad schema")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert result.terminal_reason == "error"
    assert "bad schema" in result.error_message


async def test_retries_exhausted_ends_loop_as_error(monkeypatch):
    import elstin.agents.loop as loop_mod

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmRateLimitError("1"), LlmRateLimitError("2"), LlmRateLimitError("3")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert len(provider.calls) == 3


async def test_a_timed_out_call_is_retried_once_and_the_hook_says_so(monkeypatch):
    import elstin.agents.loop as loop_mod
    from elstin.llm.errors import LlmTimeoutError

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmTimeoutError("no response within 900s"), _text_response("ok now")])
    seen: list[tuple[int, str]] = []

    result = await run_agent_loop(
        _config(provider, on_retry=lambda attempt, error, delay: seen.append((attempt, str(error)))),
        [text_message("user", "go")],
    )

    assert result.is_error is False and result.final_text == "ok now"
    assert seen == [(1, "no response within 900s")]


async def test_a_second_timeout_ends_the_turn_instead_of_waiting_out_a_third(monkeypatch):
    """A provider that stalled once tends to stall again, and each attempt
    costs the whole deadline: two tries, not three."""
    import elstin.agents.loop as loop_mod
    from elstin.llm.errors import LlmTimeoutError

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmTimeoutError("t1"), LlmTimeoutError("t2"), _text_response("never reached")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True and "t2" in result.error_message
    assert len(provider.calls) == 2


async def test_a_timeout_does_not_use_up_the_retries_other_errors_are_owed(monkeypatch):
    import elstin.agents.loop as loop_mod
    from elstin.llm.errors import LlmTimeoutError

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmTimeoutError("t"), LlmRateLimitError("429"), _text_response("made it")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.final_text == "made it" and len(provider.calls) == 3


async def test_a_failing_retry_hook_never_costs_the_run(monkeypatch):
    import elstin.agents.loop as loop_mod

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmRateLimitError("429"), _text_response("fine")])

    def boom(*args):
        raise RuntimeError("hook bug")

    result = await run_agent_loop(_config(provider, on_retry=boom), [text_message("user", "go")])

    assert result.final_text == "fine"


async def test_structured_output_surfaced_on_natural_end():
    response = LlmResponse(
        content=[TextBlock(text='{"a": 1}')], stop_reason="end_turn", usage=Usage(),
        model="fake-model", structured_output={"a": 1},
    )
    provider = FakeProvider(script=[response])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.structured_output == {"a": 1}


async def test_on_block_callback_invoked_for_each_block():
    provider = FakeProvider(script=[
        _tool_call_response("t", {}),
        _text_response("done"),
    ])
    seen = []

    await run_agent_loop(
        _config(provider, tool_fns={"t": lambda: "ok"}, on_block=lambda kind, block, step: seen.append((kind, step))),
        [text_message("user", "go")],
    )

    assert ("tool_use", 1) in seen
    assert ("tool_result", 1) in seen
    assert any(kind == "text" for kind, _ in seen)


async def test_on_turn_callback_invoked_once_per_llm_call():
    provider = FakeProvider(script=[
        _tool_call_response("t", {}),
        _text_response("done"),
    ])
    turns_seen = []

    result = await run_agent_loop(
        _config(provider, tool_fns={"t": lambda: "ok"}, on_turn=lambda turn, response: turns_seen.append(turn)),
        [text_message("user", "go")],
    )

    assert turns_seen == [1, 2]
    assert result.turns_used == 2


async def test_truncated_turn_is_a_hard_failure_not_a_clean_finish():
    """stop_reason="max_tokens" means the provider cut the turn off at its
    own output cap. Reporting that as a normal end_turn made a truncated
    response indistinguishable from "the model decided nothing needed
    doing" — for the dev role, a silent pass straight through to
    verification."""
    truncated = LlmResponse(
        content=[TextBlock(text="I'll start by writing the fi")], stop_reason="max_tokens",
        usage=Usage(input_tokens=10, output_tokens=5), model="fake-model",
    )
    provider = FakeProvider(script=[truncated])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert result.terminal_reason == "max_tokens"
    assert "truncated" in result.error_message


async def test_truncated_tool_call_is_never_dispatched():
    """The dangerous half of the same bug: a turn cut off at the output cap
    can carry a half-serialized tool call. Dispatching it writes a
    partial file into the worktree, which is worse than failing the run —
    so the stop_reason check has to happen BEFORE tool dispatch."""
    truncated_write = LlmResponse(
        content=[ToolCallBlock(id="c1", name="write_file_tool", arguments={"path": "a.py", "content": "def f():\n    retur"})],
        stop_reason="max_tokens", usage=Usage(), model="fake-model",
    )
    provider = FakeProvider(script=[truncated_write])
    written: list[str] = []

    def write_file_tool(path: str, content: str) -> str:
        written.append(path)
        return "ok"

    result = await run_agent_loop(
        _config(provider, tools=[ToolDef(name="write_file_tool", description="", parameters={})], tool_fns={"write_file_tool": write_file_tool}),
        [text_message("user", "go")],
    )

    assert written == []
    assert result.is_error is True


async def test_content_filter_is_a_hard_failure():
    """A provider refusal arrives as a normal 200 with
    stop_reason="content_filter" and usually empty text — which reads as
    a successful no-op run unless it's checked explicitly."""
    refused = LlmResponse(content=[], stop_reason="content_filter", usage=Usage(), model="fake-model")
    provider = FakeProvider(script=[refused])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert result.terminal_reason == "content_filter"


async def test_api_duration_is_measured():
    provider = FakeProvider(script=[_text_response("done")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.api_duration_ms >= 0
    assert isinstance(result.api_duration_ms, int)


# --- compaction: cache-aware tombstoning ---------------------------------


def _turn(call_id: str, result: str, *, name: str = "read_file_tool", args: dict | None = None):
    """One assistant tool-call turn plus its answering tool message."""
    from elstin.llm.types import Message

    return [
        Message(role="assistant", content=[ToolCallBlock(id=call_id, name=name, arguments=args or {"path": call_id})]),
        Message(role="tool", content=[ToolResultBlock(tool_call_id=call_id, content=result)]),
    ]


def test_compaction_tombstones_old_large_tool_results_and_keeps_recent_turns():
    """No LLM call, no message removed: results older than the last N
    turns are replaced in place with a fixed tombstone; the last N turns
    and the opening plan message are untouched."""
    from elstin.agents.loop import TOMBSTONE, _compact_history

    big = "x" * 5_000
    history = [text_message("user", "the plan")]
    for i in range(6):
        history += _turn(f"c{i}", big)

    compacted, pruned = _compact_history(history, keep_recent_turns=4)

    assert pruned == 2
    assert len(compacted) == len(history)
    assert [m.role for m in compacted] == [m.role for m in history]
    assert compacted[0] == history[0]  # the plan survives verbatim
    # turns 0 and 1 (messages 1-4) are tombstoned; turns 2-5 intact
    assert compacted[2].content[0].content == TOMBSTONE
    assert compacted[4].content[0].content == TOMBSTONE
    assert compacted[6].content[0].content == big
    assert compacted[12].content[0].content == big
    # tool_call_ids are preserved so the OpenAI tool-message pairing stays valid
    assert compacted[2].content[0].tool_call_id == "c0"
    assert compacted[1] == history[1]  # the assistant's tool_calls message itself is untouched


def test_compaction_leaves_small_results_alone_and_is_idempotent():
    """Short confirmations ("wrote file x") are the load-bearing ones and
    cost less than the tombstone; and a second pass must not change a
    byte, or the cached prefix would churn every turn."""
    from elstin.agents.loop import _compact_history

    history = [text_message("user", "plan")]
    history += _turn("c0", "Successfully wrote file to a.py.", name="write_file_tool")
    history += _turn("c1", "y" * 5_000)
    for i in range(2, 7):
        history += _turn(f"c{i}", "z" * 5_000)

    once, pruned_once = _compact_history(history, keep_recent_turns=4)
    twice, pruned_twice = _compact_history(once, keep_recent_turns=4)

    assert pruned_once == 2  # c1 and c2; c0 is small and kept
    assert once[2].content[0].content == "Successfully wrote file to a.py."
    assert pruned_twice == 0
    assert twice == once


def test_compaction_with_fewer_turns_than_keep_recent_is_a_noop():
    from elstin.agents.loop import _compact_history

    history = [text_message("user", "plan")] + _turn("c0", "x" * 5_000) + _turn("c1", "x" * 5_000)

    compacted, pruned = _compact_history(history, keep_recent_turns=4)

    assert pruned == 0
    assert compacted == history


async def test_compaction_makes_no_provider_call():
    """The old compaction summarized via an extra LLM call; the new one
    must not — one provider call per turn, full stop."""
    provider = FakeProvider(script=[_text_response("done")])
    big_history = [text_message("user", "plan")]
    for i in range(6):
        big_history += _turn(f"c{i}", "x" * 5_000)

    result = await run_agent_loop(_config(provider, compaction_token_threshold=1, compaction_keep_recent_turns=2), big_history)

    assert len(provider.calls) == 1
    assert result.is_error is False
    sent = provider.calls[0].messages
    from elstin.agents.loop import TOMBSTONE

    assert sent[2].content[0].content == TOMBSTONE  # old result pruned in what was sent
    assert sent[-1].content[0].content == "x" * 5_000  # recent turn intact


async def test_server_side_compaction_suppresses_our_own():
    """A provider that compacts server-side must not also get elstin's
    tombstoning on top."""
    from elstin.agents.loop import TOMBSTONE

    provider = FakeProvider(
        script=[_text_response("done")],
        capabilities=Capabilities(supports_server_compaction=True),
    )
    big_history = [text_message("user", "plan")]
    for i in range(6):
        big_history += _turn(f"c{i}", "x" * 5_000)

    result = await run_agent_loop(_config(provider, compaction_token_threshold=1, compaction_keep_recent_turns=2), big_history)

    assert result.is_error is False
    assert not any(
        isinstance(b, ToolResultBlock) and b.content == TOMBSTONE
        for m in provider.calls[0].messages for b in m.content
    )


async def test_size_estimate_counts_image_payloads():
    """A planner run with a JIRA screenshot attached carries megabytes in
    one ImageBlock. Ignoring that made the compaction trigger report ~0
    for the single largest thing in the request."""
    from elstin.agents.loop import _estimate_message_chars
    from elstin.llm.types import ImageBlock, Message

    with_image = [Message(role="user", content=[ImageBlock(media_type="image/png", data="A" * 10_000)])]

    assert _estimate_message_chars(with_image) == 10_000


# --- parallel dispatch ---------------------------------------------------


def _multi_call_response(*calls: tuple[str, str, dict]) -> LlmResponse:
    return LlmResponse(
        content=[ToolCallBlock(id=cid, name=name, arguments=args) for cid, name, args in calls],
        stop_reason="tool_calls", usage=Usage(), model="fake-model",
    )


async def test_read_only_tools_in_one_turn_run_concurrently():
    """Two read-only calls each block on a barrier that only releases once
    BOTH are inside the tool — sequential dispatch would never get there
    (the first call would time out on the barrier and come back as an
    error)."""
    import threading

    barrier = threading.Barrier(2, timeout=5)

    def read_file_tool(path: str) -> str:
        barrier.wait()
        return f"contents of {path}"

    provider = FakeProvider(script=[
        _multi_call_response(("c1", "read_file_tool", {"path": "a"}), ("c2", "read_file_tool", {"path": "b"})),
        _text_response("done"),
    ])
    result = await run_agent_loop(
        _config(provider, tool_fns={"read_file_tool": read_file_tool}, read_only_tools=frozenset({"read_file_tool"})),
        [text_message("user", "go")],
    )

    tool_message = result.history[2]
    assert [b.tool_call_id for b in tool_message.content] == ["c1", "c2"]  # call order preserved
    assert [b.is_error for b in tool_message.content] == [False, False]
    assert tool_message.content[0].content == "contents of a"


async def test_mutating_tools_never_overlap_reads_or_each_other():
    """Anything not declared read-only runs alone, in order: an edit never
    starts while a read is in flight, and two edits never overlap."""
    import threading
    import time as _time

    active = {"count": 0, "max": 0}
    lock = threading.Lock()
    order: list[str] = []

    def _tracked(name: str):
        def fn(**kwargs) -> str:
            with lock:
                active["count"] += 1
                active["max"] = max(active["max"], active["count"])
            order.append(name)
            _time.sleep(0.02)
            with lock:
                active["count"] -= 1
            return "ok"
        return fn

    provider = FakeProvider(script=[
        _multi_call_response(
            ("c1", "read_file_tool", {"path": "a"}),
            ("c2", "edit_file_tool", {"path": "a"}),
            ("c3", "edit_file_tool", {"path": "b"}),
            ("c4", "read_file_tool", {"path": "b"}),
        ),
        _text_response("done"),
    ])
    result = await run_agent_loop(
        _config(
            provider,
            tool_fns={"read_file_tool": _tracked("read"), "edit_file_tool": _tracked("edit")},
            read_only_tools=frozenset({"read_file_tool"}),
        ),
        [text_message("user", "go")],
    )

    assert active["max"] == 1  # nothing ever ran concurrently with a mutating call
    assert order == ["read", "edit", "edit", "read"]
    assert [b.tool_call_id for b in result.history[2].content] == ["c1", "c2", "c3", "c4"]


# --- repeat-call guard ---------------------------------------------------


async def test_identical_call_in_consecutive_turn_is_intercepted_with_nudge():
    from elstin.agents.loop import REPEAT_CALL_NUDGE

    calls: list[str] = []

    def read_file_tool(path: str) -> str:
        calls.append(path)
        return "contents"

    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a.py"}, call_id="c1"),
        _tool_call_response("read_file_tool", {"path": "a.py"}, call_id="c2"),
        _text_response("ok I see"),
    ])
    result = await run_agent_loop(_config(provider, tool_fns={"read_file_tool": read_file_tool}), [text_message("user", "go")])

    assert calls == ["a.py"]  # executed once, not twice
    repeated = result.history[4].content[0]
    assert isinstance(repeated, ToolResultBlock)
    assert repeated.is_error is True
    assert repeated.content == REPEAT_CALL_NUDGE
    assert repeated.tool_call_id == "c2"


async def test_repeat_guard_normalizes_argument_order_and_catches_same_turn_duplicates():
    from elstin.agents.loop import REPEAT_CALL_NUDGE

    calls: list[dict] = []

    def t(**kwargs) -> str:
        calls.append(kwargs)
        return "ok"

    provider = FakeProvider(script=[
        _multi_call_response(("c1", "t", {"a": 1, "b": 2}), ("c2", "t", {"b": 2, "a": 1})),
        _text_response("done"),
    ])
    result = await run_agent_loop(_config(provider, tool_fns={"t": t}), [text_message("user", "go")])

    assert len(calls) == 1
    assert result.history[2].content[1].content == REPEAT_CALL_NUDGE


async def test_repeat_guard_allows_identical_call_after_a_mutating_call():
    """analyze -> edit -> analyze is the normal fix cycle: the second
    analyze is a fresh observation of a changed worktree, not a loop."""
    analyzed = {"n": 0}

    def analyze_tool() -> str:
        analyzed["n"] += 1
        return "1 error"

    provider = FakeProvider(script=[
        _tool_call_response("analyze_tool", {}, call_id="c1"),
        _tool_call_response("edit_file_tool", {"path": "a"}, call_id="c2"),
        _tool_call_response("analyze_tool", {}, call_id="c3"),
        _text_response("done"),
    ])
    result = await run_agent_loop(
        _config(provider, tool_fns={"analyze_tool": analyze_tool, "edit_file_tool": lambda path: "edited"}),
        [text_message("user", "go")],
    )

    assert analyzed["n"] == 2
    assert all(not b.is_error for m in result.history if m.role == "tool" for b in m.content)


async def test_repeat_guard_only_catches_consecutive_repeats():
    """A re-read two turns later (after something else) is allowed —
    compaction may have tombstoned the original result by then."""
    calls: list[str] = []

    def read_file_tool(path: str) -> str:
        calls.append(path)
        return "contents"

    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a"}, call_id="c1"),
        _tool_call_response("read_file_tool", {"path": "b"}, call_id="c2"),
        _tool_call_response("read_file_tool", {"path": "a"}, call_id="c3"),
        _text_response("done"),
    ])
    await run_agent_loop(_config(provider, tool_fns={"read_file_tool": read_file_tool}), [text_message("user", "go")])

    assert calls == ["a", "b", "a"]


# --- idle-turn budget ----------------------------------------------------


async def test_no_progress_for_max_idle_turns_is_a_soft_stop():
    from elstin.agents.loop import TERMINAL_NO_PROGRESS

    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a"}, call_id="c1"),
        _tool_call_response("read_file_tool", {"path": "b"}, call_id="c2"),
        _tool_call_response("read_file_tool", {"path": "c"}, call_id="c3"),
        _text_response("never reached"),
    ])
    result = await run_agent_loop(
        _config(
            provider, tool_fns={"read_file_tool": lambda path: "x"},
            progress_tools=frozenset({"edit_file_tool"}), max_idle_turns=2,
        ),
        [text_message("user", "go")],
    )

    assert result.terminal_reason == TERMINAL_NO_PROGRESS
    assert result.is_error is False
    assert result.turns_used == 2
    assert len(provider.calls) == 2


async def test_progress_tool_call_resets_the_idle_budget():
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a"}, call_id="c1"),
        _tool_call_response("edit_file_tool", {"path": "a"}, call_id="c2"),
        _tool_call_response("read_file_tool", {"path": "b"}, call_id="c3"),
        _text_response("done"),
    ])
    result = await run_agent_loop(
        _config(
            provider, tool_fns={"read_file_tool": lambda path: "x", "edit_file_tool": lambda path: "edited"},
            progress_tools=frozenset({"edit_file_tool"}), max_idle_turns=2,
        ),
        [text_message("user", "go")],
    )

    assert result.terminal_reason == TERMINAL_END_TURN
    assert result.final_text == "done"


async def test_idle_budget_is_off_without_progress_tools():
    """The planner declares no progress tools (it never edits) — it must
    be able to read for as many turns as it likes."""
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": f"f{i}"}, call_id=f"c{i}") for i in range(5)
    ] + [_text_response("done")])
    result = await run_agent_loop(
        _config(provider, tool_fns={"read_file_tool": lambda path: "x"}, max_idle_turns=2),
        [text_message("user", "go")],
    )

    assert result.terminal_reason == TERMINAL_END_TURN


def _trailing_text(request) -> str:
    """The text of the request's last message, or "" when that message is a
    tool result (no reminder or warning was appended this turn)."""
    return getattr(request.messages[-1].content[0], "text", "")


async def test_the_last_turns_before_an_idle_stop_carry_a_warning_that_is_never_stored():
    """The stop used to be silent: a model mid-way through reading a big
    codebase was simply cut off. Two turns out it is told, and how many it
    has left."""
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": f"f{i}"}, call_id=f"c{i}") for i in range(5)
    ] + [_text_response("never reached")])
    result = await run_agent_loop(
        _config(
            provider, tool_fns={"read_file_tool": lambda path: "x"},
            progress_tools=frozenset({"edit_file_tool", "write_file_tool"}), max_idle_turns=5,
        ),
        [text_message("user", "go")],
    )

    assert result.turns_used == 5 and len(provider.calls) == 5
    assert not any("IDLE WARNING" in _trailing_text(r) for r in provider.calls[:3])
    assert "IDLE WARNING: 3 turn(s)" in _trailing_text(provider.calls[3]) and "stops after 2 more" in _trailing_text(provider.calls[3])
    assert "stops after 1 more" in _trailing_text(provider.calls[4])
    # it names what counts as writing, generically
    assert "edit_file_tool, write_file_tool" in _trailing_text(provider.calls[3])
    # and, like the reminder it rides with, it never enters history
    assert "IDLE WARNING" not in str(result.history)


async def test_the_warning_clears_once_the_model_writes():
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a"}, call_id="c1"),
        _tool_call_response("read_file_tool", {"path": "b"}, call_id="c2"),
        _tool_call_response("edit_file_tool", {"path": "a"}, call_id="c3"),
        _text_response("done"),
    ])
    await run_agent_loop(
        _config(
            provider, tool_fns={"read_file_tool": lambda path: "x", "edit_file_tool": lambda path: "edited"},
            progress_tools=frozenset({"edit_file_tool"}), max_idle_turns=4,
        ),
        [text_message("user", "go")],
    )

    assert "IDLE WARNING" in _trailing_text(provider.calls[2])  # two idle turns in, two left
    assert "IDLE WARNING" not in _trailing_text(provider.calls[3])  # the edit reset it


async def test_the_warning_shares_one_trailing_message_with_the_reminder():
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a"}, call_id="c1"),
        _text_response("done"),
    ])
    await run_agent_loop(
        _config(
            provider, tool_fns={"read_file_tool": lambda path: "x"},
            progress_tools=frozenset({"edit_file_tool"}), max_idle_turns=2, trailing_reminder=lambda: "THE BOARD",
        ),
        [text_message("user", "go")],
    )

    trailing = _trailing_text(provider.calls[1])
    assert trailing.startswith("THE BOARD") and "IDLE WARNING" in trailing
    # One trailing message after the tool results, not a reminder and a warning.
    assert [m.role for m in provider.calls[1].messages][-2:] == ["tool", "user"]


async def test_no_warning_without_an_idle_budget():
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": f"f{i}"}, call_id=f"c{i}") for i in range(4)
    ] + [_text_response("done")])
    await run_agent_loop(
        _config(provider, tool_fns={"read_file_tool": lambda path: "x"}, max_idle_turns=2),  # no progress_tools
        [text_message("user", "go")],
    )

    assert not any("IDLE WARNING" in _trailing_text(r) for r in provider.calls)


# --- turn warning --------------------------------------------------------


async def test_the_last_turns_before_the_turn_cap_carry_a_warning():
    """A capped run's last text is the narration beside its final tool call.
    Warned in time, a role whose answer is that text answers instead."""
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": f"f{i}"}, call_id=f"c{i}") for i in range(3)
    ] + [_text_response("the answer")])
    result = await run_agent_loop(
        _config(provider, tool_fns={"read_file_tool": lambda path: "x"}, max_turns=4, turn_warning_window=2),
        [text_message("user", "go")],
    )

    assert result.terminal_reason == TERMINAL_END_TURN and result.final_text == "the answer"
    assert not any("TURN LIMIT" in _trailing_text(r) for r in provider.calls[:2])
    assert "1 more turn(s) after this one" in _trailing_text(provider.calls[2])
    assert "this is your last turn" in _trailing_text(provider.calls[3])
    # Stored like any reminder: kept out of what the caller persists, but in
    # what the provider saw.
    assert "TURN LIMIT" not in str(result.history)
    assert "TURN LIMIT" in str(result.sent_history)


async def test_no_turn_warning_by_default_or_without_a_cap():
    for overrides in ({"max_turns": 2}, {"turn_warning_window": 2}):
        provider = FakeProvider(script=[
            _tool_call_response("read_file_tool", {"path": "a"}, call_id="c1"),
            _text_response("done"),
        ])
        await run_agent_loop(
            _config(provider, tool_fns={"read_file_tool": lambda path: "x"}, **overrides),
            [text_message("user", "go")],
        )

        assert not any("TURN LIMIT" in _trailing_text(r) for r in provider.calls)


async def test_sent_history_is_what_the_last_request_sent_plus_its_answer():
    """What a caller adds one more request on top of: it must extend the
    last request exactly, reminders included, or it misses the cache."""
    provider = FakeProvider(script=[
        _tool_call_response("t", {"x": 1}, call_id="c1"),
        _tool_call_response("t", {"x": 2}, call_id="c2"),
    ])
    result = await run_agent_loop(
        _config(provider, tool_fns={"t": lambda x: "ok"}, trailing_reminder=lambda: "REMINDER", max_turns=2),
        [text_message("user", "go")],
    )

    last = provider.calls[-1].messages
    assert result.sent_history[: len(last)] == last
    assert [m.role for m in result.sent_history[len(last):]] == ["assistant", "tool"]


async def test_tool_choice_reaches_every_request():
    provider = FakeProvider(script=[_text_response("done")])

    await run_agent_loop(
        _config(provider, tools=[ToolDef(name="t", description="d", parameters={"type": "object"})], tool_choice="none"),
        [text_message("user", "go")],
    )

    assert provider.calls[0].tool_choice == "none"
    assert provider.calls[0].tools


# --- trailing reminder --------------------------------------------------


async def test_trailing_reminder_is_sent_every_turn_but_never_in_the_history_the_caller_gets():
    """Each reminder is kept in the loop's own history (so the next request
    extends this one — see the test below), but what a caller persists and
    resumes from holds none of them."""
    state = {"n": 0}

    def reminder() -> str:
        state["n"] += 1
        return f"CHECKLIST: {state['n']} remaining"

    provider = FakeProvider(script=[
        _tool_call_response("t", {"x": 1}, call_id="c1"),
        _text_response("done"),
    ])
    result = await run_agent_loop(
        _config(provider, tool_fns={"t": lambda x: "ok"}, trailing_reminder=reminder),
        [text_message("user", "go")],
    )

    # Both turns carried a reminder, each freshly evaluated.
    assert provider.calls[0].messages[-1].content[0].text == "CHECKLIST: 1 remaining"
    assert provider.calls[1].messages[-1].content[0].text == "CHECKLIST: 2 remaining"
    # And the history the caller persists holds none of them.
    texts = [b.text for m in result.history for b in m.content if isinstance(b, TextBlock)]
    assert not any("CHECKLIST" in t for t in texts)


async def test_every_request_begins_with_everything_the_previous_one_sent():
    """The cache regression. GPT-5.6+ saves the prompt prefix at the end of
    the latest user/tool message — the reminder. When the reminder was sent
    and then dropped, the next request never contained that prefix, and a
    dev run read 4% of its input from the cache. Every request must extend
    the one before it, reminder included."""
    state = {"n": 0}

    def reminder() -> str:
        state["n"] += 1
        return f"CHECKLIST v{state['n']}"

    provider = FakeProvider(script=[
        _tool_call_response("t", {"x": 1}, call_id="c1"),
        _tool_call_response("t", {"x": 2}, call_id="c2"),
        _tool_call_response("t", {"x": 3}, call_id="c3"),
        _text_response("done"),
    ])
    await run_agent_loop(
        _config(provider, tool_fns={"t": lambda x: "ok"}, trailing_reminder=reminder),
        [text_message("user", "go")],
    )

    for previous, current in zip(provider.calls, provider.calls[1:]):
        assert current.messages[: len(previous.messages)] == previous.messages
        assert current.messages[-1].content[0].text.startswith("CHECKLIST")


async def test_compaction_tombstones_old_reminders_but_keeps_the_latest():
    big = "x" * 5_000
    provider = FakeProvider(script=[
        _tool_call_response("t", {"x": i}, call_id=f"c{i}") for i in range(5)
    ] + [_text_response("done")])
    state = {"n": 0}

    def reminder() -> str:
        state["n"] += 1
        return f"CHECKLIST v{state['n']} " + "." * 200

    await run_agent_loop(
        _config(
            provider, tool_fns={"t": lambda x: big}, trailing_reminder=reminder,
            compaction_token_threshold=1_000, compaction_keep_recent_turns=1,
        ),
        [text_message("user", "go")],
    )

    last = provider.calls[-1].messages
    reminders = [m.content[0].text for m in last if m.role == "user"][1:]  # skip the brief
    assert reminders[-1].startswith("CHECKLIST v6")
    assert REMINDER_TOMBSTONE in reminders[:-2]
    assert not any(r.startswith("CHECKLIST v1 ") for r in reminders)


async def test_trailing_reminder_sits_after_the_whole_cached_prefix():
    provider = FakeProvider(script=[_text_response("done")])

    await run_agent_loop(
        _config(provider, trailing_reminder=lambda: "REMINDER"),
        [text_message("user", "the plan"), text_message("assistant", "ok")],
    )

    sent = provider.calls[0].messages
    assert [m.role for m in sent] == ["user", "assistant", "user"]
    assert sent[0].content[0].text == "the plan"  # prefix untouched


async def test_empty_reminder_appends_nothing():
    provider = FakeProvider(script=[_text_response("done")])

    await run_agent_loop(_config(provider, trailing_reminder=lambda: ""), [text_message("user", "go")])

    assert len(provider.calls[0].messages) == 1


async def test_a_bookkeeping_tool_does_not_clear_the_repeat_guard():
    """Regression: any non-read-only tool used to reset the guard, so the
    checklist tool the dev prompt tells the model to call constantly
    switched off the very protection weak models need."""
    from elstin.agents.loop import REPEAT_CALL_NUDGE

    reads: list[str] = []

    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a.py"}, call_id="c1"),
        _tool_call_response("update_progress_tool", {"updates": []}, call_id="c2"),
        _tool_call_response("read_file_tool", {"path": "a.py"}, call_id="c3"),
        _text_response("done"),
    ])
    result = await run_agent_loop(
        _config(
            provider,
            tool_fns={"read_file_tool": lambda path: reads.append(path) or "contents", "update_progress_tool": lambda updates: "noted"},
            read_only_tools=frozenset({"read_file_tool"}),
            bookkeeping_tools=frozenset({"update_progress_tool"}),
        ),
        [text_message("user", "go")],
    )

    assert reads == ["a.py"]  # the repeat was intercepted, not executed
    assert result.history[6].content[0].content == REPEAT_CALL_NUDGE


async def test_a_worktree_changing_tool_still_clears_the_repeat_guard():
    """The other half of the same rule: after an edit, an identical read is
    a fresh observation and must be allowed."""
    reads: list[str] = []

    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a.py"}, call_id="c1"),
        _tool_call_response("edit_file_tool", {"path": "a.py"}, call_id="c2"),
        _tool_call_response("read_file_tool", {"path": "a.py"}, call_id="c3"),
        _text_response("done"),
    ])
    await run_agent_loop(
        _config(
            provider,
            tool_fns={"read_file_tool": lambda path: reads.append(path) or "contents", "edit_file_tool": lambda path: "edited"},
            read_only_tools=frozenset({"read_file_tool"}),
            bookkeeping_tools=frozenset({"update_progress_tool"}),
        ),
        [text_message("user", "go")],
    )

    assert reads == ["a.py", "a.py"]


async def test_a_raising_trailing_reminder_does_not_end_the_run():
    """Regression: an exception here escaped run_dev, bypassed dev_node's
    DevLoopError catch, and abandoned the rest of the daemon poll cycle."""
    def boom() -> str:
        raise KeyError("action")

    provider = FakeProvider(script=[_text_response("done anyway")])

    result = await run_agent_loop(_config(provider, trailing_reminder=boom), [text_message("user", "go")])

    assert result.is_error is False
    assert result.final_text == "done anyway"
    assert len(provider.calls[0].messages) == 1  # nothing appended


# --- tool arguments checked before the tool runs ----------------------------

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
        "overwrite": {"type": "boolean"},
    },
    "required": ["path"],
}


def _read_tool_def() -> ToolDef:
    return ToolDef(name="read_file_tool", description="read", parameters=_READ_SCHEMA)


async def test_stringified_numbers_and_booleans_are_repaired_before_the_tool_runs():
    """Regression: Python reads overwrite="false" as true, and limit="200"
    failed with a TypeError the model could not act on."""
    seen: list[dict] = []
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"path": "a.py", "limit": "200", "overwrite": "false"}),
        _text_response("done"),
    ])

    await run_agent_loop(
        _config(provider, tools=[_read_tool_def()], tool_fns={"read_file_tool": lambda **kw: seen.append(kw) or "ok"}),
        [text_message("user", "go")],
    )

    assert seen == [{"path": "a.py", "limit": 200, "overwrite": False}]


async def test_a_call_still_invalid_after_repair_is_answered_and_not_run():
    ran: list[dict] = []
    provider = FakeProvider(script=[
        _tool_call_response("read_file_tool", {"limit": "lots"}),
        _text_response("done"),
    ])

    await run_agent_loop(
        _config(provider, tools=[_read_tool_def()], tool_fns={"read_file_tool": lambda **kw: ran.append(kw) or "ok"}),
        [text_message("user", "go")],
    )

    assert ran == []
    result = provider.calls[1].messages[-1].content[0]
    assert result.is_error
    assert "path is required" in result.content and "limit must be integer" in result.content


async def test_arguments_that_were_not_json_are_reported_and_the_tool_is_not_run():
    ran: list[dict] = []
    provider = FakeProvider(script=[
        LlmResponse(
            content=[ToolCallBlock(id="c1", name="read_file_tool", arguments={}, arguments_error="the arguments were not valid JSON")],
            stop_reason="tool_calls", usage=Usage(input_tokens=10, output_tokens=5), model="fake-model",
        ),
        _text_response("done"),
    ])

    await run_agent_loop(
        _config(provider, tools=[_read_tool_def()], tool_fns={"read_file_tool": lambda **kw: ran.append(kw) or "ok"}),
        [text_message("user", "go")],
    )

    assert ran == []
    result = provider.calls[1].messages[-1].content[0]
    assert result.is_error and "not valid JSON" in result.content


# --- context overflow ----------------------------------------------------------


async def test_a_context_overflow_compacts_deeper_and_retries_the_same_turn():
    big = "x" * 5_000
    provider = FakeProvider(script=[
        _tool_call_response("write_file_tool", {"path": "a.py", "content": big}, call_id="c1"),
        _tool_call_response("write_file_tool", {"path": "b.py", "content": big}, call_id="c2"),
        LlmContextOverflowError("This model's maximum context length is 8192 tokens"),
        _text_response("done"),
    ])
    retries: list[str] = []

    result = await run_agent_loop(
        _config(
            provider, tool_fns={"write_file_tool": lambda path, content: big},
            on_retry=lambda attempt, error, delay: retries.append(str(error)),
        ),
        [text_message("user", "go")],
    )

    assert result.terminal_reason == TERMINAL_END_TURN and result.is_error is False
    assert result.turns_used == 3
    assert len(retries) == 1
    retried = provider.calls[3].messages
    first_call = next(m for m in retried if m.role == "assistant").content[0]
    assert first_call.arguments["content"] == ARGUMENT_TOMBSTONE


async def test_a_second_overflow_in_the_same_turn_ends_the_run():
    big = "x" * 5_000
    provider = FakeProvider(script=[
        _tool_call_response("t", {}, call_id="c1"),
        _tool_call_response("t", {}, call_id="c2"),
        LlmContextOverflowError("prompt is too long"),
        LlmContextOverflowError("prompt is too long"),
    ])

    result = await run_agent_loop(_config(provider, tool_fns={"t": lambda: big}), [text_message("user", "go")])

    assert result.terminal_reason == TERMINAL_ERROR and result.is_error
    assert "prompt is too long" in result.error_message


async def test_an_overflow_with_nothing_left_to_compact_ends_the_run_and_says_so():
    provider = FakeProvider(script=[LlmContextOverflowError("prompt is too long")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.terminal_reason == TERMINAL_ERROR
    assert "nothing left to compact" in result.error_message
    assert len(provider.calls) == 1


# --- spend -------------------------------------------------------------------


async def test_the_budget_counts_the_cost_the_provider_reported():
    """A model missing from the pricing table used to be unbounded; a
    provider that reports what it billed (OpenRouter) bounds it anyway."""
    provider = FakeProvider(script=[
        LlmResponse(
            content=[ToolCallBlock(id="c1", name="t", arguments={})], stop_reason="tool_calls",
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.02), model="some/unpriced-model",
        ),
        _text_response("never reached"),
    ])

    result = await run_agent_loop(
        _config(provider, model="some/unpriced-model", max_budget_usd=0.01, tool_fns={"t": lambda: "ok"}),
        [text_message("user", "go")],
    )

    assert result.terminal_reason == TERMINAL_MAX_BUDGET
    assert len(provider.calls) == 1
