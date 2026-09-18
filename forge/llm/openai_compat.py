"""OpenAI (and OpenAI-compatible) provider adapter — the only module in
forge that imports the `openai` package. Talks the Chat Completions API
(`client.chat.completions.create`) rather than the newer Responses API:
Chat Completions is the shape every OpenAI-compatible endpoint (Azure,
OpenRouter, vLLM, Ollama, Together, Groq, Fireworks, ...) actually
implements, so this one adapter — pointed at a different `base_url` — is
what reaches all of them. A second, Responses-API-specific adapter is not
planned; if a provider ever requires it, it is a new module beside this
one, not a change to it.

Model ids and the `reasoning_effort`/`response_format` request shapes
below were read directly off the installed `openai` package
(openai.types.chat.completion_create_params, openai.types.shared.
reasoning_effort, openai.types.shared_params.response_format_json_schema)
rather than recalled — the OpenAI API surface moves fast and training
priors go stale quickly; re-check against the installed package if these
drift.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from forge.llm.base import Capabilities
from forge.llm.errors import (
    LlmAuthError,
    LlmBadRequestError,
    LlmConnectionError,
    LlmError,
    LlmRateLimitError,
    LlmServerError,
    LlmTimeoutError,
)
from forge.llm.types import (
    ContentBlock,
    ImageBlock,
    LlmRequest,
    LlmResponse,
    Message,
    StopReason,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    Usage,
)

logger = logging.getLogger("forge.llm.openai")

# Which models accept `reasoning_effort`, and which levels each one takes.
#
# Both halves matter, because both mistakes fail the same way: sending the
# field to a model that doesn't accept it is a 400, and sending a LEVEL a
# model doesn't accept is also a 400 — and a 400 maps to
# LlmBadRequestError, which forge/agents/loop.py deliberately never
# retries, so it kills the whole run rather than one turn. A model
# matching nothing here simply never gets the field, which costs only the
# configured effort's effect, so erring narrow stays the safe direction.
#
# An empty level set means "accepts whatever forge is configured with" —
# used for OpenAI's own families, whose behavior is unchanged from this
# adapter's original prefix check.
#
# The namespaced entries are OpenRouter model ids. Their level sets were
# read off each model's OpenRouter page on 2026-09-18, NOT recalled:
#   z-ai/glm-5.3        reasoning always on, levels low/high/max (max is
#                       the provider default, so omitting the field bills
#                       every call at max reasoning)
#   moonshotai/kimi-k3  levels low/max
# Re-check a model's own page before adding or changing a row here.
_ANY_LEVEL: frozenset[str] = frozenset()

_EFFORT_SUPPORT: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("gpt-5", "gpt-6", "o1", "o3", "o4"), _ANY_LEVEL),
    (("z-ai/glm-5.3",), frozenset({"low", "high", "max"})),
    (("moonshotai/kimi-k3",), frozenset({"low", "max"})),
)


def _model_variants(model: str) -> tuple[str, ...]:
    """The model id, plus its bare name when the id is namespaced
    ("openai/gpt-5.1" -> also "gpt-5.1"). A gateway that prefixes the
    vendor onto an otherwise-identical model should match the same rules
    as that vendor's own API."""
    bare = model.rsplit("/", 1)[-1]
    return (model,) if bare == model else (model, bare)


def resolve_reasoning_effort(model: str, effort: str | None) -> str | None:
    """The `reasoning_effort` value to send for this (model, effort) pair,
    or None to omit the field entirely. Dropping an unsupported level
    rather than forwarding it is deliberate: a request that runs at the
    model's default effort is worse than one that runs at the configured
    effort, but far better than a 400 that fails the story."""
    if not effort:
        return None
    variants = _model_variants(model)
    for prefixes, allowed_levels in _EFFORT_SUPPORT:
        if not any(variant.startswith(prefixes) for variant in variants):
            continue
        if not allowed_levels or effort in allowed_levels:
            return effort
        logger.warning(
            "Model %s does not accept reasoning effort %r (accepts: %s) — sending this request without the field",
            model, effort, ", ".join(sorted(allowed_levels)),
        )
        return None
    return None


class OpenAiCompatProvider:
    """Adapter for OpenAI's own API and any OpenAI-compatible endpoint
    (pass `base_url` to reach one). One instance per (api_key, base_url)
    pair — see forge/llm/registry.py."""

    def __init__(self, *, api_key: str, base_url: str | None = None, name: str = "openai") -> None:
        self.name = name
        self.capabilities = Capabilities(
            supports_structured_output=True,
            # No server-side compaction on Chat Completions, so
            # forge/agents/loop.py owns it for this provider.
            supports_server_compaction=False,
        )
        # Prompt caching is deliberately left implicit. OpenAI inserts one
        # cache breakpoint itself, and `prompt_cache_options` (explicit
        # breakpoints, gpt-5.6+ only) would buy little here: forge's
        # request prefix is already stable by construction — a frozen
        # system prompt, a deterministically ordered tool list, and
        # append-only history — which is what implicit caching keys on.
        # Verify with Usage.cache_read_tokens (recorded per phase in
        # forge/db/models/story_usage.py) before adding placement logic.
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        kwargs = self._build_kwargs(request)
        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except AuthenticationError as exc:
            raise LlmAuthError(str(exc)) from exc
        except RateLimitError as exc:
            raise LlmRateLimitError(str(exc)) from exc
        except APITimeoutError as exc:
            raise LlmTimeoutError(str(exc)) from exc
        except APIConnectionError as exc:
            raise LlmConnectionError(str(exc)) from exc
        except BadRequestError as exc:
            raise LlmBadRequestError(str(exc)) from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise LlmServerError(str(exc)) from exc
            raise LlmError(str(exc)) from exc

        return self._parse_response(completion, request)

    def _build_kwargs(self, request: LlmRequest) -> dict[str, Any]:
        messages = self._to_openai_messages(request)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "parallel_tool_calls": request.parallel_tool_calls if request.tools else None,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in request.tools
            ]
        # No max_completion_tokens: forge deliberately never caps a single
        # response — see forge/llm/types.py's LlmRequest.
        reasoning_effort = resolve_reasoning_effort(request.model, request.effort)
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if request.response_format:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_format.name,
                    "schema": request.response_format.schema,
                    "strict": request.response_format.strict,
                },
            }
        return {k: v for k, v in kwargs.items() if v is not None}

    def _to_openai_messages(self, request: LlmRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if request.system:
            out.append({"role": "system", "content": request.system})
        for message in request.messages:
            out.extend(self._to_openai_message(message))
        return out

    def _to_openai_message(self, message: Message) -> list[dict[str, Any]]:
        if message.role == "tool":
            # forge's normalized Message groups all of one turn's
            # ToolResultBlocks together; OpenAI wants one {"role": "tool"}
            # message per tool_call_id.
            return [
                {"role": "tool", "tool_call_id": b.tool_call_id, "content": _tool_result_text(b)}
                for b in message.content
                if isinstance(b, ToolResultBlock)
            ]
        if message.role == "assistant":
            text_parts = [b.text for b in message.content if isinstance(b, TextBlock)]
            tool_calls = [b for b in message.content if isinstance(b, ToolCallBlock)]
            out: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                out["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in tool_calls
                ]
            return [out]
        # user/system messages: text and/or images.
        parts: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageBlock):
                parts.append({"type": "image_url", "image_url": {"url": f"data:{block.media_type};base64,{block.data}"}})
        if len(parts) == 1 and parts[0]["type"] == "text":
            return [{"role": message.role, "content": parts[0]["text"]}]
        return [{"role": message.role, "content": parts}]

    def _parse_response(self, completion: Any, request: LlmRequest) -> LlmResponse:
        choice = completion.choices[0]
        msg = choice.message
        content: list[ContentBlock] = []
        if msg.content:
            content.append(TextBlock(text=msg.content))
        structured_output: dict[str, Any] | None = None
        if request.response_format and msg.content:
            try:
                structured_output = json.loads(msg.content)
            except json.JSONDecodeError:
                logger.warning("OpenAI response_format requested but content wasn't valid JSON")
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                logger.warning("Tool call %s arguments weren't valid JSON: %r", tc.function.name, tc.function.arguments[:200])
                args = {}
            content.append(ToolCallBlock(id=tc.id, name=tc.function.name, arguments=args))

        usage_obj = completion.usage
        usage = Usage()
        if usage_obj:
            prompt_details = usage_obj.prompt_tokens_details
            completion_details = usage_obj.completion_tokens_details
            usage = Usage(
                input_tokens=usage_obj.prompt_tokens,
                output_tokens=usage_obj.completion_tokens,
                cache_read_tokens=(prompt_details.cached_tokens if prompt_details else 0) or 0,
                reasoning_tokens=(completion_details.reasoning_tokens if completion_details else 0) or 0,
            )

        return LlmResponse(
            content=content,
            stop_reason=_map_stop_reason(choice.finish_reason),
            usage=usage,
            model=completion.model,
            structured_output=structured_output,
        )


def _tool_result_text(block: ToolResultBlock) -> str:
    return f"ERROR: {block.content}" if block.is_error else block.content


def _map_stop_reason(finish_reason: str | None) -> StopReason:
    return {
        "stop": "end_turn",
        "tool_calls": "tool_calls",
        "length": "max_tokens",
        "content_filter": "content_filter",
    }.get(finish_reason or "", "error")
