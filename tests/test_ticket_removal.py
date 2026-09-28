"""Removing a ticket from forge: everything it left behind goes, nothing
else does, and it refuses while a run is in flight.

Driven against real Postgres and real git. Two tickets are taken through the
daemon (fake agents, so no model is called) as far as a merge into their epic
integration branches, which leaves every kind of trace: rows in each table,
worktrees and branches, an integration worktree and branch, baseline and
attachment folders, and LangGraph checkpoints. One is removed; the other must
come through untouched.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from forge.config.store import set_config, set_project_config
from forge.daemon import ticket_removal
from forge.daemon.main import claim_run, handle_intent, live_run_keys, poll_once, release_run
from forge.daemon.ticket_removal import TicketBusy, TicketNotFound, TicketRemovalError, remove_epic
from forge.db.engine import session_scope
from forge.db.models import (
    ActivityEvent, Epic, JiraWatermark, PullRequest, Story, StoryRunOutcome, StoryUsage, StoryWorktree,
)
from forge.orchestration.reconciler import DispatchStory
from forge.services.usage import record_phase_usage, record_run_outcome
from forge.state.manager import StateManager
from forge.orchestration.integration import integration_branch_name
from forge.tools.worktree import branch_name_for

pytestmark = [pytest.mark.usefixtures("forge_db", "fake_agents"), pytest.mark.asyncio]

GONE = ("ENG-1", "ENG-2", "ENG-2A")   # the epic, its story, and the story's sub-task
KEPT = ("ENG-9", "ENG-10")


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def _branches(repo: str) -> set[str]:
    return set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def _rows(keys: tuple[str, ...]) -> dict[str, int]:
    """How many rows, in every place a ticket leaves one, name these keys."""
    story_keys = [k for k in keys if k != keys[0]] or list(keys)
    with session_scope() as s:
        def count(model, column, values) -> int:
            return s.scalar(select(func.count()).select_from(model).where(column.in_(values))) or 0

        def checkpoint_rows(table: str) -> int:
            return s.execute(text(f"select count(*) from {table} where thread_id = any(:keys)"), {"keys": list(keys)}).scalar()

        return {
            "epics": count(Epic, Epic.jira_key, keys),
            "stories": count(Story, Story.jira_key, keys),
            "pull_requests": count(PullRequest, PullRequest.story_key, story_keys),
            "worktree_rows": count(StoryWorktree, StoryWorktree.story_key, story_keys),
            "activity_events": count(ActivityEvent, ActivityEvent.run_id, keys),
            "usage_rows": count(StoryUsage, StoryUsage.run_id, keys),
            "run_outcomes": count(StoryRunOutcome, StoryRunOutcome.run_id, keys),
            "watermarks": count(JiraWatermark, JiraWatermark.jira_key, keys),
            "checkpoints": checkpoint_rows("checkpoints"),
            "checkpoint_writes": checkpoint_rows("checkpoint_writes"),
            "checkpoint_blobs": checkpoint_rows("checkpoint_blobs"),
        }


@pytest.fixture()
def tickets(git_repo: str, tmp_path: Path, project_id: int):
    """ENG-1 (story ENG-2 with sub-task ENG-2A) and ENG-9 (story ENG-10), both
    merged into their epics' integration branches, with extra traces added
    that the fake agents do not produce."""
    dirs = {"worktrees": tmp_path / "worktrees", "data": tmp_path / "data", "attachments": tmp_path / "attachments"}
    set_config("worktrees_base_dir", str(dirs["worktrees"]))
    set_config("data_dir", str(dirs["data"]))
    set_config("jira_attachments_dir", str(dirs["attachments"]))
    set_config("openai_api_key", "sk-test")

    sm = StateManager()
    repo_id = sm.add_project_repo(project_id, git_repo)
    for epic, story, summary in (("ENG-1", "ENG-2", "Add a widget"), ("ENG-9", "ENG-10", "Add a gadget")):
        sm.create_epic(epic, project_id=project_id, levels=[[story]], manual_gate=False)
        sm.set_epic_project_repo(epic, repo_id)
        sm.create_story(story, epic_key=epic, wave=0, summary=summary)
    sm.create_story("ENG-2A", epic_key="ENG-1", wave=0, summary="A sub-task", parent_key="ENG-2")
    return {"sm": sm, "repo": git_repo, "dirs": dirs, "project_id": project_id}


async def _drive_to_merge(t: dict) -> None:
    sm = t["sm"]
    await poll_once(sm, wait=True)                          # dispatch -> plan approval gate
    for _ in range(3):                           # plan, PR open, PR review
        for key in ("ENG-2", "ENG-10"):
            sm.set_story_gate_approved(key)
        await poll_once(sm, wait=True)
    assert {sm.get_story(k)["status"] for k in ("ENG-2", "ENG-10")} == {"merged"}

    # Traces the fake agents never write.
    for key in ("ENG-2", "ENG-10"):
        sm.upsert_pull_request(key, number=7 if key == "ENG-2" else 8, url=f"https://github.com/o/r/pull/{key}", state="open")
        record_phase_usage(key, phase="dev", model="m", provider="p", input_tokens=10, output_tokens=5, cost_usd=0.01, duration_ms=1)
        record_run_outcome(key, dev_passes=1)
        (t["dirs"]["data"] / "baselines" / key).mkdir(parents=True, exist_ok=True)
        (t["dirs"]["data"] / "baselines" / key / "snapshot.txt").write_text("x")
        (t["dirs"]["attachments"] / key).mkdir(parents=True, exist_ok=True)
        (t["dirs"]["attachments"] / key / "shot.png").write_text("x")
    for key in ("ENG-1", "ENG-9"):
        with session_scope() as s:
            s.add(JiraWatermark(jira_key=key, last_comment_id="42"))


@pytest.fixture()
async def merged(tickets):
    await _drive_to_merge(tickets)
    return tickets


# --- everything goes -------------------------------------------------------


async def test_the_starting_point_really_has_every_kind_of_trace(merged):
    """If this fails the removal tests below prove nothing."""
    repo, dirs = merged["repo"], merged["dirs"]
    rows = _rows(GONE)
    assert all(rows[k] > 0 for k in rows), rows
    assert (dirs["worktrees"] / "eng-2").is_dir() and (dirs["worktrees"] / "_integration-eng-1").is_dir()
    assert branch_name_for("ENG-2", "Add a widget") in _branches(repo) and integration_branch_name("ENG-1") in _branches(repo)


async def test_removing_a_ticket_clears_every_trace_of_it(merged):
    repo, dirs, sm = merged["repo"], merged["dirs"], merged["sm"]
    before = _rows(GONE)

    report = await remove_epic(sm, "ENG-1")

    assert _rows(GONE) == {k: 0 for k in before}
    # filesystem
    assert not (dirs["worktrees"] / "eng-2").exists() and not (dirs["worktrees"] / "_integration-eng-1").exists()
    assert not (dirs["data"] / "baselines" / "ENG-2").exists()
    assert not (dirs["attachments"] / "ENG-2").exists()
    # git: no worktree registered, no branch left, and the repo's own branch survives
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert "eng-2" not in listed and "_integration-eng-1" not in listed
    branches = _branches(repo)
    assert branch_name_for("ENG-2", "Add a widget") not in branches and integration_branch_name("ENG-1") not in branches
    assert "main" in branches
    # the report says what went
    removed = report["removed"]
    assert removed["stories"] == before["stories"] == 2  # ENG-2 and its sub-task
    assert removed["pull_request_records"] == before["pull_requests"] > 0
    assert removed["activity_events"] == before["activity_events"] > 0
    assert removed["usage_rows"] == before["usage_rows"] > 0
    assert removed["run_outcomes"] == before["run_outcomes"] > 0
    assert removed["checkpoint_threads"] == 3  # the epic's key and its two stories
    assert branch_name_for("ENG-2", "Add a widget") in removed["branches"] and integration_branch_name("ENG-1") in removed["branches"]
    assert report["warnings"] == []


async def test_other_tickets_come_through_untouched(merged):
    repo, dirs, sm = merged["repo"], merged["dirs"], merged["sm"]
    before = _rows(KEPT)
    assert all(before[k] > 0 for k in before), before

    await remove_epic(sm, "ENG-1")

    assert _rows(KEPT) == before
    assert (dirs["worktrees"] / "eng-10").is_dir() and (dirs["worktrees"] / "_integration-eng-9").is_dir()
    assert (dirs["data"] / "baselines" / "ENG-10" / "snapshot.txt").exists()
    assert (dirs["attachments"] / "ENG-10" / "shot.png").exists()
    assert branch_name_for("ENG-10", "Add a gadget") in _branches(repo) and integration_branch_name("ENG-9") in _branches(repo)
    assert sm.get_story("ENG-10")["status"] == "merged"


async def test_removal_is_local_only_and_the_report_lists_what_is_left_on_github(merged):
    report = await remove_epic(merged["sm"], "ENG-1")

    assert report["left_on_remote"]["pull_requests"] == [
        {"story_key": "ENG-2", "number": 7, "url": "https://github.com/o/r/pull/ENG-2", "state": "open"}
    ]
    assert branch_name_for("ENG-2", "Add a widget") in report["left_on_remote"]["branches_that_may_be_pushed"]


async def test_the_report_warns_when_the_projects_ticket_filter_could_bring_it_back(merged):
    sm = merged["sm"]
    first = await remove_epic(sm, "ENG-1")
    assert first["ticket_filter_may_readd"] is False

    sm.set_project_ticket_filter(merged["project_id"], enabled=True, jira_project_key="ENG")
    assert (await remove_epic(sm, "ENG-9"))["ticket_filter_may_readd"] is True


# --- refusing, and failing safely ------------------------------------------


async def test_a_ticket_with_a_run_in_flight_is_refused_and_left_alone(merged):
    sm = merged["sm"]
    before = _rows(GONE)
    assert claim_run("ENG-2")  # what a running story holds
    try:
        with pytest.raises(TicketBusy, match="ENG-2 has a run in progress"):
            await remove_epic(sm, "ENG-1")
    finally:
        release_run("ENG-2")

    assert _rows(GONE) == before
    assert live_run_keys() == frozenset()  # nothing the refused call claimed is left held
    await remove_epic(sm, "ENG-1")  # and once the run is done it goes


async def test_the_ticket_is_claimed_while_it_is_being_removed(merged, monkeypatch):
    """The poller and /start must not be able to begin a run mid-removal."""
    seen: list[bool] = []
    real = ticket_removal._clean_local_artifacts

    def spy(footprint):
        seen.append(all(k in live_run_keys() for k in footprint["keys"]))
        return real(footprint)

    monkeypatch.setattr(ticket_removal, "_clean_local_artifacts", spy)
    await remove_epic(merged["sm"], "ENG-1")

    assert seen == [True] and live_run_keys() == frozenset()


async def test_an_unknown_ticket_is_not_found(tickets):
    with pytest.raises(TicketNotFound):
        await remove_epic(tickets["sm"], "NOPE-1")


async def test_a_checkpoint_failure_stops_before_any_row_is_deleted_and_can_be_retried(merged, monkeypatch):
    """Stale checkpoints would resume a re-added ticket's old thread, so this
    is not a warning: the rows stay, and the ticket can be removed again."""
    sm = merged["sm"]
    before = _rows(GONE)

    async def broken(keys):
        raise TicketRemovalError("checkpoint store unreachable")

    real = ticket_removal._delete_checkpoints
    monkeypatch.setattr(ticket_removal, "_delete_checkpoints", broken)
    with pytest.raises(TicketRemovalError, match="unreachable"):
        await remove_epic(sm, "ENG-1")
    assert {k: v for k, v in _rows(GONE).items() if k not in ("checkpoints", "checkpoint_writes", "checkpoint_blobs")} == {
        k: v for k, v in before.items() if k not in ("checkpoints", "checkpoint_writes", "checkpoint_blobs")
    }
    assert live_run_keys() == frozenset()

    # not monkeypatch.undo(): that would also revert the DATABASE_URL forge_db set
    monkeypatch.setattr(ticket_removal, "_delete_checkpoints", real)
    await remove_epic(sm, "ENG-1")  # the filesystem was already clean; the retry still finishes the job
    assert _rows(GONE) == {k: 0 for k in before}


async def test_a_worktree_deleted_by_hand_is_no_obstacle(merged):
    import shutil

    shutil.rmtree(merged["dirs"]["worktrees"] / "eng-2")

    report = await remove_epic(merged["sm"], "ENG-1")

    assert _rows(GONE) == {k: 0 for k in _rows(GONE)}
    assert branch_name_for("ENG-2", "Add a widget") not in _branches(merged["repo"])
    assert report["warnings"] == []


async def test_a_ticket_that_never_ran_is_removed_too(tickets):
    """No worktree, no branch, no checkpoint: just rows."""
    report = await remove_epic(tickets["sm"], "ENG-9")

    assert _rows(KEPT) == {k: 0 for k in _rows(KEPT)}
    assert report["removed"]["worktrees"] == [] and report["removed"]["branches"] == []
    assert tickets["sm"].get_epic("ENG-1") is not None


# --- the poller ------------------------------------------------------------


async def test_the_poller_skips_an_intent_for_a_story_that_was_just_removed(tickets):
    """Intents are planned from a snapshot; one may name a story removed since."""
    epic = tickets["sm"].get_epic("ENG-1")

    await handle_intent(tickets["sm"], epic, DispatchStory("NO-SUCH-STORY"))  # must not raise


# --- the API ---------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from forge.app import create_app

    return TestClient(create_app())


async def test_delete_over_http_removes_the_ticket_and_reports(merged):
    set_project_config(merged["project_id"], "auto_run_enabled", "false")  # keep the poller out of the request
    with _client() as client:
        response = client.delete("/api/epics/ENG-1")
        assert response.status_code == 200
        body = response.json()
        assert body["epic"] == "ENG-1" and body["removed"]["stories"] == 2
        assert client.get("/api/epics/ENG-1").status_code == 404
        assert client.get("/api/epics/ENG-9").status_code == 200


async def test_delete_over_http_answers_404_and_409(merged):
    set_project_config(merged["project_id"], "auto_run_enabled", "false")
    assert claim_run("ENG-10")
    try:
        with _client() as client:
            assert client.delete("/api/epics/NOPE-1").status_code == 404
            busy = client.delete("/api/epics/ENG-9")
            assert busy.status_code == 409 and "ENG-10 has a run in progress" in busy.json()["detail"]
            assert client.get("/api/epics/ENG-9").status_code == 200
    finally:
        release_run("ENG-10")


# --- the path guard --------------------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_directory_removal_never_leaves_its_base(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    removed: list[str] = []
    warnings: list[str] = []

    ticket_removal._remove_dir(str(base / ".." / "outside"), str(base), removed, warnings)
    ticket_removal._remove_dir(str(base), str(base), removed, warnings)  # the base itself

    assert (outside / "keep.txt").exists() and base.exists()
    assert removed == [] and len(warnings) == 2


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_a_symlink_is_unlinked_not_followed(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    (base / "eng-2").symlink_to(target)
    removed: list[str] = []

    ticket_removal._remove_dir(str(base / "eng-2"), str(base), removed, [])

    assert not (base / "eng-2").exists() and (target / "keep.txt").exists()
    assert removed == [str(base / "eng-2")]
