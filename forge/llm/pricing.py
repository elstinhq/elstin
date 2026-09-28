"""Fallback $/token pricing, used only when a provider adapter doesn't
report a cost for a call (OpenAI's Chat Completions API never does — no
provider currently wired reports cost directly, so this table is the
primary cost source today, not a fallback in practice; forge/services/
usage.py:record_phase_usage still treats an explicit cost_usd as
authoritative when a future adapter does supply one).

Keyed by (provider, model) — NOT model alone — because two providers can
serve a same-named model (e.g. an OpenAI-compatible self-hosted endpoint
proxying "gpt-4.1") at different rates; collapsing the key to just the
model id would silently misattribute cost between them.

*** THE "gpt-*" RATES BELOW ARE BEST-EFFORT PLACEHOLDERS, NOT VERIFIED
*** AGAINST A LIVE SOURCE. OpenAI's price list changes independently of
*** this codebase and this module has no live pricing API to read from.
*** Verify every rate against https://openai.com/api/pricing (or your
*** provider's own price list, for a non-OpenAI base_url) before relying
*** on these figures for budget alerts or cost dashboards. Update this
*** table — and this notice's "verified as of" date — whenever you do.
***
*** The OpenRouter rows were read off each model's own OpenRouter page on
*** 2026-09-18. See the note above those rows on why they are not the
*** cheapest figure each page advertises.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float
    # Writing a prompt prefix into the cache, for models that bill it above
    # the normal input rate (GPT-5.6+, Anthropic). None: same as input.
    cache_write_per_mtok: float | None = None


# provider name -> model id -> pricing. The "gpt-*" rows are unverified
# placeholders (see module docstring); the namespaced rows were read off
# OpenRouter on 2026-09-18. Add a row per (provider, model) your
# deployment actually uses; an unlisted pair falls back to
# cost_source="unknown" rather than a silently wrong number — which also
# means max_budget_usd never trips for it, so a model you rely on a
# budget cap for MUST have a row here.
MODEL_PRICING: dict[str, dict[str, ModelPricing]] = {
    # Every OpenAI-compatible endpoint lands under "openai" today, because
    # forge/llm/registry.py names the provider after the adapter it picked
    # rather than after the base_url it points at. So an OpenRouter model
    # id belongs in THIS dict, not a sibling one — a second key here would
    # simply never be looked up.
    "openai": {
        "gpt-5.1": ModelPricing(input_per_mtok=1.25, output_per_mtok=10.00, cached_input_per_mtok=0.125),
        "gpt-5.1-mini": ModelPricing(input_per_mtok=0.25, output_per_mtok=2.00, cached_input_per_mtok=0.025),
        "gpt-4.1": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00, cached_input_per_mtok=0.50),
        "gpt-4.1-mini": ModelPricing(input_per_mtok=0.40, output_per_mtok=1.60, cached_input_per_mtok=0.10),
        # Verified, unlike the rows above: read off
        # developers.openai.com/api/docs/pricing on 2026-09-25 (Standard
        # tier). OpenAI lists a second, higher "long context" rate for this
        # model (0.40 / 0.04 / 0.50 / 1.80); forge's prompts stay well
        # under it, so only the short-context rate is modeled.
        "gpt-5.6-luna": ModelPricing(
            input_per_mtok=0.20, output_per_mtok=1.20, cached_input_per_mtok=0.02, cache_write_per_mtok=0.25,
        ),
        # --- OpenRouter model ids, read off each model page 2026-09-18 ---
        #
        # Each of these is served by 17-23 competing hosts whose rates
        # differ by up to threefold, and OpenRouter's default routing does
        # not promise you the cheapest. These rows therefore carry the
        # MODAL LISTED rate rather than the discounted headline each page
        # advertises: the only consumer is max_budget_usd (forge/agents/
        # loop.py), and a budget cap that under-estimates is a cap that
        # doesn't stop a runaway run. Cost dashboards read the same figures,
        # so treat them as an upper bound, not an invoice.
        "moonshotai/kimi-k3": ModelPricing(input_per_mtok=3.00, output_per_mtok=15.00, cached_input_per_mtok=0.30),
        "z-ai/glm-5.3": ModelPricing(input_per_mtok=1.40, output_per_mtok=4.40, cached_input_per_mtok=0.26),
        "deepseek/deepseek-v4-pro-0813": ModelPricing(input_per_mtok=1.32, output_per_mtok=3.96, cached_input_per_mtok=0.044),
        "deepseek/deepseek-v4.1-flash": ModelPricing(input_per_mtok=0.30, output_per_mtok=1.20, cached_input_per_mtok=0.03),
    },
}


# A release-date suffix on a model id: "-20260813", "-2026-08-13", "-0813".
_DATE_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2}|\d{4})$")


def _undated(model: str) -> str:
    return _DATE_SUFFIX_RE.sub("", model)


def pricing_for(provider: str | None, model: str | None) -> ModelPricing | None:
    """The table row for (provider, model), or None.

    Exact id first. Failing that, the same model with its release-date
    suffix ignored on both sides, when exactly one row matches: an API
    answers with a dated snapshot id ("deepseek/deepseek-v4-pro-20260813",
    "gpt-5.6-luna-2026-08-01") where the config — and this table — name
    the model, and a miss here silently disables max_budget_usd."""
    if not provider or not model:
        return None
    rows = MODEL_PRICING.get(provider, {})
    exact = rows.get(model)
    if exact is not None:
        return exact
    wanted = _undated(model)
    candidates = [pricing for key, pricing in rows.items() if _undated(key) == wanted]
    return candidates[0] if len(candidates) == 1 else None


def estimate_cost_usd(
    provider: str | None,
    model: str | None,
    *,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
) -> float | None:
    """None when the (provider, model) pair isn't in the table — callers
    should record cost_source="unknown" rather than assert a wrong
    number. `input_tokens` is the whole prompt, cache reads and writes
    included, as OpenAI-compatible APIs report it."""
    pricing = pricing_for(provider, model)
    if pricing is None:
        return None
    cache_write_rate = pricing.cache_write_per_mtok if pricing.cache_write_per_mtok is not None else pricing.input_per_mtok
    uncached_input = max(input_tokens - cache_read_tokens - cache_creation_tokens, 0)
    return (
        uncached_input * pricing.input_per_mtok
        + cache_read_tokens * pricing.cached_input_per_mtok
        + cache_creation_tokens * cache_write_rate
        + output_tokens * pricing.output_per_mtok
    ) / 1_000_000
