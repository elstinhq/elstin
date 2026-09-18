"""forge/agents/loop.py — the shared agentic loop every LLM-driven role
(dev, planner) runs through. Exercised entirely against a fake
LlmProvider — no network, no real provider SDK — since the loop only ever
touches forge/llm/types.py's normalized vocabulary and forge/llm/base.py's
Protocol, never a concrete adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from forge.agents.loop import (
    TERMINAL_END_TURN,
    TERMINAL_MAX_BUDGET,
    TERMINAL_MAX_TURNS,
    LoopConfig,
    run_agent_loop,
)
from forge.llm.base import Capabilities
from forge.llm.errors import LlmRateLimitError
from forge.llm.types import (
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
    # gpt-5.1 costs $1.25/mtok input in forge/llm/pricing.py; a single
    # 10-input-token call is far too cheap to hit a real budget, so use an
    # artificially tiny cap to force the trip on the first turn.
    result = await run_agent_loop(
        _config(provider, max_budget_usd=0.0000001, tool_fns={"t": lambda: "ok"}), [text_message("user", "go")]
    )

    assert result.terminal_reason == TERMINAL_MAX_BUDGET
    assert result.is_error is False


async def test_transient_error_is_retried_then_succeeds(monkeypatch):
    import forge.agents.loop as loop_mod

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmRateLimitError("slow down"), _text_response("ok now")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is False
    assert result.final_text == "ok now"
    assert len(provider.calls) == 2


async def test_non_retryable_error_ends_loop_as_error(monkeypatch):
    import forge.agents.loop as loop_mod
    from forge.llm.errors import LlmBadRequestError

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmBadRequestError("bad schema")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert result.terminal_reason == "error"
    assert "bad schema" in result.error_message


async def test_retries_exhausted_ends_loop_as_error(monkeypatch):
    import forge.agents.loop as loop_mod

    monkeypatch.setattr(loop_mod, "retry_backoff_seconds", lambda attempt: 0)
    provider = FakeProvider(script=[LlmRateLimitError("1"), LlmRateLimitError("2"), LlmRateLimitError("3")])

    result = await run_agent_loop(_config(provider), [text_message("user", "go")])

    assert result.is_error is True
    assert len(provider.calls) == 3


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


async def test_compaction_usage_is_reported_through_on_turn():
    """The compaction call is a billable provider call. Its tokens used to
    reach LoopResult.usage but never the on_turn hook — and since the
    roles build their story_usage rows from on_turn, every compaction a
    long run needed was silently unbilled."""
    provider = FakeProvider(script=[
        _text_response("summary of everything"),  # the compaction call
        _text_response("done"),                    # the real turn
    ])
    seen_usage = []

    big_history = [text_message("user", "x" * 5000) for _ in range(10)]
    result = await run_agent_loop(
        _config(provider, compaction_token_threshold=1, compaction_keep_recent=2,
                on_turn=lambda turn, response: seen_usage.append(response.usage.input_tokens)),
        big_history,
    )

    # Two provider calls happened (compaction + turn); on_turn saw both.
    assert len(provider.calls) == 2
    assert seen_usage == [10, 10]
    assert result.usage.input_tokens == 20


async def test_compaction_failure_is_non_fatal():
    """A failed *housekeeping* call shouldn't kill a run that's otherwise
    fine — the next turn just carries the uncompacted history."""
    from forge.llm.errors import LlmBadRequestError

    provider = FakeProvider(script=[LlmBadRequestError("summarizer rejected"), _text_response("done anyway")])
    big_history = [text_message("user", "x" * 5000) for _ in range(10)]

    result = await run_agent_loop(
        _config(provider, compaction_token_threshold=1, compaction_keep_recent=2), big_history
    )

    assert result.is_error is False
    assert result.final_text == "done anyway"


async def test_server_side_compaction_suppresses_our_own():
    """A provider that compacts server-side must not also get forge's
    summarize-and-splice pass on top."""
    provider = FakeProvider(
        script=[_text_response("done")],
        capabilities=Capabilities(supports_server_compaction=True),
    )
    big_history = [text_message("user", "x" * 5000) for _ in range(10)]

    result = await run_agent_loop(
        _config(provider, compaction_token_threshold=1, compaction_keep_recent=2), big_history
    )

    # One call only — no compaction call was made.
    assert len(provider.calls) == 1
    assert result.is_error is False


async def test_size_estimate_counts_image_payloads():
    """A planner run with a JIRA screenshot attached carries megabytes in
    one ImageBlock. Ignoring that made the compaction trigger report ~0
    for the single largest thing in the request."""
    from forge.agents.loop import _estimate_message_chars
    from forge.llm.types import ImageBlock, Message

    with_image = [Message(role="user", content=[ImageBlock(media_type="image/png", data="A" * 10_000)])]

    assert _estimate_message_chars(with_image) == 10_000


async def test_compaction_splits_without_separating_tool_call_from_result():
    from forge.agents.loop import _split_for_compaction
    from forge.llm.types import Message

    history = [
        text_message("user", "a"),
        Message(role="assistant", content=[ToolCallBlock(id="c1", name="t", arguments={})]),
        Message(role="tool", content=[ToolResultBlock(tool_call_id="c1", content="ok")]),
        text_message("user", "b"),
    ]
    # keep_recent=2 naively wants to split at index 2 (right BETWEEN the
    # tool_calls-bearing assistant message and its answering tool message)
    # — must walk the boundary back to before the assistant message
    # instead, so the pair stays together on the "keep" side.
    to_summarize, to_keep = _split_for_compaction(history, keep_recent=2)

    assert to_summarize == history[:1]
    assert to_keep == history[1:]
