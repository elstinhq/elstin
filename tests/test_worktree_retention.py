"""forge/daemon/worktree_retention.py — a finished story's checkout holds a full
dependency install and nothing used to take it away. Real git and real Postgres:
what is under test is what is left on disk, in git and in the rows."""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.config.store import get_config, set_config
from forge.daemon import worktree_retention
from forge.daemon.worktree_retention import maybe_reclaim, reclaim_finished_worktrees
from forge.state.manager import StateManager
from forge.tools.worktree import create_worktree, run_git

pytestmark = pytest.mark.usefixtures("forge_db")

EPIC_BRANCH = "epic/ENG-1"


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def _branches(repo: str) -> set[str]:
    return set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


@pytest.fixture()
def world(git_repo, tmp_path, project_id):
    """One repo, one epic with a branch, an empty base dir for worktrees."""
    base = str(tmp_path / "worktrees")
    os.makedirs(base)
    set_config("worktrees_base_dir", base)
    _git(git_repo, "branch", EPIC_BRANCH, "main")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.set_integration_branch("ENG-1", EPIC_BRANCH)

    class World:
        pass

    w = World()
    w.sm, w.repo, w.base, w.n = sm, git_repo, base, 0
    return w


def _story(w, status: str = "merged", *, label: str | None = None, key: str | None = None) -> dict:
    """A story with a real worktree on its own branch, in `status`."""
    w.n += 1
    key = key or f"ENG-{w.n + 1}"
    w.sm.create_story(key, epic_key="ENG-1", wave=0, summary=f"story {key}")
    made = create_worktree(w.repo, story_key=key, summary=f"story {key}", worktrees_base_dir=w.base, repo_label=label)
    w.sm.record_story_worktree(
        key, repo_label=label or "repo", repo_path=w.repo,
        worktree_path=made["worktree_path"], branch=made["branch"], is_default=True,
    )
    w.sm.set_story_status(key, status)
    return {"key": key, **made}


def _commit(path: str, name: str) -> None:
    with open(os.path.join(path, name), "w") as handle:
        handle.write(name)
    _git(path, "add", name)
    _git(path, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", name)


def test_the_newest_finished_stories_keep_their_worktrees_and_older_ones_lose_the_directory_only(world):
    stories = [_story(world) for _ in range(4)]

    report = reclaim_finished_worktrees(world.sm, keep=2)

    assert report["reclaimed"] == [stories[1]["key"], stories[0]["key"]]
    assert report["kept"] == 2
    for gone in stories[:2]:
        assert not os.path.exists(gone["worktree_path"])
        assert gone["branch"] in _branches(world.repo)  # the work stays recoverable
        assert gone["worktree_path"] not in _git(world.repo, "worktree", "list", "--porcelain")
    for kept in stories[2:]:
        assert os.path.isdir(kept["worktree_path"])
    # The rows are left as they were: every reader already tolerates a missing directory.
    assert len(world.sm.list_story_worktrees(stories[0]["key"])) == 1


def test_a_reclaimed_worktree_can_be_made_again_on_its_branch(world):
    [old, _new] = [_story(world), _story(world)]
    _commit(old["worktree_path"], "kept-work.txt")
    reclaim_finished_worktrees(world.sm, keep=1)
    assert not os.path.exists(old["worktree_path"])

    again = create_worktree(
        world.repo, story_key=old["key"], summary="story", worktrees_base_dir=world.base, branch=old["branch"],
    )
    assert os.path.exists(os.path.join(again["worktree_path"], "kept-work.txt"))


def test_zero_keeps_everything(world):
    stories = [_story(world) for _ in range(3)]
    assert reclaim_finished_worktrees(world.sm, keep=0)["reclaimed"] == []
    assert all(os.path.isdir(s["worktree_path"]) for s in stories)


def test_the_setting_defaults_to_ten_and_is_what_a_sweep_uses(world):
    assert get_config("worktree_retention") == "10"
    stories = [_story(world) for _ in range(3)]
    set_config("worktree_retention", "1")
    assert reclaim_finished_worktrees(world.sm)["reclaimed"] == [stories[1]["key"], stories[0]["key"]]


def test_a_story_that_is_not_finished_is_never_reclaimed(world):
    running = _story(world, "developing")
    waiting = _story(world, "ready_to_merge")
    failed = _story(world, "failed")
    _story(world)
    reclaim_finished_worktrees(world.sm, keep=1)
    for story in (running, waiting, failed):
        assert os.path.isdir(story["worktree_path"])


def test_a_story_with_a_run_in_flight_is_left_alone(world):
    first, _second = _story(world), _story(world)
    report = reclaim_finished_worktrees(world.sm, keep=1, live=frozenset({first["key"]}))
    assert os.path.isdir(first["worktree_path"])
    assert report["reclaimed"] == []
    assert report["skipped"] == [{"story_key": first["key"], "reason": "a run is in flight"}]


def test_one_already_reclaimed_takes_no_slot_so_a_second_pass_does_nothing(world):
    stories = [_story(world) for _ in range(3)]
    reclaim_finished_worktrees(world.sm, keep=2)
    again = reclaim_finished_worktrees(world.sm, keep=2)
    assert again["reclaimed"] == [] and again["kept"] == 2
    assert [os.path.isdir(s["worktree_path"]) for s in stories] == [False, True, True]


def test_a_path_outside_the_base_dir_is_never_touched(world, tmp_path):
    outside = _story(world)
    _story(world)
    elsewhere = str(tmp_path / "somewhere-else")
    os.makedirs(elsewhere)
    report = reclaim_finished_worktrees(world.sm, keep=1, base_dir=elsewhere)
    assert os.path.isdir(outside["worktree_path"])
    assert report["skipped"][0]["reason"] == "a worktree is outside worktrees_base_dir"


def test_the_base_dir_itself_is_not_a_worktree_to_remove(world):
    assert worktree_retention._under(world.base, world.base) is False
    assert worktree_retention._under(os.path.join(world.base, "x", "y"), world.base) is True
    assert worktree_retention._under(world.base + "-sibling/x", world.base) is False


@pytest.mark.parametrize("status", ["done", "skipped"])
def test_a_done_story_goes_only_when_clean_and_carrying_nothing_the_epic_lacks(world, status):
    clean = _story(world, status)
    dirty = _story(world, status)
    ahead = _story(world, status)
    _story(world)  # the one that keeps a slot
    with open(os.path.join(dirty["worktree_path"], "scratch.txt"), "w") as handle:
        handle.write("not committed")
    _commit(ahead["worktree_path"], "unmerged.txt")

    report = reclaim_finished_worktrees(world.sm, keep=1)

    assert not os.path.exists(clean["worktree_path"])
    assert os.path.isdir(dirty["worktree_path"]) and os.path.isdir(ahead["worktree_path"])
    reasons = {item["story_key"]: item["reason"] for item in report["skipped"]}
    assert reasons[dirty["key"]] == "it has uncommitted changes"
    assert reasons[ahead["key"]] == "it has commits the epic branch lacks"


def test_a_merged_story_goes_even_with_leftovers_because_its_commits_are_on_the_epic(world):
    merged = _story(world)
    _story(world)
    with open(os.path.join(merged["worktree_path"], "scratch.txt"), "w") as handle:
        handle.write("disposable")
    assert reclaim_finished_worktrees(world.sm, keep=1)["reclaimed"] == [merged["key"]]


def test_a_multi_repo_story_loses_every_worktree_and_its_folder(world):
    multi = _story(world, label="api")
    second = create_worktree(
        world.repo, story_key=multi["key"], summary="story", worktrees_base_dir=world.base, repo_label="ui",
        branch=f"{multi['branch']}-ui",
    )
    world.sm.record_story_worktree(
        multi["key"], repo_label="ui", repo_path=world.repo, worktree_path=second["worktree_path"], branch=second["branch"],
    )
    _story(world)

    assert reclaim_finished_worktrees(world.sm, keep=1)["reclaimed"] == [multi["key"]]
    assert not os.path.exists(multi["worktree_path"]) and not os.path.exists(second["worktree_path"])
    assert not os.path.exists(os.path.dirname(multi["worktree_path"]))  # the per-story folder


def test_a_worktree_already_deleted_by_hand_is_not_an_error(world):
    gone, _kept = _story(world), _story(world)
    run_git(world.repo, "worktree", "remove", "--force", gone["worktree_path"])
    assert reclaim_finished_worktrees(world.sm, keep=1) == {"reclaimed": [], "kept": 1, "skipped": []}


def test_the_poll_loops_sweep_is_throttled_and_never_raises(world, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(worktree_retention, "reclaim_finished_worktrees", lambda sm, **kw: calls.append(1) or {"reclaimed": []})
    worktree_retention.reset_throttle()

    assert maybe_reclaim(world.sm, live=frozenset(), now=1000.0) == {"reclaimed": []}
    assert maybe_reclaim(world.sm, live=frozenset(), now=1000.0 + 10) is None
    assert maybe_reclaim(world.sm, live=frozenset(), now=1000.0 + worktree_retention.SWEEP_INTERVAL_SECONDS + 1) is not None
    assert len(calls) == 2

    def boom(sm, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worktree_retention, "reclaim_finished_worktrees", boom)
    worktree_retention.reset_throttle()
    assert maybe_reclaim(world.sm, live=frozenset(), now=1.0) is None
    worktree_retention.reset_throttle()


@pytest.mark.asyncio
async def test_poll_once_runs_the_sweep(world, monkeypatch):
    import forge.daemon.main as daemon

    seen: list[frozenset] = []
    monkeypatch.setattr(daemon, "maybe_reclaim", lambda sm, *, live: seen.append(live))
    await daemon.poll_once(world.sm, wait=True)
    assert len(seen) == 1
