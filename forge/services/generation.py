"""A deliberately small LLM completion helper for the verifier and QA
roles. Calls forge/llm/registry.py's configured provider directly — no
tools, no multi-turn loop, so forge/agents/loop.py (built for dev/planner's
tool-driven, potentially many-turn runs) would be pure overhead here. A
plain system+user -> JSON-object completion, using the provider's native
structured-output support (forge/llm/types.py's ResponseFormat) instead of
the old "instruct the model to return JSON, then hope" approach — the
prior implementation's tolerant regex-extraction parse is kept only as a
defensive fallback for a provider whose adapter reports
supports_structured_output=False.

If forge ever needs a second provider, forge/llm/registry.py is already
that abstraction (a resolve-provider layer keyed on config) — this module
doesn't grow its own.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

from forge.config.store import get_config
from forge.llm.errors import LlmError
from forge.llm.registry import get_provider
from forge.llm.types import LlmRequest, ResponseFormat, text_message
from forge.services.run_activity import append_activity
from forge.services.usage import remaining_story_budget_usd

logger = logging.getLogger("forge.generation")


class GenerationError(RuntimeError):
    """A single-shot JSON completion that cannot be used: either the
    provider call failed, or its reply carried no JSON object.

    A RuntimeError subclass so anything already catching RuntimeError keeps
    working, but a distinct type so forge/orchestration/graph.py's verifier
    and qa nodes can catch it and fail the *story*. That matters more than
    it looks: neither node used to catch anything, and nothing upstream of
    the graph catches a per-story exception either — the daemon's poll loop
    has no handler around handle_intent, so its only net is the blanket
    one in run_forever that abandons the whole cycle. One chatty model
    reply was therefore enough to skip every epic queued behind this one
    until the next tick. See forge/orchestration/graph.py's dev_node for
    the same reasoning applied to DevLoopError."""


def get_verifier_qa_model() -> str:
    return get_config("verifier_qa_model") or "gpt-5.1-mini"


# ```json ... ``` — by far the most common way a chat-tuned model wraps a
# JSON reply when it hasn't been given a hard schema to honor.
_FENCED_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} span in `text`, brace-counted and
    string-aware. A regex can't do this: the greedy first-brace-to-last
    -brace span below breaks the moment a reply contains two objects (a
    worked example followed by the real answer), and a lazy one breaks on
    the first nested object instead."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
    return spans


def _json_candidates(content: str) -> Iterator[str]:
    """Substrings to try parsing, best-first: the whole reply, then each
    fenced block, then the widest brace span, then each balanced object
    largest-first (the biggest one is the report; a smaller one is usually
    an example the model talked through first)."""
    yield content
    for match in _FENCED_RE.finditer(content):
        yield match.group(1)
    widest = re.search(r"\{.*\}", content, re.DOTALL)
    if widest:
        yield widest.group(0)
    yield from sorted(_balanced_objects(content), key=len, reverse=True)


def _parse_json_content(content: str) -> dict[str, Any]:
    """Parse a model's JSON reply, tolerating prose and code fences around
    the object — only exercised when the caller passed no schema, or the
    provider doesn't support native structured output; a compliant
    provider's content IS the JSON object already.

    Raises GenerationError rather than JSONDecodeError when nothing in the
    reply parses, because the callers that matter (verifier, QA) let it
    escape into the graph, and only a forge-owned type can be caught there
    without also swallowing real bugs."""
    for candidate in _json_candidates(content):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        # A bare list or scalar is not usable: every caller immediately
        # does .get() on this. Keep looking rather than handing back
        # something that fails later and further away.
        if isinstance(parsed, dict):
            return parsed
    raise GenerationError(
        f"model reply contained no JSON object (first 300 chars: {content[:300]!r})"
    )


async def chat_completion_json(
    *,
    messages: list[dict[str, Any]],
    response_schema: dict[str, Any] | None = None,
    response_schema_name: str = "response",
    run_id: str | None = None,
    phase: str = "system",
    label: str = "LLM call",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a JSON-object chat completion, optionally recording activity +
    token usage. `messages` is OpenAI-shaped ({"role": "system"|"user", ...})
    — translated into a system prompt + single user turn here, since every
    caller (verifier, qa) only ever sends a plain system+user pair, not a
    multi-turn conversation.

    `response_schema`, when given, is passed to the provider as a strict
    json_schema constraint (see forge/llm/types.py:ResponseFormat) rather
    than relying purely on the system prompt's "Return JSON" instruction —
    callers should still keep that instruction for providers/models where
    structured output isn't honored."""
    model = get_verifier_qa_model()
    # Both callers (verifier, QA) reach a model only through here, so one
    # check covers them. A GenerationError is what the graph's verifier and
    # qa nodes already catch, so an exhausted ceiling becomes a failed
    # story a human can retry after raising it, not a crashed poll cycle.
    remaining = remaining_story_budget_usd(run_id)
    if remaining is not None and remaining <= 0:
        raise GenerationError(
            f"{label} skipped: story spend ceiling (story_max_budget_usd) is exhausted. "
            "Raise the ceiling at /settings and retry the story."
        )
    provider = get_provider()
    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    user_parts = [m["content"] for m in messages if m.get("role") != "system"]
    system = "\n\n".join(str(part) for part in system_parts) if system_parts else None
    prompt = "\n\n".join(str(part) for part in user_parts)

    if run_id:
        append_activity(run_id, type="llm", phase=phase, title=f"Starting {label}", meta={"model": model, "provider": provider.name, "state": "started"})

    response_format = None
    if response_schema and provider.capabilities.supports_structured_output:
        response_format = ResponseFormat(name=response_schema_name, schema=response_schema)

    started_at = time.monotonic()
    try:
        response = await provider.complete(
            LlmRequest(model=model, system=system, messages=[text_message("user", prompt)], response_format=response_format)
        )
    except LlmError as exc:
        raise GenerationError(f"{label} failed via {provider.name}: {exc}") from exc
    api_duration_ms = int((time.monotonic() - started_at) * 1000)

    content = response.text()
    parsed = response.structured_output if response.structured_output is not None else _parse_json_content(content)

    usage_dict = {
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
        "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
        "cached_tokens": response.usage.cache_read_tokens,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": response.usage.cache_read_tokens,
        "cache_creation_tokens": response.usage.cache_creation_tokens,
        "cost_usd": response.usage.cost_usd,
        # Time waiting on the provider for this one call. The caller
        # (verifier/qa) records it as story_usage.api_duration_ms next to
        # its own phase-wide duration_ms, which also covers the
        # deterministic build/test gate — that's the whole point of
        # keeping the two separate.
        "api_duration_ms": api_duration_ms,
        "model": model,
    }

    if run_id:
        append_activity(run_id, type="llm", phase=phase, title=f"Completed {label}", meta={"model": model, "provider": provider.name, "state": "completed"})
        if usage_dict["total_tokens"] > 0:
            append_activity(run_id, type="token", phase=phase, title=f"Token usage ({label})", detail=f"{usage_dict['total_tokens']} tokens", meta=usage_dict)

    return parsed, usage_dict
