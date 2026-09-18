"""forge/agents/roles/explorer.py — the planner's parallel read-only recon
sub-agents. Faked at the forge/llm/ boundary (a scripted provider handed
straight in), so the real loop, the real read-only tools and the real
prompt assembly all run.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field

import pytest

from forge.agents.roles.explorer import (
    _SCOPE_JSON_SCHEMA,
    EXPLORER_SYSTEM,
    _explorer_budget,
    gather_exploration,
    run_explorer,
    scope_investigation,
)
from forge.llm.base import Capabilities
from forge.llm.errors import LlmBadRequestError
from forge.llm.types import LlmRequest, LlmResponse, TextBlock, ToolCallBlock, Usage


@dataclass
class FakeProvider:
    script: list[LlmResponse | Exception]
    name: str = "openai"
    capabilities: Capabilities = field(default_factory=Capabilities)
    calls: list[LlmRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        with self.lock:
            self.calls.append(request)
            item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text(text: str) -> LlmResponse:
    return LlmResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5), model="m")


def _scope(*areas: dict) -> LlmResponse:
    payload = {"areas": list(areas)}
    return LlmResponse(
        content=[TextBlock(text=json.dumps(payload))], stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5), model="m", structured_output=payload,
    )


def _area(name="ui", question="How is it rendered?", hints=None) -> dict:
    return {"name": name, "question": question, "path_hints": hints or []}


# --- the scope schema --------------------------------------------------


def test_scope_schema_satisfies_strict_structured_output_rules():
    """Same constraint as the plan schema: strict json_schema mode requires
    every property in `required` at every level with additionalProperties
    false. A schema that breaks this is rejected at request time, i.e. on a
    real run and in no test that fakes the provider."""

    def check(node, path="root"):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
            assert set(node.get("properties", {})) == set(node.get("required", [])), f"{path}: required != properties"
            for key, value in node["properties"].items():
                check(value, f"{path}.{key}")
        elif node.get("type") == "array":
            check(node.get("items", {}), f"{path}[]")

    check(_SCOPE_JSON_SCHEMA)


# --- scoping -----------------------------------------------------------


async def test_scoping_asks_for_no_tools_and_one_turn():
    provider = FakeProvider(script=[_scope(_area())])

    areas, _ = await scope_investigation(provider=provider, model="m", user_request="Add a widget")

    assert [a["name"] for a in areas] == ["ui"]
    assert provider.calls[0].tools == []
    assert provider.calls[0].response_format is not None


async def test_scoping_caps_the_number_of_areas():
    """A vague ticket will happily ask for a dozen readers."""
    provider = FakeProvider(script=[_scope(*[_area(name=f"area{i}") for i in range(9)])])

    areas, _ = await scope_investigation(provider=provider, model="m", user_request="Rewrite everything", max_agents=3)

    assert len(areas) == 3


async def test_scoping_drops_areas_with_no_question():
    provider = FakeProvider(script=[_scope({"name": "empty", "question": "  ", "path_hints": []}, _area())])

    areas, _ = await scope_investigation(provider=provider, model="m", user_request="Add a widget")

    assert [a["name"] for a in areas] == ["ui"]


async def test_scoping_failure_returns_no_areas_rather_than_raising():
    provider = FakeProvider(script=[LlmBadRequestError("schema rejected")])

    areas, _ = await scope_investigation(provider=provider, model="m", user_request="Add a widget")

    assert areas == []


# --- one explorer ------------------------------------------------------


async def test_explorer_gets_only_read_only_tools(git_repo):
    provider = FakeProvider(script=[_text("Rendered in `a.py:1`.")])

    findings = await run_explorer(provider=provider, model="m", project_path=git_repo, area=_area())

    assert findings == "Rendered in `a.py:1`."
    offered = {t.name for t in provider.calls[0].tools}
    assert offered == {"read_file_tool", "grep_tool", "find_files_tool"}
    # No write, edit, shell or spawn path exists for a sub-agent.
    assert not any("write" in name or "edit" in name or "command" in name or "agent" in name for name in offered)


async def test_explorer_prompt_is_self_contained(git_repo):
    """An explorer sees its question and its hints, never the ticket or the
    other explorers — that isolation is what keeps its context small."""
    provider = FakeProvider(script=[_text("done")])

    await run_explorer(
        provider=provider, model="m", project_path=git_repo,
        area=_area(question="How does auth work?", hints=["lib/auth.py"]),
    )

    prompt = provider.calls[0].messages[0].content[0].text
    assert "How does auth work?" in prompt
    assert "lib/auth.py" in prompt
    assert provider.calls[0].system == EXPLORER_SYSTEM


async def test_explorer_turn_cap_is_applied(git_repo):
    """A sub-agent that keeps reading has to be stopped without stopping
    the plan; a capped run still returns whatever it had."""
    provider = FakeProvider(script=[
        LlmResponse(content=[ToolCallBlock(id="c1", name="grep_tool", arguments={"pattern": "x"})], stop_reason="tool_calls", usage=Usage(), model="m"),
        LlmResponse(content=[TextBlock(text="partial finding")], stop_reason="tool_calls", usage=Usage(), model="m"),
    ])

    findings = await run_explorer(provider=provider, model="m", project_path=git_repo, area=_area(), max_turns=2)

    assert findings == "partial finding"
    assert len(provider.calls) == 2


async def test_explorer_failure_returns_empty_rather_than_raising(git_repo):
    provider = FakeProvider(script=[LlmBadRequestError("nope")])

    assert await run_explorer(provider=provider, model="m", project_path=git_repo, area=_area()) == ""


async def test_oversized_report_is_truncated(git_repo):
    """An explorer that ignores "compact" would otherwise defeat the point."""
    provider = FakeProvider(script=[_text("x" * 20_000)])

    findings = await run_explorer(provider=provider, model="m", project_path=git_repo, area=_area())

    assert len(findings) < 20_000
    assert findings.endswith("[report truncated]")


# --- the fan-out -------------------------------------------------------


async def test_explorers_run_concurrently(git_repo):
    """Two explorers each block on a barrier that only releases when BOTH
    are inside their provider call. Sequential fan-out would deadlock and
    time out instead."""
    barrier = threading.Barrier(2, timeout=5)

    @dataclass
    class BarrierProvider(FakeProvider):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            if request.tools:  # an explorer call, not the scoping call
                await asyncio.to_thread(barrier.wait)
            return await super().complete(request)

    provider = BarrierProvider(script=[
        _scope(_area(name="a"), _area(name="b")),
        _text("finding a"),
        _text("finding b"),
    ])

    block, _ = await gather_exploration(provider=provider, model="m", project_path=git_repo, user_request="go")

    assert "finding a" in block
    assert "finding b" in block


async def test_findings_block_names_each_area_and_its_question(git_repo):
    provider = FakeProvider(script=[
        _scope(_area(name="cart UI", question="How is the cart rendered?")),
        _text("In `cart.py:12`."),
    ])

    block, _ = await gather_exploration(provider=provider, model="m", project_path=git_repo, user_request="go")

    assert "REPOSITORY INVESTIGATION" in block
    assert "### cart UI" in block
    assert "Question: How is the cart rendered?" in block
    assert "In `cart.py:12`." in block
    # The planner is told these are observations, not requirements.
    assert "not requirements" in block


async def test_no_areas_means_no_block_and_no_explorer_calls(git_repo):
    provider = FakeProvider(script=[_scope()])

    block, _ = await gather_exploration(provider=provider, model="m", project_path=git_repo, user_request="go")

    assert block == ""
    assert len(provider.calls) == 1


async def test_every_explorer_failing_yields_no_block(git_repo):
    provider = FakeProvider(script=[_scope(_area()), LlmBadRequestError("nope")])

    block, _ = await gather_exploration(provider=provider, model="m", project_path=git_repo, user_request="go")

    assert block == ""


# --- budget split ------------------------------------------------------


@pytest.mark.parametrize(
    "planner_budget,agents,expected",
    [
        # Unbounded stays unbounded, as zero does everywhere else in forge.
        (0.0, 3, 0.0),
        # Half the cap is reserved for the plan loop, the rest is split.
        (1.20, 3, 0.20),
        (1.00, 1, 0.50),
        (1.00, 0, 0.50),
    ],
)
def test_explorer_budget_split(planner_budget, agents, expected):
    """Without a split, each explorer carries the planner's WHOLE cap, so
    three of them could spend three times what the operator allowed and the
    plan loop would still be owed its share."""
    assert _explorer_budget(planner_budget, agents) == pytest.approx(expected)


async def test_max_agents_zero_means_no_explorers():
    """Regression: the cap was checked after appending, so "0 = off" (what
    the config field promises) still dispatched one."""
    provider = FakeProvider(script=[_scope(_area(), _area(name="b"))])

    areas, _ = await scope_investigation(provider=provider, model="m", user_request="go", max_agents=0)

    assert areas == []
