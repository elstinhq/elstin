"""Provider-agnostic vocabulary for forge's agent loop and roles.

This is the ONLY set of types forge/agents/loop.py and the role modules
(dev/planner/verifier/qa) are allowed to know about. Every provider
adapter under forge/llm/ (openai_compat.py today; others later) translates
its own SDK's request/response shapes into and out of these dataclasses at
the edge — nothing upstream of the adapter ever imports `openai` (or any
other provider package) directly.

Deliberately modeled close to Anthropic's content-block shape (a list of
typed blocks per message) rather than OpenAI's older
content-string-plus-separate-tool_calls-list shape, because the block
model is strictly more expressive (thinking blocks, mixed text/tool_use in
one turn) and every provider's shape can be projected onto it. Adapters
own the two-way translation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

StopReason = Literal[
    "end_turn",       # model finished naturally
    "tool_calls",      # model wants to call one or more tools
    "max_tokens",      # hit the output token cap
    "content_filter",  # provider refused / filtered the content
    "error",           # provider-side error terminated the turn
]


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ThinkingBlock:
    """Reasoning/thinking content.

    **No currently wired adapter produces one of these** — OpenAI's Chat
    Completions response carries no reasoning content (only a
    `reasoning_tokens` count, which lands on Usage instead), so don't go
    hunting for the producer. It stays in the vocabulary because it is
    part of the normalized shape a reasoning-exposing adapter would need,
    and because `history_from_json` must be able to round-trip one if a
    stored history ever contains it. Consumers must treat "no
    ThinkingBlocks, ever" as the normal case."""

    text: str
    type: Literal["thinking"] = "thinking"


@dataclass(frozen=True)
class ToolCallBlock:
    id: str
    name: str
    arguments: dict[str, Any]
    # Set by an adapter when the provider's arguments could not be decoded
    # (not valid JSON): `arguments` is then {} and this says why, so the
    # loop answers the model with the real problem instead of running the
    # tool with no arguments. None for every well-formed call.
    arguments_error: str | None = None
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True)
class ImageBlock:
    """Base64-encoded image input (planner attachments)."""

    media_type: str
    data: str
    type: Literal["image"] = "image"


ContentBlock = TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock | ImageBlock


@dataclass(frozen=True)
class Message:
    role: Role
    content: list[ContentBlock]


def text_message(role: Role, text: str) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ResponseFormat:
    """A json_schema structured-output request. `strict=True` requires
    every property to be listed in `required` at every nesting level (a
    provider-side constraint, not a forge one) — see
    forge/agents/roles/planner.py's schema for the shape this demands."""

    name: str
    schema: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost_usd=(
                None if self.cost_usd is None and other.cost_usd is None
                else (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
            ),
        )


@dataclass(frozen=True)
class LlmRequest:
    model: str
    messages: list[Message]
    system: str | None = None
    tools: list[ToolDef] = field(default_factory=list)
    response_format: ResponseFormat | None = None
    effort: str | None = None
    # Deliberately no `max_tokens`: forge never caps a single response's
    # output length. Truncating an agent turn mid-edit is worse than
    # paying for the whole turn, and run-level spend is already bounded by
    # LoopConfig's max_turns/max_budget_usd (forge/agents/loop.py). A
    # response that hits the *provider's* own cap comes back as
    # stop_reason="max_tokens" and the loop treats it as a hard failure.
    parallel_tool_calls: bool = True


@dataclass(frozen=True)
class LlmResponse:
    content: list[ContentBlock]
    stop_reason: StopReason
    usage: Usage
    model: str
    structured_output: dict[str, Any] | None = None

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.content if isinstance(b, ToolCallBlock)]


# --- (de)serialization -------------------------------------------------
# forge/orchestration/state.py's FeatureRunState carries a run's dev-loop
# history as plain JSON (a `dev_history: list[dict]` field) because it is
# durably checkpointed by LangGraph's AsyncPostgresSaver alongside every
# other piece of run state — see forge/orchestration/graph.py's
# checkpointer_from_url. That's what replaces the old Claude Agent SDK's
# `resume=<session_id>` (the SDK owned its own server-side conversation
# state; forge now owns it, and gets it persisted for free through the
# graph's existing checkpoint mechanism rather than a bespoke DB table).
# These two functions are the only place that boundary is crossed.
#
# Keyed by each block dataclass's own `type` discriminator, so a block
# added to ContentBlock without a row here fails loudly in
# block_from_dict rather than silently round-tripping as something else.
_BLOCK_TYPES: dict[str, type] = {
    "text": TextBlock,
    "thinking": ThinkingBlock,
    "tool_call": ToolCallBlock,
    "tool_result": ToolResultBlock,
    "image": ImageBlock,
}


def block_to_dict(block: ContentBlock) -> dict[str, Any]:
    return asdict(block)


def block_from_dict(data: dict[str, Any]) -> ContentBlock:
    block_type = data.get("type")
    cls = _BLOCK_TYPES.get(block_type or "")
    if cls is None:
        raise ValueError(f"Unknown content block type: {block_type!r}")
    kwargs = {k: v for k, v in data.items() if k != "type"}
    return cls(**kwargs)


def message_to_dict(message: Message) -> dict[str, Any]:
    return {"role": message.role, "content": [block_to_dict(b) for b in message.content]}


def message_from_dict(data: dict[str, Any]) -> Message:
    return Message(role=data["role"], content=[block_from_dict(b) for b in data["content"]])


def history_to_json(history: list[Message]) -> list[dict[str, Any]]:
    return [message_to_dict(m) for m in history]


def history_from_json(data: list[dict[str, Any]]) -> list[Message]:
    return [message_from_dict(m) for m in data]
