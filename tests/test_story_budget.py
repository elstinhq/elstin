"""The per-story spend ceiling (`story_max_budget_usd`).

Every other cap in forge is per loop: the planner has one, dev has one,
each explorer carves a slice out of the planner's. Nothing bounded a story
end to end, so planning plus investigation plus however many dev fix
rounds plus a review could each stay inside their own limit while the
story as a whole cost an unbounded multiple of any of them. These tests
cover the arithmetic and each of the four places a story can reach a
model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from forge.config.store import set_config
from forge.services.usage import (
    effective_budget_usd,
    record_phase_usage,
    remaining_story_budget_usd,
    story_spend_usd,
)

pytestmark = pytest.mark.usefixtures("forge_db")


def _spend(run_id: str, cost: float, *, phase: str = "dev") -> None:
    record_phase_usage(run_id, phase=phase, model="gpt-5.1", cost_usd=cost, input_tokens=10)


# --- arithmetic --------------------------------------------------------


def test_spend_sums_every_phase_of_a_story():
    _spend("ENG-1", 0.10, phase="planner")
    _spend("ENG-1", 0.25, phase="dev")
    _spend("ENG-1", 0.05, phase="verifier")
    _spend("ENG-2", 9.99, phase="dev")

    assert story_spend_usd("ENG-1") == pytest.approx(0.40)
    assert story_spend_usd("ENG-NOTHING") == 0.0


def test_no_ceiling_configured_means_unbounded():
    """None is distinct from zero: it means no ceiling exists, whereas
    zero would mean one exists and is used up."""
    set_config("story_max_budget_usd", "0")

    assert remaining_story_budget_usd("ENG-1") is None
    assert effective_budget_usd("ENG-1", 1.50) == (1.50, False)


def test_remaining_is_the_ceiling_minus_what_was_spent():
    set_config("story_max_budget_usd", "1.00")
    _spend("ENG-1", 0.30)

    assert remaining_story_budget_usd("ENG-1") == pytest.approx(0.70)


def test_phase_cap_is_clamped_to_what_the_story_has_left():
    """The point of the whole change: dev's own generous cap cannot be
    spent when the story is nearly out."""
    set_config("story_max_budget_usd", "1.00")
    _spend("ENG-1", 0.90)

    cap, exhausted = effective_budget_usd("ENG-1", 5.00)

    assert cap == pytest.approx(0.10)
    assert exhausted is False


def test_an_unbounded_phase_still_inherits_the_story_ceiling():
    set_config("story_max_budget_usd", "2.00")
    _spend("ENG-1", 0.50)

    cap, exhausted = effective_budget_usd("ENG-1", 0.0)

    assert cap == pytest.approx(1.50)
    assert exhausted is False


def test_a_tighter_phase_cap_still_wins():
    set_config("story_max_budget_usd", "10.00")

    assert effective_budget_usd("ENG-1", 0.25) == (0.25, False)


def test_exhausted_reports_a_flag_rather_than_a_zero_cap():
    """Zero means unbounded everywhere in forge, so an exhausted remainder
    must never be passed through as a cap or it would do the opposite of
    what was asked."""
    set_config("story_max_budget_usd", "1.00")
    _spend("ENG-1", 1.00)

    cap, exhausted = effective_budget_usd("ENG-1", 5.00)

    assert exhausted is True
    assert cap == 0.0


def test_overspend_is_still_exhausted_not_negative_budget():
    set_config("story_max_budget_usd", "1.00")
    _spend("ENG-1", 2.50)

    assert effective_budget_usd("ENG-1", 5.00) == (0.0, True)


def test_a_run_with_no_id_is_never_ceilinged():
    set_config("story_max_budget_usd", "1.00")

    assert remaining_story_budget_usd(None) is None


def test_usage_with_no_price_row_cannot_constrain_the_ceiling():
    """Documented limitation rather than a bug: with no rate to multiply
    by there is no spend to count, so a model forge has no price for
    cannot be bounded. This is why the pricing table has a test pinning
    every shipped model default."""
    set_config("story_max_budget_usd", "1.00")
    record_phase_usage("ENG-1", phase="dev", model="unpriced/model", input_tokens=10_000_000, output_tokens=10_000_000)

    assert story_spend_usd("ENG-1") == 0.0
    assert remaining_story_budget_usd("ENG-1") == pytest.approx(1.00)


# --- enforcement at each model entry point -----------------------------


@dataclass
class FakeProvider:
    script: list
    name: str = "openai"
    capabilities: object = field(default_factory=lambda: __import__("forge.llm.base", fromlist=["Capabilities"]).Capabilities())

    async def complete(self, request):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _exhaust(run_id: str) -> None:
    set_config("story_max_budget_usd", "1.00")
    _spend(run_id, 1.00)


async def test_dev_refuses_to_start_when_the_ceiling_is_exhausted(git_repo, monkeypatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.agents.roles.dev_loop import DevLoopError, run_dev

    set_config("openai_api_key", "sk-test")
    _exhaust("ENG-1")
    monkeypatch.setattr(dev_loop_module, "get_provider", lambda: FakeProvider(script=[]))

    with pytest.raises(DevLoopError, match="spend ceiling"):
        await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo, run_id="ENG-1")


async def test_planner_refuses_to_start_when_the_ceiling_is_exhausted(git_repo, monkeypatch):
    import forge.agents.roles.planner_loop as planner_module
    from forge.agents.roles.planner_loop import PlannerLoopError, run_planner

    set_config("openai_api_key", "sk-test")
    _exhaust("ENG-1")
    monkeypatch.setattr(planner_module, "get_provider", lambda: FakeProvider(script=[]))

    with pytest.raises(PlannerLoopError, match="spend ceiling"):
        await run_planner(user_request="Add a widget", project_path=git_repo, run_id="ENG-1")


async def test_verifier_and_qa_are_skipped_when_the_ceiling_is_exhausted(monkeypatch):
    """Both reach a model only through chat_completion_json, so one check
    covers them, and GenerationError is what the graph already catches."""
    import forge.services.generation as generation
    from forge.services.generation import GenerationError

    set_config("openai_api_key", "sk-test")
    _exhaust("ENG-1")
    monkeypatch.setattr(generation, "get_provider", lambda: FakeProvider(script=[]))

    with pytest.raises(GenerationError, match="spend ceiling"):
        await generation.chat_completion_json(messages=[{"role": "user", "content": "review"}], run_id="ENG-1")


async def test_dev_runs_normally_with_budget_remaining(git_repo, monkeypatch):
    import forge.agents.roles.dev_loop as dev_loop_module
    from forge.agents.roles.dev_loop import run_dev
    from forge.llm.types import LlmResponse, TextBlock, Usage

    set_config("openai_api_key", "sk-test")
    set_config("story_max_budget_usd", "1.00")
    _spend("ENG-1", 0.10)
    monkeypatch.setattr(
        dev_loop_module, "get_provider",
        lambda: FakeProvider(script=[LlmResponse(content=[TextBlock(text="nothing to change")], stop_reason="end_turn", usage=Usage(), model="gpt-5.1")]),
    )

    result = await run_dev(plan={}, context_bundle={}, worktree_path=git_repo, project_path=git_repo, run_id="ENG-1")

    assert result["truncated"] is False
