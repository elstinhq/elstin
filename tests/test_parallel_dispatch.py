"""Stories run in parallel: poll_once starts every story whose dependencies
have merged as its own background task, up to max_parallel_stories, and
returns without waiting (forge/daemon/main.py's _launch). Real Postgres and
git; the agents are fakes, and a gate event holds runs open so the test can
see them overlap."""
from __future__ import annotations

import asyncio

import pytest

import forge.daemon.main as daemon
from forge.config.store import set_config
from forge.state.manager import StateManager

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]


def _configure(tmp_path, *, max_parallel: int | None = None) -> None:
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "false")
    if max_parallel is not None:
        set_config("max_parallel_stories", str(max_parallel))


def _epic(sm: StateManager, project_id: int, git_repo: str, deps: dict[str, list[str]]) -> None:
    keys = list(deps)
    sm.create_epic("ENG-1", project_id=project_id, levels=[keys], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    for key in keys:
        sm.create_story(key, epic_key="ENG-1", wave=0, summary=f"{key} work", depends_on=deps[key])


def _held_dev(monkeypatch, release: asyncio.Event, running: set[str], peak: list[int]):
    import forge.orchestration.graph as graph

    async def dev(**kwargs):
        running.add(kwargs["run_id"])
        peak[0] = max(peak[0], len(running))
        await release.wait()
        running.discard(kwargs["run_id"])
        return {"file_changes": [], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}}

    monkeypatch.setattr(graph, "run_dev", dev)


async def _settle() -> None:
    await asyncio.gather(*daemon._background.values(), return_exceptions=True)


async def _until(condition, timeout: float = 10.0) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


async def test_independent_stories_run_at_the_same_time(git_repo, tmp_path, project_id, monkeypatch):
    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, {"ENG-2": [], "ENG-3": [], "ENG-4": ["ENG-2"]})
    release, running, peak = asyncio.Event(), set(), [0]
    _held_dev(monkeypatch, release, running, peak)

    await daemon.poll_once(sm)  # returns at once; the runs go on in the background
    await _until(lambda: running == {"ENG-2", "ENG-3"})

    assert sm.get_story("ENG-4")["status"] == "pending"  # waits for ENG-2 only
    release.set()
    await _settle()
    assert peak[0] == 2
    assert {sm.get_story(k)["status"] for k in ("ENG-2", "ENG-3")} == {"ready_to_merge"}


async def test_max_parallel_stories_caps_how_many_start(git_repo, tmp_path, project_id, monkeypatch):
    _configure(tmp_path, max_parallel=1)
    sm = StateManager()
    _epic(sm, project_id, git_repo, {"ENG-2": [], "ENG-3": [], "ENG-5": []})
    release, running, peak = asyncio.Event(), set(), [0]
    _held_dev(monkeypatch, release, running, peak)

    await daemon.poll_once(sm)
    await _until(lambda: len(running) == 1)
    await daemon.poll_once(sm)  # still busy: nothing more starts
    await asyncio.sleep(0.1)
    assert len(running) == 1
    assert [sm.get_story(k)["status"] for k in ("ENG-2", "ENG-3", "ENG-5")].count("pending") == 2

    release.set()
    await _settle()
    assert peak[0] == 1


async def test_a_story_already_in_flight_is_not_started_again(git_repo, tmp_path, project_id, monkeypatch):
    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, {"ENG-2": []})
    release, running, peak = asyncio.Event(), set(), [0]
    _held_dev(monkeypatch, release, running, peak)
    starts: list[str] = []
    original = daemon._run_or_resume_story

    async def counting(sm_, epic, story, *, resume):
        starts.append(story["jira_key"])
        return await original(sm_, epic, story, resume=resume)

    monkeypatch.setattr(daemon, "_run_or_resume_story", counting)

    await daemon.poll_once(sm)
    await _until(lambda: running == {"ENG-2"})
    await daemon.poll_once(sm)
    await daemon.poll_once(sm)
    release.set()
    await _settle()

    assert starts == ["ENG-2"]


async def test_one_story_crashing_does_not_stop_the_others(git_repo, tmp_path, project_id, monkeypatch):
    import forge.orchestration.graph as graph

    _configure(tmp_path)
    sm = StateManager()
    _epic(sm, project_id, git_repo, {"ENG-2": [], "ENG-3": []})

    async def dev(**kwargs):
        if kwargs["run_id"] == "ENG-2":
            raise RuntimeError("boom")
        return {"file_changes": [], "truncated": False, "messages": [], "dev_history": [], "dev_build_runner": {}}

    monkeypatch.setattr(graph, "run_dev", dev)

    await daemon.poll_once(sm, wait=True)

    assert sm.get_story("ENG-3")["status"] == "ready_to_merge"
    assert daemon._background == {}
