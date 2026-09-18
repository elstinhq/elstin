"""Runs the real ported LangGraph graph against a real (testcontainers)
Postgres checkpointer. The graph structure, node wiring, and Postgres
checkpointer are all real production code; forge/agents/roles/*_loop.py's
actual LLM-provider calls (via forge/agents/loop.py and
forge/llm/registry.py) are replaced by the
`fake_agents` fixture (conftest.py) so these tests run with no network
access and no API key — they're proving the checkpointer/interrupt/resume
mechanics, not agent output quality.

The key thing this proves: a run parked at the planner-approval interrupt
survives a *process restart* — a fresh AsyncPostgresSaver connection (a
brand new `checkpointer_from_url` context, standing in for the daemon
having crashed and been restarted) can resume it by thread_id alone. SQLite
in a container wouldn't durably prove this the same way; Postgres does.
"""
from __future__ import annotations

import uuid

import pytest

from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.state import initial_state

# forge_db: graph.py's node functions call append_activity() directly
# (outside the fake_agents swap), which now persists to Postgres — needs
# DATABASE_URL set, which only the forge_db fixture does.
pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("fake_agents", "forge_db")]


async def test_run_parks_at_planner_gate_and_resumes_after_reconnect(postgres_url: str):
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")

    # First "process": run up to the planner-approval interrupt.
    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=True)
        result = await graph.ainvoke(state, config)
        assert result["status"] == "awaiting_approval"

        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("dev",)  # parked before dev, exactly at the interrupt

    # Second "process": a brand-new checkpointer connection (simulating a
    # daemon restart), resuming purely from thread_id — no in-memory state
    # carried over from the first block.
    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=True)

        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("dev",)  # durably persisted, not lost with the old connection

        # In production, the daemon calls forge.tools.worktree.create_worktree
        # between plan-approval and resume, then writes worktree_path onto the
        # checkpoint — the graph itself never creates a worktree.
        await graph.aupdate_state(config, {"worktree_path": "/repo-worktrees/eng-1"})

        final = await graph.ainvoke(None, config)
        assert final["status"] == "completed"
        # A dev story ends at the verifier. QA is not a per-story stage any
        # more — it runs once per epic as its own ticket, so nothing on this
        # path should have produced a report.
        assert final["qa_report"] == {}


async def test_gate_off_runs_straight_through_without_parking(postgres_url: str):
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    # No interrupt with the gate off, so planner -> dev happens inside one
    # ainvoke call — nothing between them to inject worktree_path, so it has
    # to be present in the initial state instead (as it would be for a
    # workspace_mode="in_place" run, or a resume-from-approval call).
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)
        assert result["status"] == "completed"


async def test_dev_provider_error_ends_the_run_failed_not_uncaught(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """dev_node must catch DevLoopError and return status="failed"
    rather than let it propagate — nothing upstream of the graph (the
    daemon's poll_once loop) catches a per-story exception, so an uncaught
    one here would silently abort the whole poll cycle forever instead of
    landing on a retryable "failed" story (see forge/daemon/main.py's
    poll_once, which has no try/except around handle_intent)."""
    import forge.orchestration.graph as graph_module
    from forge.agents.roles.dev_loop import DevLoopError

    async def failing_dev(**kwargs) -> dict:
        raise DevLoopError("simulated provider failure")

    monkeypatch.setattr(graph_module, "run_dev", failing_dev)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)  # must not raise
        assert result["status"] == "failed"
        assert "simulated provider failure" in result["error"]


def _qa_state(thread_id: str) -> dict:
    state = initial_state(thread_id, "Validate implemented end-to-end flow", "/repo")
    state["worktree_path"] = "/repo-worktrees/proj-3722"
    state["story_kind"] = "qa"
    state["qa_context"] = {
        "qa_story": {"jira_key": "PROJ-3722", "summary": "Validate implemented end-to-end flow"},
        "epic": {"jira_key": "PROJ-3480", "summary": "Multi-item type selection"},
        "dev_stories": [{"jira_key": "PROJ-3481", "summary": "Select multiple item types"}],
        "branch_diff": {"base": "main", "branch": "forge/epic/PROJ-3480", "changed_files": [], "diff": ""},
    }
    return state


async def test_qa_story_skips_planner_and_dev_entirely(postgres_url: str):
    """The core of the redesign: a validation ticket must not be planned or
    implemented. With the plan-approval gate ON it must also not park at
    the planner interrupt — there is no plan to approve."""
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=True)
        result = await graph.ainvoke(_qa_state(thread_id), config)

    assert result["status"] == "qa_completed"
    assert result["qa_report"]["verdict"] == "pass"
    # Nothing on the dev path ran.
    assert result["plan"] == {}
    assert result["file_changes"] == []
    assert result["verifier_report"] == {}
    roles = {m["role"] for m in result["messages"]}
    assert roles == {"qa"}


async def test_qa_story_fails_the_run_when_the_merged_branch_suite_fails(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """A failing full suite is deterministic evidence, so it fails the run.
    The report still comes back, because that is what a human needs to read
    in exactly this case."""
    async def _suite_fails(**kwargs):
        return {
            "qa_report": {
                "verdict": "pass",  # the model's own opinion is overridden by evidence
                "full_test_suite": {"ran": True, "passed": False, "profiles": {}},
            },
            "messages": [],
        }

    import forge.orchestration.graph as graph_module

    monkeypatch.setattr(graph_module, "run_epic_qa", _suite_fails)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(_qa_state(thread_id), config)

    assert result["status"] == "failed"
    assert result["error"] == "Full test suite failed on the integration branch"
    assert result["qa_report"]["full_test_suite"]["passed"] is False


async def test_qa_story_without_context_fails_rather_than_reviewing_nothing(postgres_url: str):
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = _qa_state(thread_id)
    state["qa_context"] = {}

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "failed"
    assert result["error"] == "QA context not assembled"


async def test_verifier_failure_retries_dev_up_to_max_iterations(postgres_url: str, monkeypatch: pytest.MonkeyPatch):
    async def _always_fails(**kwargs):
        return {"verifier_report": {"passed": False, "fake": True}, "diffs": [], "messages": []}

    import forge.orchestration.graph as graph_module

    monkeypatch.setattr(graph_module, "run_verifier", _always_fails)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["max_iterations"] = 2
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)
        assert result["status"] == "failed"
        assert result["error"] == "Verifier retries exhausted"
        assert result["iteration"] == 2
