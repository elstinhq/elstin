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

import asyncio
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
    LlmContextOverflowError,
    LlmError,
    LlmRateLimitError,
    LlmServerError,
    LlmTimeoutError,
    is_context_overflow_message,
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

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        name: str = "openai",
        request_timeout_seconds: float | None = None,
    ) -> None:
        self.name = name
        # An overall wall-clock deadline for one call (0/None = none). The SDK's
        # own timeout is per read, and a gateway that answers a slow request
        # with keep-alive bytes never trips it — one run's single model call
        # sat for 2h39m. This bounds the whole call, whatever arrives on the wire.
        self._request_timeout = request_timeout_seconds if request_timeout_seconds and request_timeout_seconds > 0 else None
        self.capabilities = Capabilities(
            supports_structured_output=True,
            # No server-side compaction on Chat Completions, so
            # forge/agents/loop.py owns it for this provider.
            supports_server_compaction=False,
        )
        # Prompt caching is deliberately left implicit. OpenAI inserts one
        # cache breakpoint itself — on gpt-5.6+ at the end of the latest
        # user or tool message — and `prompt_cache_options` (explicit
        # breakpoints, gpt-5.6+ only) would buy little here: forge's
        # request prefix is stable by construction — a frozen system
        # prompt, a deterministically ordered tool list, and append-only
        # history in which each request begins with everything the last
        # one sent (forge/agents/loop.py, point 5: breaking that last
        # property is what cut one dev run's cache reads to 4%). Verify
        # with the per-turn cache_read_tokens in each "token" activity
        # event before adding placement logic.
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        kwargs = self._build_kwargs(request)
        call = self._client.chat.completions.create(**kwargs)
        try:
            completion = await (asyncio.wait_for(call, timeout=self._request_timeout) if self._request_timeout else call)
        except asyncio.TimeoutError as exc:  # the deadline above; the SDK's own timeout is APITimeoutError
            raise LlmTimeoutError(
                f"no response from the model within {self._request_timeout:g}s — the request was abandoned"
            ) from exc
        except AuthenticationError as exc:
            raise LlmAuthError(str(exc)) from exc
        except RateLimitError as exc:
            raise LlmRateLimitError(str(exc)) from exc
        except APITimeoutError as exc:
            raise LlmTimeoutError(str(exc)) from exc
        except APIConnectionError as exc:
            raise LlmConnectionError(str(exc)) from exc
        except BadRequestError as exc:
            if getattr(exc, "code", None) == "context_length_exceeded" or is_context_overflow_message(str(exc)):
                raise LlmContextOverflowError(str(exc)) from exc
            raise LlmBadRequestError(str(exc)) from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise LlmServerError(str(exc)) from exc
            # 413: the request body itself is too big for the gateway —
            # the same remedy (a smaller history) applies.
            if exc.status_code == 413 or is_context_overflow_message(str(exc)):
                raise LlmContextOverflowError(str(exc)) from exc
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
            kwargs["tool_choice"] = request.tool_choice
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
            args: Any = {}
            args_error: str | None = None
            raw = tc.function.arguments or ""
            try:
                args = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                logger.warning("Tool call %s arguments weren't valid JSON: %r", tc.function.name, raw[:200])
                args_error = f"the arguments were not valid JSON ({exc.msg} at character {exc.pos}): {raw[:200]!r}"
            if not isinstance(args, dict):
                args_error = args_error or f"the arguments must be a JSON object, got {type(args).__name__}: {raw[:200]!r}"
                args = {}
            content.append(ToolCallBlock(id=tc.id, name=tc.function.name, arguments=args, arguments_error=args_error))

        usage = _parse_usage(completion.usage)

        return LlmResponse(
            content=content,
            stop_reason=_map_stop_reason(choice.finish_reason),
            usage=usage,
            model=completion.model,
            structured_output=structured_output,
        )


def _int_field(obj: Any, name: str) -> int:
    value = getattr(obj, name, None) if obj is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse_usage(usage_obj: Any) -> Usage:
    """forge's Usage from a Chat Completions `usage` object.

    OpenAI-compatible endpoints disagree on where some of it lives, and the
    SDK keeps any field it does not model as an extra attribute, so each is
    read with getattr:
    - cache hits: `prompt_tokens_details.cached_tokens` (OpenAI, OpenRouter),
      top-level `prompt_cache_hit_tokens` (DeepSeek's own API), or top-level
      `cached_tokens` (Z.AI);
    - cache writes: `prompt_tokens_details.cache_write_tokens` (OpenRouter),
      which some models bill above the normal input rate;
    - `cost`: what OpenRouter actually billed for the call, reported on
      every response. It is authoritative: pricing.py's table can only
      guess which upstream served the call, and upstreams differ — the
      table overstated one DeepSeek run's spend 2.3x."""
    if not usage_obj:
        return Usage()
    prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
    completion_details = getattr(usage_obj, "completion_tokens_details", None)
    cache_read = (
        _int_field(prompt_details, "cached_tokens")
        or _int_field(usage_obj, "prompt_cache_hit_tokens")
        or _int_field(usage_obj, "cached_tokens")
    )
    cost = getattr(usage_obj, "cost", None)
    return Usage(
        input_tokens=_int_field(usage_obj, "prompt_tokens"),
        output_tokens=_int_field(usage_obj, "completion_tokens"),
        cache_read_tokens=cache_read,
        cache_creation_tokens=_int_field(prompt_details, "cache_write_tokens"),
        reasoning_tokens=_int_field(completion_details, "reasoning_tokens"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
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
