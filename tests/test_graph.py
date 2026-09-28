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


async def test_answering_a_clarification_resumes_into_planner_not_dev(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Regression for MKT2-858. route_after_planner only special-cased
    "failed"; every other status, including "awaiting_clarification",
    routed to "dev" — computed once, right when planner_node returns and
    interrupt_after=["planner"] pauses the graph, and frozen into the
    checkpoint's `next` pointer from that point on.

    forge/daemon/main.py's resume patches in a human's answer
    (clarification_answers) via `aupdate_state` before resuming, exactly
    like this test does — but `aupdate_state` recomputes `next` by
    re-running the outgoing conditional edge, so the fix has to live in
    that edge function, not in the patch. Confirmed against the unfixed
    code: dev ran directly on a nearly-empty plan (no files, no
    acceptance_criteria) and the run failed with "nothing to verify"."""
    import forge.orchestration.graph as graph_module

    calls: list[list[dict] | None] = []
    earlier: list[bool] = []

    async def planner_needs_one_answer(**kwargs) -> dict:
        answers = kwargs.get("clarification_answers")
        calls.append(answers)
        earlier.append(kwargs.get("answers_from_earlier_run"))
        if not answers:
            return {
                "questions": [{
                    "id": "q0", "question": "Which store should own this?", "context": None,
                    "options": [{"id": "A", "label": "Redux", "description": None}],
                }],
                "context_bundle": {}, "plan": {}, "acceptance_criteria": [], "messages": [],
            }
        return {
            "questions": [],
            "context_bundle": {},
            "plan": {"summary": "answered", "files_to_create": [{"path": "a.py", "purpose": "x"}], "files_to_modify": []},
            "acceptance_criteria": ["it works"],
            "messages": [],
        }

    monkeypatch.setattr(graph_module, "run_planner", planner_needs_one_answer)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=True)
        result = await graph.ainvoke(state, config)
        assert result["status"] == "awaiting_clarification"
        assert len(calls) == 1

        # Same shape forge/daemon/main.py's resume patch applies from
        # stories.clarification_answers once a human answers.
        answer = [{"question": "Which store should own this?", "option_label": "Redux", "option_description": None}]
        await graph.aupdate_state(config, {"clarification_answers": answer})

        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("planner",)  # recomputed, not stuck on the original "dev"

        final = await graph.ainvoke(None, config)

    assert len(calls) == 2
    assert calls[1] == answer  # the second planner call actually saw it
    assert earlier[1] is False  # as this run's round trip, not a decision from an earlier run
    assert final["status"] == "awaiting_approval"  # a real plan now, parked for its own review


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


# --- Tier 1 deterministic gate -------------------------------------------


def _failing_gate(*, blockers: list[str]) -> dict:
    return {
        "passed": False,
        "issues": [{"severity": "blocker", "file": None, "reason": b, "details": f"{b} detail"} for b in blockers],
        "required_fixes": blockers,
    }


async def test_gate_failure_routes_back_to_dev_with_feedback_and_never_enters_the_verifier(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Tier 1 fails once (code doesn't compile), dev gets one targeted
    fix message on top of its existing history, Tier 1 then passes, and
    only THEN does the verifier run — exactly once."""
    import forge.orchestration.graph as graph_module

    gate_results = [_failing_gate(blockers=["alpha: analyze failed"]), {"passed": True, "issues": [], "required_fixes": []}]
    dev_calls: list[dict] = []
    verifier_calls: list[dict] = []

    async def scripted_gate(**kwargs) -> dict:
        return gate_results.pop(0)

    async def recording_dev(**kwargs) -> dict:
        dev_calls.append(kwargs)
        return {"file_changes": [{"path": "a.py"}], "truncated": False, "messages": [], "dev_history": [{"role": "user", "content": [{"type": "text", "text": "earlier"}]}], "dev_build_runner": {}}

    async def recording_verifier(**kwargs) -> dict:
        verifier_calls.append(kwargs)
        return {"verifier_report": {"passed": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_deterministic_gate", scripted_gate)
    monkeypatch.setattr(graph_module, "run_dev", recording_dev)
    monkeypatch.setattr(graph_module, "run_verifier", recording_verifier)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "completed"
    assert len(dev_calls) == 2
    assert dev_calls[0]["gate_feedback"] is None
    feedback = dev_calls[1]["gate_feedback"]
    assert feedback.startswith("Deterministic checks FAILED with 1 problem(s)")
    assert "alpha: analyze failed" in feedback
    assert "(fix attempt 1/3)" in feedback
    # The fix round continues the same conversation rather than starting cold.
    assert dev_calls[1]["prior_history"] == [{"role": "user", "content": [{"type": "text", "text": "earlier"}]}]
    # The verifier ran once, after the gate passed, and got the gate's report to reuse.
    assert len(verifier_calls) == 1
    assert verifier_calls[0]["precomputed_gate"] == {"passed": True, "issues": [], "required_fixes": []}
    assert result["gate_feedback"] is None  # consumed
    assert result["gate_attempts"] == 0  # reset by the pass


async def test_gate_feedback_says_nothing_was_written_only_while_the_worktree_is_untouched(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Round one writes nothing, round two writes a file but not the rest:
    dev is told it wrote nothing the first time and not the second."""
    import forge.orchestration.graph as graph_module

    unwritten = {
        "passed": False,
        "issues": [{"severity": "blocker", "file": "b.py", "reason": "Dev did not modify target file: b.py", "kind": "unmodified_target"}],
        "required_fixes": ["Dev did not modify target file: b.py"],
    }
    gate_results = [unwritten, unwritten, {"passed": True, "issues": [], "required_fixes": []}]
    dev_results = [
        {"file_changes": [], "truncated": True, "messages": [], "dev_history": [], "dev_build_runner": {}},
        {"file_changes": [{"path": "a.py"}], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}},
        {"file_changes": [{"path": "a.py"}, {"path": "b.py"}], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}},
    ]
    dev_calls: list[dict] = []

    async def scripted_gate(**kwargs) -> dict:
        return gate_results.pop(0)

    async def scripted_dev(**kwargs) -> dict:
        dev_calls.append(kwargs)
        return dev_results.pop(0)

    async def passing_verifier(**kwargs) -> dict:
        return {"verifier_report": {"passed": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_deterministic_gate", scripted_gate)
    monkeypatch.setattr(graph_module, "run_dev", scripted_dev)
    monkeypatch.setattr(graph_module, "run_verifier", passing_verifier)

    thread_id = str(uuid.uuid4())
    state = initial_state(thread_id, "Add a widget", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"
    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await graph.ainvoke(state, {"configurable": {"thread_id": thread_id}})

    assert len(dev_calls) == 3
    assert "wrote NO files" in dev_calls[1]["gate_feedback"]
    assert "Change these with edit_file_tool" in dev_calls[1]["gate_feedback"]
    assert "wrote NO files" not in dev_calls[2]["gate_feedback"]


async def test_gate_failing_every_time_fails_the_run_without_a_verifier_call(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    import forge.orchestration.graph as graph_module

    verifier_calls: list[dict] = []

    async def always_failing_gate(**kwargs) -> dict:
        return _failing_gate(blockers=["Missing planned new file: lib/x.dart"])

    async def recording_verifier(**kwargs) -> dict:
        verifier_calls.append(kwargs)
        return {"verifier_report": {"passed": True}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_deterministic_gate", always_failing_gate)
    monkeypatch.setattr(graph_module, "run_verifier", recording_verifier)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"
    state["max_gate_attempts"] = 2

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "failed"
    assert result["gate_attempts"] == 2
    assert result["error"].startswith("Deterministic gate failed after 2 fix attempt(s)")
    assert "Missing planned new file: lib/x.dart" in result["error"]
    assert result["verifier_report"]["passed"] is False  # the gate report, for the daemon's readers
    assert verifier_calls == []  # never paid for a review of code that doesn't build
    assert result["iteration"] == 0  # the verifier's own budget was never touched


async def test_gate_runs_after_every_dev_pass_including_verifier_retries(postgres_url: str, monkeypatch: pytest.MonkeyPatch):
    """dev -> gate -> verifier(fail) -> dev -> gate -> verifier(pass)."""
    import forge.orchestration.graph as graph_module

    gate_calls = {"n": 0}
    verdicts = [False, True]

    async def counting_gate(**kwargs) -> dict:
        gate_calls["n"] += 1
        return {"passed": True, "issues": [], "required_fixes": []}

    async def scripted_verifier(**kwargs) -> dict:
        passed = verdicts.pop(0)
        return {"verifier_report": {"passed": passed, "required_fixes": [] if passed else ["nope"]}, "diffs": [], "messages": []}

    monkeypatch.setattr(graph_module, "run_deterministic_gate", counting_gate)
    monkeypatch.setattr(graph_module, "run_verifier", scripted_verifier)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "completed"
    assert gate_calls["n"] == 2
    assert result["iteration"] == 1


# --- a bad model reply fails the story, not the poll cycle -------------


async def test_verifier_generation_error_fails_the_story_instead_of_escaping(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """The verifier and QA nodes used to catch nothing. Since the daemon's
    poll loop has no per-story handler, an unparseable model reply escaped
    all the way to run_forever's blanket except, abandoning every epic
    queued behind this one for the rest of the cycle."""
    import forge.orchestration.graph as graph_module
    from forge.services.generation import GenerationError

    async def verifier_gets_prose(**kwargs) -> dict:
        raise GenerationError("model reply contained no JSON object")

    monkeypatch.setattr(graph_module, "run_verifier", verifier_gets_prose)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)  # must not raise

    assert result["status"] == "failed"
    assert "Verifier review could not be completed" in result["error"]


async def test_qa_generation_error_fails_the_story_instead_of_escaping(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    import forge.orchestration.graph as graph_module
    from forge.services.generation import GenerationError

    async def qa_provider_down(**kwargs) -> dict:
        raise GenerationError("epic QA failed via openai: service unavailable")

    monkeypatch.setattr(graph_module, "run_epic_qa", qa_provider_down)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(_qa_state(thread_id), config)  # must not raise

    assert result["status"] == "failed"
    assert "Epic QA review could not be completed" in result["error"]


async def test_gate_and_verifier_populate_the_run_outcome_counters(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """The counters that answer "is the Tier 1 gate earning its keep":
    a high gate_failures with a low verifier_rejections means it is
    catching broken builds before the expensive reviewer sees them.
    Recorded by the graph nodes themselves, so this proves the wiring, not
    just the service (see forge/db/models/run_outcome.py)."""
    import forge.orchestration.graph as graph_module
    from forge.services.usage import get_run_outcome

    gate_results = [_failing_gate(blockers=["alpha: analyze failed"]), {"passed": True, "issues": [], "required_fixes": []}]

    async def scripted_gate(**kwargs) -> dict:
        return gate_results.pop(0)

    monkeypatch.setattr(graph_module, "run_deterministic_gate", scripted_gate)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "completed"

    outcome = get_run_outcome(thread_id)
    assert outcome is not None
    # Gate ran twice: failed, then passed after the fix round.
    assert outcome["gate_runs"] == 2
    assert outcome["gate_failures"] == 1
    # The reviewer only ever saw the build that had already cleared Tier 1.
    assert outcome["verifier_runs"] == 1
    assert outcome["verifier_rejections"] == 0


async def test_a_failed_verifier_is_still_counted_as_a_run(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Regression: the counter sat after the error handler returned, so the
    one case worth measuring — how often a cheap model fails to return
    usable JSON — was the case the table omitted."""
    import forge.orchestration.graph as graph_module
    from forge.services.generation import GenerationError
    from forge.services.usage import get_run_outcome

    async def verifier_gets_prose(**kwargs) -> dict:
        raise GenerationError("model reply contained no JSON object")

    monkeypatch.setattr(graph_module, "run_verifier", verifier_gets_prose)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Add a widget to the cart", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "failed"
    outcome = get_run_outcome(thread_id)
    assert outcome["verifier_runs"] == 1
    assert outcome["verifier_rejections"] == 0  # it never got to judge


async def test_a_run_with_nothing_to_verify_fails_without_sending_dev_back(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """A verifier rejection normally means "dev, fix it and come back".
    This one cannot: dev may only write files the plan names, and the
    rejection says the plan names none. Routing it back would buy three
    more no-op passes at model prices before the run failed anyway."""
    import forge.orchestration.graph as graph_module

    dev_passes = {"count": 0}
    fake_dev = graph_module.run_dev

    async def counting_dev(**kwargs) -> dict:
        dev_passes["count"] += 1
        return await fake_dev(**kwargs)

    async def verifier_finds_nothing(**kwargs) -> dict:
        return {
            "verifier_report": {
                "passed": False,
                "nothing_to_verify": True,
                "required_fixes": ["Nothing to verify: the plan named no files to create or modify."],
            },
            "diffs": [],
            "messages": [],
        }

    monkeypatch.setattr(graph_module, "run_dev", counting_dev)
    monkeypatch.setattr(graph_module, "run_verifier", verifier_finds_nothing)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Show the currency next to each price", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "failed"
    assert "Nothing to verify" in result["error"]
    assert dev_passes["count"] == 1  # the pass that produced nothing, and no retry of it


async def test_a_planner_that_cannot_produce_a_usable_plan_fails_the_run(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """A PlannerLoopError used to propagate out of the run, and the
    daemon's unwind restored the story to "pending" — which the next poll
    dispatched again, re-planning and re-failing every cycle forever. It
    has to end as a failure a human sees, and it must not reach dev, which
    would be scoped to write the files of a plan that does not exist."""
    import forge.orchestration.graph as graph_module
    from forge.agents.roles.planner_loop import PlannerLoopError

    dev_passes = {"count": 0}
    fake_dev = graph_module.run_dev

    async def counting_dev(**kwargs) -> dict:
        dev_passes["count"] += 1
        return await fake_dev(**kwargs)

    async def planner_cannot_converge(**kwargs) -> dict:
        raise PlannerLoopError("Planner agent loop failed after 3 attempt(s): plan gate rejected the plan")

    monkeypatch.setattr(graph_module, "run_dev", counting_dev)
    monkeypatch.setattr(graph_module, "run_planner", planner_cannot_converge)

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = initial_state(thread_id, "Show the currency next to each price", "/repo")
    state["worktree_path"] = "/repo-worktrees/eng-1"

    async with checkpointer_from_url(postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        result = await graph.ainvoke(state, config)

    assert result["status"] == "failed"
    assert "plan gate rejected the plan" in result["error"]
    assert dev_passes["count"] == 0
