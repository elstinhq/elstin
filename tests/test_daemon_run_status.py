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
from types import SimpleNamespace

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

    async def aget_state(self, config):
        return SimpleNamespace(next=(), values={})

    async def aupdate_state(self, config, patch, as_node=None):
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


# --- cancelling a run in flight ------------------------------------------


async def _start_blocked_run(sm, monkeypatch):
    """A run parked inside the graph until released; returns (task, release, graph)."""
    started, release = asyncio.Event(), asyncio.Event()

    async def run():
        started.set()
        await release.wait()
        return {"status": "awaiting_approval"}

    graph = _use_graph(monkeypatch, run)
    task = asyncio.create_task(_resume(sm))
    await started.wait()
    return task, release, graph


async def test_cancelling_a_run_fails_the_story_as_cancelled_and_the_caller_carries_on(approved_story, monkeypatch):
    """The daemon's poll loop awaits runs inline. Cancelling must stop the run
    without cancelling that caller — otherwise cancelling one story would
    stop the daemon."""
    sm = approved_story
    task, _release, _graph = await _start_blocked_run(sm, monkeypatch)
    assert sm.get_story(STORY)["status"] == "dispatched"

    assert await daemon.cancel_run(STORY) is True

    await task  # returns normally: no CancelledError leaks out to the awaiting caller
    story = sm.get_story(STORY)
    assert (story["status"], story["parked_reason"], story["failure_stage"], story["gate_approved"]) == (
        "failed", "cancelled", "run", False,
    )
    assert daemon.live_run_keys() == frozenset()
    assert any(e["title"] == "Run cancelled" for e in _activity(STORY))


async def test_cancelling_when_nothing_is_running_says_so(approved_story):
    assert await daemon.cancel_run(STORY) is False
    assert sm_status(approved_story) == "awaiting_plan_approval"  # and touches nothing


async def test_a_cancelled_story_can_be_retried_and_removed(approved_story, monkeypatch):
    sm = approved_story
    task, _release, _graph = await _start_blocked_run(sm, monkeypatch)
    await daemon.cancel_run(STORY)
    await task

    sm.retry_story(STORY)  # the existing recovery for a failed run: a full redispatch
    assert sm.get_story(STORY)["status"] == "pending"


async def test_a_cancel_that_lands_after_the_outcome_was_written_does_not_override_it(approved_story, monkeypatch):
    sm = approved_story
    started, release = asyncio.Event(), asyncio.Event()

    async def run():
        sm.set_story_status(STORY, "ready_to_merge")  # the run got as far as recording its result
        started.set()
        await release.wait()

    _use_graph(monkeypatch, run)
    task = asyncio.create_task(_resume(sm))
    await started.wait()

    await daemon.cancel_run(STORY)
    await task

    assert sm.get_story(STORY)["status"] == "ready_to_merge"


async def test_a_shutdown_is_still_a_restore_and_not_mistaken_for_a_person_cancelling(approved_story, monkeypatch):
    """The outer task being cancelled (the daemon stopping) must unwind and
    re-raise, exactly as before cancel-run existed."""
    sm = approved_story
    task, _release, _graph = await _start_blocked_run(sm, monkeypatch)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    story = sm.get_story(STORY)
    assert (story["status"], story["gate_approved"]) == ("awaiting_plan_approval", True)
    assert daemon.live_run_keys() == frozenset()


async def test_the_poll_loop_survives_a_cancel_and_does_not_redispatch_the_story(git_repo, tmp_path, project_id, monkeypatch):
    """The integration property: poll_once is what awaits the run."""
    from forge.config.store import set_config

    set_config("worktrees_base_dir", str(tmp_path / "wt"))
    set_config("openai_api_key", "sk-test")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[[STORY]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story(STORY, epic_key="ENG-1", wave=0, summary="Add a widget")

    started, release = asyncio.Event(), asyncio.Event()

    async def run():
        started.set()
        await release.wait()
        return {"status": "awaiting_approval"}

    graph = _use_graph(monkeypatch, run)
    poll = asyncio.create_task(daemon.poll_once(sm))
    await started.wait()

    assert await daemon.cancel_run(STORY) is True
    await poll  # poll_once completes normally

    assert sm.get_story(STORY)["status"] == "failed" and graph.invocations == 1
    await daemon.poll_once(sm, wait=True)  # a later cycle leaves the failed story alone
    assert graph.invocations == 1 and sm.get_story(STORY)["status"] == "failed"


def sm_status(sm: StateManager) -> str:
    return sm.get_story(STORY)["status"]


def _activity(run_id: str) -> list[dict]:
    from forge.services.run_activity import list_activity

    return list_activity(run_id)


# --- the route --------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_the_cancel_route_maps_its_outcomes(approved_story, monkeypatch):
    """404 for an unknown story, 409 when nothing is running, 200 with the
    story once cancelled. (The run itself lives on the app's own loop, so the
    stopping is covered above; this is the HTTP contract.)"""
    from fastapi.testclient import TestClient

    import forge.api.run_routes as run_routes
    from forge.app import create_app

    set_project_config_off(approved_story)
    answers = {STORY: False}

    async def fake_cancel(key, **kwargs):
        return answers.get(key, False)

    monkeypatch.setattr(run_routes, "cancel_run", fake_cancel)
    with TestClient(create_app()) as client:
        assert client.post("/api/stories/NOPE-1/cancel").status_code == 404
        idle = client.post(f"/api/stories/{STORY}/cancel")
        assert idle.status_code == 409 and "has no run in progress" in idle.json()["detail"]
        answers[STORY] = True
        ok = client.post(f"/api/stories/{STORY}/cancel")
        assert ok.status_code == 200 and ok.json()["jira_key"] == STORY


def set_project_config_off(sm: StateManager) -> None:
    from forge.config.store import set_project_config

    project_id = sm.get_epic("ENG-1")["project_id"]
    set_project_config(project_id, "auto_run_enabled", "false")  # keep the poller out of the request
