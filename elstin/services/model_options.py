"""The models a settings screen can offer for a model setting.

The provider's own listing when it can be had (an OpenAI-compatible endpoint
serves `GET /models` — OpenRouter lists several hundred), else the models elstin
has a price for. Either way it is a list of suggestions: a model id elstin has
never heard of still works when typed, so nothing here is a whitelist.

A project may use its own provider, endpoint and key, so the suggestions for a
project are asked of that project's provider.

Cached for a few minutes, keyed on where and as whom it was asked: opening a
settings dialog should not cost a round trip to the provider every time, and
a changed key or base URL — or another project that has its own — must not be
answered from someone else's cache.
A failed lookup is remembered too, for a minute, so a provider that is down
does not hold every dialog open for the timeout.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from elstin.config.store import resolve_text
from elstin.llm.errors import LlmError
from elstin.llm.pricing import known_models
from elstin.llm.registry import LlmNotConfiguredError, get_provider

logger = logging.getLogger(__name__)

LISTING_TIMEOUT_SECONDS = 15.0
_OK_TTL_SECONDS = 600.0
_FAILED_TTL_SECONDS = 60.0

_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _cache_key(project_id: int | None) -> tuple:
    return tuple(resolve_text(key, project_id) for key in ("llm_provider", "llm_base_url", "openai_api_key"))


def _fallback(error: str | None) -> dict[str, Any]:
    return {"models": known_models(), "source": "known", "error": error}


async def list_model_options(project_id: int | None = None) -> dict[str, Any]:
    """{"models": [...], "source": "provider" | "known", "error": str | None}
    for a project (None: the global settings). `error` says why the provider's
    listing was not used. Never raises."""
    key = _cache_key(project_id)
    cached = _cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]

    try:
        provider = get_provider(project_id)
        models = await asyncio.wait_for(provider.list_models(), timeout=LISTING_TIMEOUT_SECONDS)
    except LlmNotConfiguredError as exc:
        # Nothing to ask yet — not worth remembering, the next save fixes it.
        return _fallback(str(exc))
    except asyncio.TimeoutError:
        result, ttl = _fallback(f"the provider did not list its models within {LISTING_TIMEOUT_SECONDS:g}s"), _FAILED_TTL_SECONDS
    except LlmError as exc:
        logger.warning("could not list the provider's models: %s", exc)
        result, ttl = _fallback(f"the provider could not list its models: {exc}"), _FAILED_TTL_SECONDS
    else:
        if models:
            result, ttl = {"models": models, "source": "provider", "error": None}, _OK_TTL_SECONDS
        else:
            result, ttl = _fallback("the provider listed no models"), _FAILED_TTL_SECONDS

    _cache[key] = (time.monotonic() + ttl, result)
    return result
