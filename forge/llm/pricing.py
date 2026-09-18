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

*** PRICING BELOW IS A BEST-EFFORT PLACEHOLDER, NOT VERIFIED AGAINST A
*** LIVE SOURCE. OpenAI's price list changes independently of this
*** codebase and this module has no live pricing API to read from.
*** Verify every rate against https://openai.com/api/pricing (or your
*** provider's own price list, for a non-OpenAI base_url) before relying
*** on these figures for budget alerts or cost dashboards. Update this
*** table — and this notice's "verified as of" date — whenever you do.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float


# provider name -> model id -> pricing. "openai" entries are placeholders
# (see module docstring) — NOT verified as of any date. Add a row per
# (provider, model) your deployment actually uses; an unlisted pair falls
# back to cost_source="unknown" rather than a silently wrong number.
MODEL_PRICING: dict[str, dict[str, ModelPricing]] = {
    "openai": {
        "gpt-5.1": ModelPricing(input_per_mtok=1.25, output_per_mtok=10.00, cached_input_per_mtok=0.125),
        "gpt-5.1-mini": ModelPricing(input_per_mtok=0.25, output_per_mtok=2.00, cached_input_per_mtok=0.025),
        "gpt-4.1": ModelPricing(input_per_mtok=2.00, output_per_mtok=8.00, cached_input_per_mtok=0.50),
        "gpt-4.1-mini": ModelPricing(input_per_mtok=0.40, output_per_mtok=1.60, cached_input_per_mtok=0.10),
    },
}


def estimate_cost_usd(
    provider: str | None,
    model: str | None,
    *,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
) -> float | None:
    """None when the (provider, model) pair isn't in the table — callers
    should record cost_source="unknown" rather than assert a wrong
    number."""
    if not provider or not model:
        return None
    pricing = MODEL_PRICING.get(provider, {}).get(model)
    if pricing is None:
        return None
    uncached_input = max(input_tokens - cache_read_tokens, 0)
    return (
        uncached_input * pricing.input_per_mtok
        + cache_read_tokens * pricing.cached_input_per_mtok
        + output_tokens * pricing.output_per_mtok
    ) / 1_000_000
