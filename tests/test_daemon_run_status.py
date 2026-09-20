"""What a story's status says while its run is in flight, and what happens
when the run doesn't finish cleanly.

A story used to keep its previous status — "awaiting_plan_approval", approval
still set — for the whole of its run, so the dashboard read as stuck, a second
/start could pick it up again, and the ticket sync treated a story being
implemented as not yet started. The graph itself is stubbed here: what is
under test is the daemon's bookkeeping around `graph.ainvoke`, not the agents.
"""
from __future__ import annotations

import asyncio

import pytest

from forge.daemon import main as daemon
from forge.orchestration.reconciler import NoOp, ResumeStory, plan_orchestration
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db"), pytest.mark.asyncio]

STORY = "ENG-2"


class _StubGraph:
    def __init__(self, on_invoke):
        self._on_invoke = on_invoke
        self.invocations = 0

    async def aupdate_state(self, config, patch):
        return None

    async def ainvoke(self, state, config):
        self.invocations += 1
        return await self._on_invoke()


@pytest.fixture()
def approved_story(git_repo: str, project_id: int) -> StateManager:
    """ENG-2 parked at the plan gate with the human's approval already given."""
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[[STORY]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story(STORY, epic_key="ENG-1", wave=0, summary="Add a widget")
    sm.set_story_status(STORY, "awaiting_plan_approval", parked_reason="plan_approval_gate")
    sm.set_story_gate_approved(STORY)
    return sm


def _use_graph(monkeypatch: pytest.MonkeyPatch, on_invoke) -> _StubGraph:
    graph = _StubGraph(on_invoke)
    monkeypatch.setattr(daemon, "compile_graph", lambda *args, **kwargs: graph)
    return graph


async def _resume(sm: StateManager) -> None:
    await daemon.handle_intent(sm, sm.get_epic("ENG-1"), ResumeStory(STORY))


async def test_story_reads_dispatched_while_its_run_is_in_flight(approved_story, monkeypatch):
    sm = approved_story
    seen: dict = {}

    async def run():
        story = sm.get_story(STORY)
        seen.update(
            status=story["status"], parked=story["parked_reason"], approved=story["gate_approved"],
            thread=story["thread_id"], live=daemon.live_run_keys(),
        )
        return {"status": "awaiting_approval"}

    _use_graph(monkeypatch, run)
    await _resume(sm)

    assert seen == {
        "status": "dispatched", "parked": None, "approved": False, "thread": STORY, "live": frozenset({STORY}),
    }
    # the run's own outcome replaces "dispatched", and nothing is left claimed
    after = sm.get_story(STORY)
    assert (after["status"], after["gate_approved"]) == ("awaiting_plan_approval", False)
    assert daemon.live_run_keys() == frozenset()


async def test_a_second_run_for_a_story_in_flight_is_refused(approved_story, monkeypatch):
    sm = approved_story
    started, release = asyncio.Event(), asyncio.Event()

    async def run():
        started.set()
        await release.wait()
        return {"status": "awaiting_approval"}

    graph = _use_graph(monkeypatch, run)
    first = asyncio.create_task(_resume(sm))
    await started.wait()

    # e.g. the Start button while the poll loop is still awaiting the run
    await _resume(sm)
    assert graph.invocations == 1

    # and the reconciler now answers the story with a NoOp, not a resume
    epic = sm.get_epic("ENG-1")
    intents = plan_orchestration(daemon.build_snapshot(sm, "ENG-1"), daemon.build_context(sm, epic))
    assert intents == [NoOp(reason="already_running", story_key=STORY)]

    release.set()
    await first
    assert graph.invocations == 1
    assert daemon.live_run_keys() == frozenset()


async def test_a_run_that_raises_puts_the_story_back_with_its_approval(approved_story, monkeypatch):
    sm = approved_story

    async def run():
        raise RuntimeError("provider unavailable")

    _use_graph(monkeypatch, run)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await _resume(sm)

    story = sm.get_story(STORY)
    assert (story["status"], story["parked_reason"], story["gate_approved"]) == (
        "awaiting_plan_approval", "plan_approval_gate", True,
    )  # so the next cycle retries it without asking the human again
    assert daemon.live_run_keys() == frozenset()


async def test_a_cancelled_run_is_restored_and_released(approved_story, monkeypatch):
    sm = approved_story
    started = asyncio.Event()

    async def run():
        started.set()
        await asyncio.Event().wait()

    _use_graph(monkeypatch, run)
    task = asyncio.create_task(_resume(sm))
    await started.wait()
    assert sm.get_story(STORY)["status"] == "dispatched"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    story = sm.get_story(STORY)
    assert (story["status"], story["gate_approved"]) == ("awaiting_plan_approval", True)
    assert daemon.live_run_keys() == frozenset()


async def test_a_status_the_graph_should_never_end_on_fails_the_story(approved_story, monkeypatch):
    sm = approved_story

    async def run():
        return {"status": "developing"}

    _use_graph(monkeypatch, run)
    await _resume(sm)

    story = sm.get_story(STORY)
    assert (story["status"], story["failure_stage"]) == ("failed", "run")


async def test_a_failed_restore_does_not_mask_the_runs_own_error(approved_story, monkeypatch):
    sm = approved_story

    async def run():
        raise RuntimeError("the real error")

    def broken_restore(*args, **kwargs):
        raise ConnectionError("database went away")

    _use_graph(monkeypatch, run)
    monkeypatch.setattr(daemon, "_restore_unstarted", broken_restore)
    with pytest.raises(RuntimeError, match="the real error"):
        await _resume(sm)
    assert daemon.live_run_keys() == frozenset()
