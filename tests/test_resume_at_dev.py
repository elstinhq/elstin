"""Retry for a run that failed in dev: pick up at dev with the plan already made.

The real graph, the real Postgres checkpointer and real worktrees; only the
agents behind the graph's entry points are fakes (`fake_agents`). What matters
is what the checkpoint says afterwards — whether the planner is called again —
so each test fails a run at some node, calls `arm_resume_at_dev`, and reads
the outcome off the graph.

A model provider that runs out of credits is the case this exists for: planning
was paid for and is still good, dev failed on its first call, and a full
redispatch would pay for the planner a second time.
"""
from __future__ import annotations

import shutil
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import forge.orchestration.graph as graph_module
from forge.agents.roles.dev_loop import DevLoopError
from forge.agents.roles.planner_loop import PlannerLoopError
from forge.daemon.main import (
    _apply_resume_patch, _ended_failed_in_dev, arm_resume_after_failure, arm_resume_at_dev,
)
from forge.db.engine import session_scope
from forge.db.models import Story
from forge.llm.errors import ProviderLimit
from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.state import initial_state
from forge.services.generation import GenerationError
from forge.services.run_activity import append_activity
from forge.state.manager import StateManager
from forge.tools.worktree import create_worktree

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("fake_agents", "forge_db")]

STORY = "ENG-2"


class Run:
    """One story with a real worktree, and helpers to fail and re-enter its run."""

    def __init__(self, postgres_url: str, sm: StateManager, worktree: dict) -> None:
        self.postgres_url = postgres_url
        self.sm = sm
        self.worktree_path = worktree["worktree_path"]
        self.config = {"configurable": {"thread_id": STORY}}
        self.planner_calls = 0
        self.dev_calls = 0

    async def start(self, **state_overrides) -> dict:
        state = initial_state(STORY, "Add a widget to the cart", "/repo")
        state["worktree_path"] = self.worktree_path
        state.update(state_overrides)
        async with checkpointer_from_url(self.postgres_url) as checkpointer:
            graph = compile_graph(checkpointer, require_plan_approval=False)
            return await graph.ainvoke(state, self.config)

    async def arm(self) -> bool:
        return await arm_resume_at_dev(self.sm, self.sm.get_epic("ENG-1"), self.sm.get_story(STORY))

    async def snapshot(self):
        async with checkpointer_from_url(self.postgres_url) as checkpointer:
            return await compile_graph(checkpointer, require_plan_approval=False).aget_state(self.config)

    async def resume(self) -> dict:
        """What the daemon's resume does: re-supply the worktree, then continue."""
        async with checkpointer_from_url(self.postgres_url) as checkpointer:
            graph = compile_graph(checkpointer, require_plan_approval=False)
            await _apply_resume_patch(graph, self.config, {"worktree_path": self.worktree_path})
            return await graph.ainvoke(None, self.config)


@pytest.fixture()
def run(postgres_url: str, project_id: int, git_repo: str, tmp_path, fake_agents, monkeypatch: pytest.MonkeyPatch) -> Run:
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story(STORY, epic_key="ENG-1", wave=0, summary="Add a widget", kind="dev")
    worktree = create_worktree(git_repo, story_key=STORY, summary="Add a widget", worktrees_base_dir=str(tmp_path / "wt"))
    sm.record_story_worktree(
        STORY, repo_label="widgets", repo_path=git_repo,
        worktree_path=worktree["worktree_path"], branch=worktree["branch"], is_default=True,
    )
    the_run = Run(postgres_url, sm, worktree)

    fake_planner, fake_dev = graph_module.run_planner, graph_module.run_dev

    async def counting_planner(**kwargs):
        the_run.planner_calls += 1
        return await fake_planner(**kwargs)

    async def dev_that_counts(**kwargs):
        the_run.dev_calls += 1
        return await fake_dev(**kwargs)

    monkeypatch.setattr(graph_module, "run_planner", counting_planner)
    monkeypatch.setattr(graph_module, "run_dev", dev_that_counts)
    return the_run


def _dev_fails_once(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev's first call fails the way an out-of-credits provider does; later ones work."""
    working_dev = graph_module.run_dev

    async def flaky_dev(**kwargs):
        if run.dev_calls == 0:
            run.dev_calls += 1
            raise DevLoopError("Error code: 402 - This request requires more credits")
        return await working_dev(**kwargs)

    monkeypatch.setattr(graph_module, "run_dev", flaky_dev)


async def test_a_run_that_failed_in_dev_continues_at_dev_without_planning_again(run: Run, monkeypatch):
    _dev_fails_once(run, monkeypatch)

    failed = await run.start()
    assert failed["status"] == "failed"
    assert run.planner_calls == 1

    assert await run.arm() is True
    armed = await run.snapshot()
    assert armed.next == ("dev",)  # not finished any more: waiting on dev
    assert armed.values["status"] == "developing"
    assert armed.values["error"] is None
    assert armed.values["plan"]  # the plan the earlier planner call made

    final = await run.resume()

    assert final["status"] == "completed"  # dev -> gate -> review, all the way
    assert run.planner_calls == 1  # planning was not paid for a second time
    assert run.dev_calls == 2


async def test_the_resume_patch_does_not_skip_dev_on_a_re_armed_run(run: Run, monkeypatch):
    """Found while building this: patched with no node named, LangGraph took
    the patch to come from dev and moved on to the gate, so the resumed run
    skipped dev, reviewed an untouched worktree and "completed"."""
    _dev_fails_once(run, monkeypatch)
    await run.start()
    assert await run.arm() is True

    async with checkpointer_from_url(run.postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await _apply_resume_patch(graph, run.config, {"worktree_path": run.worktree_path})

        assert (await graph.aget_state(run.config)).next == ("dev",)


async def test_the_resume_patch_leaves_a_run_parked_at_plan_approval_at_dev(run: Run):
    state = initial_state(STORY, "Add a widget to the cart", "/repo")
    async with checkpointer_from_url(run.postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=True)
        assert (await graph.ainvoke(state, run.config))["status"] == "awaiting_approval"

        await _apply_resume_patch(graph, run.config, {"worktree_path": run.worktree_path})

        assert (await graph.aget_state(run.config)).next == ("dev",)


async def test_the_resume_patch_leaves_a_landing_send_back_at_dev(run: Run):
    """The merge queue re-opens a finished run at dev the same way."""
    await run.start(worktree_path=run.worktree_path)
    async with checkpointer_from_url(run.postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await graph.aupdate_state(
            run.config, {"status": "developing", "gate_feedback": "conflicts", "gate_attempts": 0}, as_node="gate",
        )

        await _apply_resume_patch(graph, run.config, {"worktree_path": run.worktree_path})

        assert (await graph.aget_state(run.config)).next == ("dev",)


async def test_arming_grants_a_fresh_gate_budget(run: Run, monkeypatch):
    """The failure was dev's; a run that had already used up gate attempts
    before it must not be one failed gate away from failing again."""
    _dev_fails_once(run, monkeypatch)
    await run.start(gate_attempts=2)
    assert (await run.snapshot()).values["gate_attempts"] == 2

    assert await run.arm() is True

    assert (await run.snapshot()).values["gate_attempts"] == 0


async def test_a_planner_failure_plans_again(run: Run, monkeypatch):
    async def planner_fails(**kwargs):
        raise PlannerLoopError("the plan gate rejected every attempt")

    monkeypatch.setattr(graph_module, "run_planner", planner_fails)

    assert (await run.start())["status"] == "failed"

    assert await run.arm() is False  # there is no plan to keep
    assert (await run.snapshot()).next == ()  # and the checkpoint was left alone


async def test_a_review_failure_plans_again(run: Run, monkeypatch):
    """A provider that fails at review is not resumed at dev: dev's work is
    done and would be redone. It takes the full redispatch it always did."""
    async def verifier_fails(**kwargs):
        raise GenerationError("the provider could not be reached")

    monkeypatch.setattr(graph_module, "run_verifier", verifier_fails)

    assert (await run.start())["status"] == "failed"
    assert run.dev_calls == 1

    assert await run.arm() is False
    assert (await run.snapshot()).next == ()


async def test_a_story_with_no_run_plans_again(run: Run):
    assert await run.arm() is False


async def test_a_missing_worktree_plans_again(run: Run, monkeypatch):
    _dev_fails_once(run, monkeypatch)
    await run.start()
    shutil.rmtree(run.worktree_path)

    assert await run.arm() is False  # nothing for dev to continue in
    assert (await run.snapshot()).next == ()


async def test_a_story_without_recorded_worktrees_plans_again(run: Run, monkeypatch):
    """Dispatched before per-repo worktrees existed (list_story_worktrees is
    empty): the conservative answer is the full redispatch."""
    _dev_fails_once(run, monkeypatch)
    await run.start()
    monkeypatch.setattr(StateManager, "list_story_worktrees", lambda self, key: [])

    assert await run.arm() is False


async def test_a_plan_older_than_the_done_work_in_the_epic_plans_again(run: Run, monkeypatch):
    """The rule that sends a plan waiting for approval back to planning: it was
    made before done tickets' work landed in the epic branch."""
    _dev_fails_once(run, monkeypatch)
    run.sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Done elsewhere", kind="dev")
    append_activity(STORY, type="status", phase="planner", title="Planning started")
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    run.sm.set_story_prior_work("ENG-3", {"epic_merge": {"widgets": {"result": "merged", "at": later}}})
    await run.start()

    assert await run.arm() is False
    assert (await run.snapshot()).next == ()


async def test_a_plan_newer_than_the_done_work_in_the_epic_is_kept(run: Run, monkeypatch):
    _dev_fails_once(run, monkeypatch)
    run.sm.create_story("ENG-3", epic_key="ENG-1", wave=0, summary="Done elsewhere", kind="dev")
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    run.sm.set_story_prior_work("ENG-3", {"epic_merge": {"widgets": {"result": "merged", "at": earlier}}})
    append_activity(STORY, type="status", phase="planner", title="Planning started")
    await run.start()

    assert await run.arm() is True


async def test_a_ticket_changed_in_its_source_plans_again(run: Run, monkeypatch):
    """The text under the plan changed and is waiting on a person: the plan
    may no longer say what the ticket asks."""
    _dev_fails_once(run, monkeypatch)
    await run.start()
    with session_scope() as session:
        session.get(Story, STORY).external_pending = {"hash": "abc", "description": "now asks for something else"}

    assert await run.arm() is False
    assert (await run.snapshot()).next == ()


async def test_a_qa_story_plans_again(run: Run, monkeypatch):
    _dev_fails_once(run, monkeypatch)
    await run.start()
    story = {**run.sm.get_story(STORY), "kind": "qa"}

    assert await arm_resume_at_dev(run.sm, run.sm.get_epic("ENG-1"), story) is False


def _snapshot(*, next: tuple, status: str | None = "failed", plan: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(next=next, values={"status": status, "plan": {"summary": "x"} if plan is None else plan})


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        pytest.param([_snapshot(next=()), _snapshot(next=("dev",), status="awaiting_approval")], True, id="failed in dev"),
        pytest.param([_snapshot(next=()), _snapshot(next=("planner",), status="planning")], False, id="failed in the planner"),
        pytest.param([_snapshot(next=()), _snapshot(next=("gate",), status="verifying")], False, id="failed at the gate"),
        pytest.param([_snapshot(next=()), _snapshot(next=("verifier",), status="verifying")], False, id="failed at review"),
        pytest.param([_snapshot(next=("dev",)), _snapshot(next=("dev",))], False, id="not finished (cancelled)"),
        pytest.param([_snapshot(next=(), status="completed"), _snapshot(next=("verifier",))], False, id="succeeded"),
        pytest.param([_snapshot(next=(), plan={}), _snapshot(next=("dev",))], False, id="no plan"),
        pytest.param([_snapshot(next=())], False, id="one checkpoint only"),
        pytest.param([], False, id="no checkpoint"),
    ],
)
async def test_ended_failed_in_dev_reads_the_checkpoint_sequence(history, expected):
    assert _ended_failed_in_dev(history) is expected


# --- a review the provider could not complete ----------------------------------------
#
# Dev's work is done and a re-run of the gate and the review is exactly what is
# missing. Only for a failure the provider caused: a review that ran and rejected
# would only reject again.


def _review_fails_once(run: Run, monkeypatch: pytest.MonkeyPatch, *, limit: ProviderLimit | None) -> dict:
    calls = {"n": 0}
    working = graph_module.run_verifier

    async def flaky_review(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GenerationError("the provider refused", limit=limit)
        return await working(**kwargs)

    monkeypatch.setattr(graph_module, "run_verifier", flaky_review)
    return calls


LIMIT = ProviderLimit(kind="rate", reset_at=time.time() + 60, message="slow down")


async def test_a_review_the_provider_refused_on_a_limit_continues_at_review_without_redoing_dev(run: Run, monkeypatch):
    reviews = _review_fails_once(run, monkeypatch, limit=LIMIT)

    failed = await run.start()
    assert failed["status"] == "failed" and failed["provider_limit"]["kind"] == "rate"
    assert (run.planner_calls, run.dev_calls) == (1, 1)

    story, epic = run.sm.get_story(STORY), run.sm.get_epic("ENG-1")
    assert await arm_resume_after_failure(run.sm, epic, story) == "review"

    armed = await run.snapshot()
    assert armed.next == ("gate",)  # ahead of the gate: dev's work is kept
    assert armed.values["status"] == "verifying"
    assert armed.values["error"] is None and armed.values["provider_limit"] is None

    final = await run.resume()

    assert final["status"] == "completed"
    assert (run.planner_calls, run.dev_calls) == (1, 1)  # neither planned nor built a second time
    assert reviews["n"] == 2


async def test_the_resume_patch_keeps_a_run_re_armed_at_review_waiting_at_the_gate(run: Run, monkeypatch):
    _review_fails_once(run, monkeypatch, limit=LIMIT)
    await run.start()
    assert await arm_resume_after_failure(run.sm, run.sm.get_epic("ENG-1"), run.sm.get_story(STORY)) == "review"

    async with checkpointer_from_url(run.postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await _apply_resume_patch(graph, run.config, {"worktree_path": run.worktree_path})

        assert (await graph.aget_state(run.config)).next == ("gate",)


async def test_a_review_that_failed_for_another_reason_plans_again_even_by_this_road(run: Run, monkeypatch):
    """No limit on it: the review may have rejected, or failed in a way a re-run
    would repeat."""
    _review_fails_once(run, monkeypatch, limit=None)
    await run.start()

    story, epic = run.sm.get_story(STORY), run.sm.get_epic("ENG-1")
    assert await arm_resume_after_failure(run.sm, epic, story) is None
    assert (await run.snapshot()).next == ()


async def test_the_dev_only_road_never_resumes_at_review(run: Run, monkeypatch):
    _review_fails_once(run, monkeypatch, limit=LIMIT)
    await run.start()

    assert await run.arm() is False  # arm_resume_at_dev keeps its contract
    assert (await run.snapshot()).next == ()


async def test_a_dev_failure_resumes_at_dev_by_the_wider_road_too_and_clears_the_limit(run: Run, monkeypatch):
    working_dev = graph_module.run_dev

    async def refused_once(**kwargs):
        if run.dev_calls == 0:
            run.dev_calls += 1
            raise DevLoopError("daily limit", limit=LIMIT)
        return await working_dev(**kwargs)

    monkeypatch.setattr(graph_module, "run_dev", refused_once)
    failed = await run.start()
    assert failed["provider_limit"]["kind"] == "rate"

    story, epic = run.sm.get_story(STORY), run.sm.get_epic("ENG-1")
    assert await arm_resume_after_failure(run.sm, epic, story) == "dev"
    assert (await run.snapshot()).values["provider_limit"] is None
    assert (await run.resume())["status"] == "completed"


async def test_the_resume_patch_leaves_a_run_waiting_on_the_competing_attempts_where_it_is(run: Run):
    """A crash while the variants node ran leaves the checkpoint waiting on it,
    entered from the gate like dev. Patched with no writer named, LangGraph would
    guess a node and could move past it."""
    await run.start(worktree_path=run.worktree_path)
    async with checkpointer_from_url(run.postgres_url) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        await graph.aupdate_state(
            run.config, {"status": "developing", "variants_pending": True, "gate_attempts": 2}, as_node="gate",
        )
        assert (await graph.aget_state(run.config)).next == ("variants",)

        await _apply_resume_patch(graph, run.config, {"worktree_path": run.worktree_path})

        assert (await graph.aget_state(run.config)).next == ("variants",)
