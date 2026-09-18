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
from typing import Any

from forge.config.store import get_config
from forge.llm.errors import LlmError
from forge.llm.registry import get_provider
from forge.llm.types import LlmRequest, ResponseFormat, text_message
from forge.services.run_activity import append_activity

logger = logging.getLogger("forge.generation")


def get_verifier_qa_model() -> str:
    return get_config("verifier_qa_model") or "gpt-5.1-mini"


def _parse_json_content(content: str) -> dict[str, Any]:
    """Parse a model's JSON reply, tolerating prose around the object —
    only exercised when the provider doesn't support native structured
    output; a compliant provider's content IS the JSON object already."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


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
        raise RuntimeError(f"{label} failed via {provider.name}: {exc}") from exc
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
