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

from forge.config.store import get_config
from forge.llm.base import LlmProvider
from forge.llm.openai_compat import OpenAiCompatProvider


class LlmNotConfiguredError(RuntimeError):
    """Raised when the configured provider has no credentials set."""


def get_provider() -> LlmProvider:
    """The single provider forge is configured to use. `llm_provider`
    (config, default "openai") selects the adapter; today "openai" is the
    only one wired. A future adapter (e.g. an Anthropic one, or a second
    OpenAI-compatible endpoint under a different name) is a new `elif`
    branch here, not a change to any caller."""
    provider_name = (get_config("llm_provider") or "openai").strip().lower()
    if provider_name == "openai":
        api_key = get_config("openai_api_key")
        if not api_key:
            raise LlmNotConfiguredError(
                "No OpenAI API key configured — set one at /settings before running planner/dev/verifier/qa."
            )
        base_url = get_config("llm_base_url") or None
        return OpenAiCompatProvider(api_key=api_key, base_url=base_url, name="openai")
    raise LlmNotConfiguredError(f"Unknown llm_provider {provider_name!r} — only 'openai' is wired today.")
