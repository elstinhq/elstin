"""The sole place forge decides which LlmProvider adapter to construct.
Every role (verifier/QA's forge/services/generation.py, dev/planner's
forge/agents/loop.py) calls get_provider() instead of importing an
adapter or a provider SDK directly — adding a second provider later means
teaching this one function a new branch, not touching any caller.

Providers are cheap to construct (an AsyncOpenAI client wraps an httpx
client but opens no connection at construction time) and config can change
between calls (edited live at /settings), so this deliberately does NOT
cache an instance across calls the way forge/services/jira_service.py's
JiraService caches its network handshake — there's no handshake here to
avoid repeating.
"""
from __future__ import annotations

from forge.config.store import get_config, get_float_config, project_override, resolve_text
from forge.llm.base import LlmProvider
from forge.llm.openai_compat import OpenAiCompatProvider


class LlmNotConfiguredError(RuntimeError):
    """Raised when the configured provider has no credentials set."""


def _points_elsewhere(project_id: int | None) -> bool:
    """The project chose its own endpoint or provider, rather than inheriting
    the global one."""
    if project_override("llm_base_url", project_id):
        return True
    chosen = project_override("llm_provider", project_id)
    return chosen is not None and chosen.lower() != (get_config("llm_provider") or "openai").strip().lower()


def get_provider(project_id: int | None = None) -> LlmProvider:
    """The provider a project's calls go to. `llm_provider` (config, default
    "openai") selects the adapter; today "openai" is the only one wired. A
    future adapter (e.g. an Anthropic one, or a second OpenAI-compatible
    endpoint under a different name) is a new `elif` branch here, not a change
    to any caller.

    A project may set its own provider, endpoint and key (each inheriting the
    global setting when it sets none). One rule keeps that safe: a project that
    points at its own endpoint must bring its own key. The global key was issued
    for the global endpoint, and sending it to another host — a project's
    self-hosted gateway, a different vendor — hands that host a credential it
    was never meant to see. A project that sets only a key (another account on
    the same endpoint) is fine."""
    if _points_elsewhere(project_id) and not project_override("openai_api_key", project_id):
        raise LlmNotConfiguredError(
            "This project sends its model calls to its own endpoint but has no API key of its own — "
            "set one in the project's settings. The global key is not sent to another endpoint."
        )
    provider_name = (resolve_text("llm_provider", project_id) or "openai").lower()
    if provider_name == "openai":
        api_key = resolve_text("openai_api_key", project_id)
        if not api_key:
            raise LlmNotConfiguredError(
                "No OpenAI API key configured — set one at /settings before running planner/dev/verifier/qa."
            )
        base_url = resolve_text("llm_base_url", project_id)
        return OpenAiCompatProvider(
            api_key=api_key,
            base_url=base_url,
            name="openai",
            request_timeout_seconds=get_float_config("llm_request_timeout_seconds", 900.0),
            connect_timeout_seconds=get_float_config("llm_connect_timeout_seconds", 30.0),
        )
    raise LlmNotConfiguredError(f"Unknown llm_provider {provider_name!r} — only 'openai' is wired today.")
