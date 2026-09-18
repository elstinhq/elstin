"""forge/services/usage.py — accumulation semantics of record_phase_usage
and the per-story/per-epic rollup shapes get_story_usage/get_epic_usage
return. Also forge/llm/pricing.py's fallback cost estimate math."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("forge_db")


def _seed_story(project_id: int, story_key: str = "ENG-2") -> None:
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[[story_key]])
    sm.create_story(story_key, epic_key="ENG-1", wave=0, kind="dev")


def _seed_epic(project_id: int) -> None:
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2", "ENG-3"], ["ENG-4"]])
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, kind="dev")
    sm.create_story("ENG-3", epic_key="ENG-1", wave=0, kind="dev")
    sm.create_story("ENG-4", epic_key="ENG-1", wave=1, kind="qa")
    # A sub-task: never dispatched, must never appear in usage rollups.
    sm.create_story("ENG-5", epic_key="ENG-1", wave=0, kind="dev", parent_key="ENG-2")


def test_pricing_estimate_known_model():
    from forge.llm.pricing import estimate_cost_usd

    cost = estimate_cost_usd("openai", "gpt-5.1", input_tokens=1_000_000, cache_read_tokens=0, output_tokens=0)
    assert cost == pytest.approx(1.25)


def test_pricing_estimate_unknown_model_returns_none():
    from forge.llm.pricing import estimate_cost_usd

    assert estimate_cost_usd("openai", "some-future-model", input_tokens=1000, cache_read_tokens=0, output_tokens=0) is None
    assert estimate_cost_usd(None, "gpt-5.1", input_tokens=1000, cache_read_tokens=0, output_tokens=0) is None
    assert estimate_cost_usd("openai", None, input_tokens=1000, cache_read_tokens=0, output_tokens=0) is None


def test_pricing_estimate_two_providers_same_model_name_dont_collide():
    """A same-named model under a different provider key must not
    accidentally hit the "openai" pricing row — the (provider, model) pair
    is the whole point of keying by both."""
    from forge.llm.pricing import estimate_cost_usd

    assert estimate_cost_usd("self-hosted", "gpt-5.1", input_tokens=1_000_000, cache_read_tokens=0, output_tokens=0) is None


def test_record_phase_usage_accumulates_across_invocations(project_id):
    from forge.services.usage import get_story_usage, record_phase_usage

    _seed_story(project_id)
    record_phase_usage(
        "ENG-2", phase="dev", model="gpt-5.1",
        input_tokens=1000, output_tokens=200, cache_read_tokens=50, cache_creation_tokens=10,
        cost_usd=0.05, duration_ms=1500, api_duration_ms=1200,
    )
    record_phase_usage(
        "ENG-2", phase="dev", model="gpt-5.1",
        input_tokens=500, output_tokens=100, cache_read_tokens=25, cache_creation_tokens=0,
        cost_usd=0.02, duration_ms=800, api_duration_ms=600,
    )

    usage = get_story_usage("ENG-2")
    dev_phase = next(p for p in usage["phases"] if p["phase"] == "dev")
    assert dev_phase["totals"]["input_tokens"] == 1500
    assert dev_phase["totals"]["output_tokens"] == 300
    assert dev_phase["totals"]["cache_read_tokens"] == 75
    assert dev_phase["totals"]["cache_creation_tokens"] == 10
    assert dev_phase["totals"]["cost_usd"] == pytest.approx(0.07)
    assert dev_phase["totals"]["duration_ms"] == 2300
    assert dev_phase["totals"]["api_duration_ms"] == 1800
    assert dev_phase["totals"]["invocation_count"] == 2
    assert dev_phase["totals"]["has_estimated_cost"] is False


def test_record_phase_usage_no_run_id_is_a_noop():
    from forge.services.usage import record_phase_usage

    # Must not raise and must not create a row for an empty run_id.
    record_phase_usage("", phase="dev", model="gpt-5.1", input_tokens=10)
    record_phase_usage(None, phase="dev", model="gpt-5.1", input_tokens=10)


def test_record_phase_usage_falls_back_to_estimated_cost_when_sdk_reports_none(project_id):
    from forge.services.usage import get_story_usage, record_phase_usage

    _seed_story(project_id)
    record_phase_usage(
        "ENG-2", phase="planner", model="gpt-5.1-mini",
        input_tokens=1_000_000, output_tokens=0, cache_read_tokens=0, cache_creation_tokens=0,
        cost_usd=None, duration_ms=100,
    )

    usage = get_story_usage("ENG-2")
    planner_phase = next(p for p in usage["phases"] if p["phase"] == "planner")
    assert planner_phase["totals"]["cost_usd"] == pytest.approx(0.25)
    assert planner_phase["totals"]["has_estimated_cost"] is True
    assert planner_phase["models"][0]["cost_source"] == "estimated"


def test_record_phase_usage_sdk_source_outranks_estimated(project_id):
    from forge.services.usage import get_story_usage, record_phase_usage

    _seed_story(project_id)
    # First an SDK-reported cost, then an estimated one (e.g. a later
    # invocation without SDK cost) — the row's cost_source must stay "sdk".
    record_phase_usage("ENG-2", phase="verifier", model="gpt-5.1", input_tokens=100, cost_usd=0.01)
    record_phase_usage("ENG-2", phase="verifier", model="gpt-5.1", input_tokens=100, cost_usd=None)

    usage = get_story_usage("ENG-2")
    verifier_phase = next(p for p in usage["phases"] if p["phase"] == "verifier")
    assert verifier_phase["models"][0]["cost_source"] == "sdk"


def test_get_story_usage_for_unknown_story_returns_empty_shape():
    from forge.services.usage import get_story_usage

    usage = get_story_usage("NO-SUCH-STORY")
    assert usage == {
        "story_key": "NO-SUCH-STORY",
        "run_id": None,
        "phases": [],
        "totals": {
            "input_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
            "cost_usd": None, "duration_ms": 0, "api_duration_ms": None, "invocation_count": 0,
            "has_estimated_cost": False, "has_unknown_cost": False,
        },
    }


def test_get_story_usage_with_no_recorded_usage_is_not_a_zero_cost_run(project_id):
    from forge.services.usage import get_story_usage

    _seed_story(project_id)
    usage = get_story_usage("ENG-2")
    assert usage["phases"] == []
    assert usage["totals"]["cost_usd"] is None


def test_get_epic_usage_groups_by_wave_and_excludes_subtasks(project_id):
    from forge.services.usage import get_epic_usage, record_phase_usage

    _seed_epic(project_id)
    record_phase_usage("ENG-2", phase="dev", model="gpt-5.1", input_tokens=1000, output_tokens=500, cost_usd=0.10, duration_ms=1000)
    record_phase_usage("ENG-3", phase="dev", model="gpt-5.1", input_tokens=2000, output_tokens=1000, cost_usd=0.20, duration_ms=2000)
    record_phase_usage("ENG-4", phase="qa", model="gpt-5.1", input_tokens=500, output_tokens=250, cost_usd=0.05, duration_ms=500)
    # ENG-5 is a sub-task (parent_key=ENG-2) — recording usage against its
    # own key must never surface in the epic rollup.
    record_phase_usage("ENG-5", phase="dev", model="gpt-5.1", input_tokens=999, cost_usd=99.0)

    usage = get_epic_usage("ENG-1")

    story_keys = {s["story_key"] for s in usage["stories"]}
    assert story_keys == {"ENG-2", "ENG-3", "ENG-4"}

    assert [level["wave"] for level in usage["levels"]] == [0, 1]
    wave0 = usage["levels"][0]
    assert {s["story_key"] for s in wave0["stories"]} == {"ENG-2", "ENG-3"}
    assert wave0["totals"]["cost_usd"] == pytest.approx(0.30)
    assert wave0["totals"]["input_tokens"] == 3000

    wave1 = usage["levels"][1]
    assert wave1["stories"][0]["story_key"] == "ENG-4"
    assert wave1["totals"]["cost_usd"] == pytest.approx(0.05)

    assert usage["totals"]["cost_usd"] == pytest.approx(0.35)
    assert usage["totals"]["input_tokens"] == 3500


def test_get_epic_usage_for_epic_with_no_usage_recorded(project_id):
    from forge.services.usage import get_epic_usage

    _seed_epic(project_id)
    usage = get_epic_usage("ENG-1")
    assert usage["totals"]["cost_usd"] is None
    assert all(story["phases"] == [] for story in usage["stories"])
