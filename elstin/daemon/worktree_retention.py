"""Reclaiming the disk a finished story's worktree holds.

Every story gets a full checkout per repo it writes — `node_modules/`,
`target/` and all — and nothing took one away once the story had merged into the
epic branch: only removing the whole ticket did. On a laptop that is the disk
filling up one epic at a time.

Once a story has merged, its code lives on the epic branch and its own branch
still holds every commit, so the directory is only convenience: somewhere to
look at what dev did. This keeps the newest `worktree_retention` finished
stories' worktrees for that (default 10; 0 keeps everything) and removes the
rest. What goes:

* the **directory** and git's registration of it — never the branch, so the work
  stays recoverable with `git worktree add <path> <branch>`;
* only for a story that is finished (`merged`, `done`, `skipped`), has no run in
  flight, and whose worktree sits under `worktrees_base_dir` (a path recorded
  before that setting changed is left alone, as is anything outside it — an
  attached repo's own checkout is never under it);
* for a `done` or `skipped` story, only when the worktree is clean and carries
  nothing the epic branch lacks. A `merged` story's commits are in the epic by
  definition, so only its disposable leftovers are lost.

A story that later needs a worktree again (a re-plan, say) gets one made on its
existing branch, like any dispatch. The `story_worktrees` rows are left as they
are: every reader of them already tolerates a directory that is gone.

Best effort and idempotent: one stuck path is logged and skipped, the sweep never
raises into the poll loop, and a second pass over the same state does nothing.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Any

from elstin.config.store import get_config, get_int_config
from elstin.state.manager import StateManager
from elstin.tools.worktree import discard_worktree, run_git

logger = logging.getLogger("elstin.daemon")

FINISHED_STATUSES = ("merged", "done", "skipped")
DEFAULT_RETENTION = 10

# The sweep walks the filesystem, and what it frees changes by the hour, not by
# the poll. poll_once asks every cycle; this answers at most this often.
SWEEP_INTERVAL_SECONDS = 300.0

_last_sweep: float | None = None


def _under(path: str, base: str) -> bool:
    real_path, real_base = os.path.realpath(path), os.path.realpath(base)
    return real_path != real_base and os.path.commonpath([real_path, real_base]) == real_base


def _holds_nothing_unmerged(row: dict[str, Any]) -> str | None:
    """Why this worktree must stay (a `done`/`skipped` story's work is not
    known to be anywhere else), or None when it can go."""
    path, epic_branch = row["worktree_path"], row.get("integration_branch")
    status = run_git(path, "status", "--porcelain", check=False)
    if status.returncode != 0:
        return "git could not read its status"
    if status.stdout.strip():
        return "it has uncommitted changes"
    if not epic_branch:
        return "its epic has no branch to compare with"
    ahead = run_git(path, "rev-list", "--count", f"{epic_branch}..HEAD", check=False)
    if ahead.returncode != 0:
        return "git could not compare it with the epic branch"
    if int(ahead.stdout.strip() or 0) > 0:
        return "it has commits the epic branch lacks"
    return None


def reclaim_finished_worktrees(
    sm: StateManager, *, keep: int | None = None, live: frozenset[str] = frozenset(), base_dir: str | None = None,
) -> dict[str, Any]:
    """Remove the worktrees of finished stories beyond the newest `keep`
    (default: the `worktree_retention` setting). Returns {"reclaimed": [story
    keys], "kept": n, "skipped": [{"story_key", "reason"}]} — nothing is
    reclaimed when retention is 0 or unset-to-unlimited."""
    keep = max(0, get_int_config("worktree_retention", DEFAULT_RETENTION)) if keep is None else keep
    report: dict[str, Any] = {"reclaimed": [], "kept": 0, "skipped": []}
    if keep <= 0:
        return report
    base = base_dir or get_config("worktrees_base_dir") or "/data/repos"

    # Stories that still have something on disk, newest first. One already
    # reclaimed takes no slot: the limit is about what is occupying disk.
    by_story: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sm.list_worktrees_of_stories_in(FINISHED_STATUSES):
        if row["worktree_path"] and os.path.isdir(row["worktree_path"]):
            by_story[row["story_key"]].append(row)

    stories = list(by_story)  # insertion order is newest-first
    report["kept"] = min(len(stories), keep)
    for story_key in stories[keep:]:
        rows = by_story[story_key]
        if story_key in live:
            report["skipped"].append({"story_key": story_key, "reason": "a run is in flight"})
            continue
        if any(not _under(r["worktree_path"], base) for r in rows):
            report["skipped"].append({"story_key": story_key, "reason": "a worktree is outside worktrees_base_dir"})
            continue
        reason = next(
            (why for r in rows if r["status"] != "merged" and (why := _holds_nothing_unmerged(r))), None,
        )
        if reason:
            report["skipped"].append({"story_key": story_key, "reason": reason})
            continue

        failed = False
        for row in rows:
            # branch=None: the directory goes, the branch stays.
            result = discard_worktree(row["repo_path"], row["worktree_path"], None)
            if result["errors"]:
                failed = True
                logger.warning("story %s: could not reclaim %s: %s", story_key, row["worktree_path"], result["errors"])
        story_dir = os.path.dirname(rows[0]["worktree_path"])
        if rows[0]["repo_label"] and _under(story_dir, base):
            try:
                os.rmdir(story_dir)  # the per-story folder a multi-repo story nests its repos in
            except OSError:
                pass  # not empty, or already gone
        if failed:
            report["skipped"].append({"story_key": story_key, "reason": "a worktree could not be removed"})
        else:
            report["reclaimed"].append(story_key)

    if report["reclaimed"]:
        logger.info(
            "reclaimed the worktrees of %d finished stories (kept the newest %d): %s",
            len(report["reclaimed"]), keep, ", ".join(report["reclaimed"]),
        )
    return report


def maybe_reclaim(sm: StateManager, *, live: frozenset[str], now: float | None = None) -> dict[str, Any] | None:
    """`reclaim_finished_worktrees`, at most once per SWEEP_INTERVAL_SECONDS and
    never raising — for the poll loop. None when it did not run."""
    global _last_sweep
    moment = time.monotonic() if now is None else now
    if _last_sweep is not None and moment - _last_sweep < SWEEP_INTERVAL_SECONDS:
        return None
    _last_sweep = moment
    try:
        return reclaim_finished_worktrees(sm, live=live)
    except Exception:  # noqa: BLE001 - housekeeping must never stall the poll loop
        logger.exception("could not reclaim finished worktrees")
        return None


def reset_throttle() -> None:
    global _last_sweep
    _last_sweep = None
