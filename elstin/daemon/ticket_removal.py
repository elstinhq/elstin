"""Remove a ticket from elstin, locally and completely.

"Ticket" is what the dashboard's Add ticket adds: an epic, or a lone story
that is its own epic. Removing one forgets the epic, its stories and sub-tasks
and everything they left behind:

- the rows (epic, stories, pull-request records, worktree records, activity
  events, token usage, run outcomes, watermarks),
- each story's worktree and local branch, and the epic's integration
  worktree and branch, in every repo the ticket touched,
- the story and integration directories under the worktrees folder, the
  dev loop's baseline snapshots, and any downloaded attachments,
- the LangGraph checkpoints for each story's thread. These matter for more
  than tidiness: a ticket added again under the same key would otherwise
  resume the old thread's state.

It is LOCAL ONLY. Nothing is sent to GitHub or JIRA: a pull request that was
opened stays open, and a branch that was pushed stays pushed. The report says
which, so they can be closed by hand. And elstin keeps no record that the
ticket was removed, so a project whose ticket filter still matches it will
ingest it again on the next sweep — the report says when that is possible.

Order matters. A run in flight is refused (and the story keys are claimed for
the duration so the poller or /start cannot begin one meanwhile). The
footprint is read first; then the filesystem, then checkpoints, then the rows
last. A failure before the rows go leaves the ticket listed and removable
again, rather than files that no row points at. Filesystem problems are
warnings — a worktree already deleted by hand is not a reason to keep a
ticket — but a checkpoint failure stops the removal, because stale checkpoints
would haunt a re-added ticket.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any

from elstin.config.store import get_config
from elstin.daemon.main import claim_run, release_run
from elstin.db.engine import get_database_url
from elstin.orchestration.graph import checkpointer_from_url
from elstin.orchestration.integration import integration_branch_name, integration_worktree_path
from elstin.state.manager import StateManager
from elstin.tools.worktree import discard_worktree

logger = logging.getLogger("elstin.ticket_removal")


class TicketNotFound(LookupError):
    """No epic with that key."""


class TicketBusy(RuntimeError):
    """A story of the ticket has a run in flight."""


class TicketRemovalError(RuntimeError):
    """Removal stopped before the rows were deleted; the ticket is still
    listed and can be removed again."""


def _inside(path: str, base: str) -> bool:
    real_base = os.path.realpath(base)
    real = os.path.realpath(path)
    return real != real_base and os.path.commonpath([real, real_base]) == real_base


def _remove_dir(path: str, base: str, removed: list[str], warnings: list[str]) -> None:
    """Delete `path` if it exists and really is under `base` — never a path
    that a bad key or a symlink would point somewhere else."""
    if not os.path.lexists(path):
        return
    if os.path.islink(path):
        os.unlink(path)
        removed.append(path)
        return
    if not _inside(path, base):
        warnings.append(f"not removing {path}: it is not under {base}")
        return
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        warnings.append(f"could not remove {path}")
    else:
        removed.append(path)


def _clean_local_artifacts(footprint: dict[str, Any]) -> dict[str, Any]:
    """Worktrees, branches and directories. Never raises on a missing or stuck
    path; returns what went and what could not."""
    worktrees_base = get_config("worktrees_base_dir") or "/data/repos"
    baselines_base = os.path.join(get_config("data_dir") or "/data", "baselines")
    attachments_base = get_config("jira_attachments_dir") or "/data/jira-attachments"
    epic_key = footprint["epic"]

    removed_worktrees: list[str] = []
    deleted_branches: list[str] = []
    removed_dirs: list[str] = []
    warnings: list[str] = []

    # Every (repo, worktree, branch) a story owns: the per-repo rows, plus the
    # default-repo mirror on the story itself for one dispatched before those
    # rows existed.
    targets: list[tuple[str | None, str | None, str | None]] = [
        (w["repo_path"], w["worktree_path"], w["branch"]) for w in footprint["worktrees"]
    ]
    for story in footprint["stories"]:
        if story.get("worktree_path") or story.get("branch"):
            targets.append((story.get("repo"), story.get("worktree_path"), story.get("branch")))

    repos = {r for r in (footprint["epic_repo"], *footprint.get("project_repos", ()), *(t[0] for t in targets)) if r}
    done: set[tuple[str | None, str | None, str | None]] = set()
    for repo, worktree, branch in targets:
        if (repo, worktree, branch) in done:
            continue
        done.add((repo, worktree, branch))
        if repo is None:
            if worktree:
                _remove_dir(worktree, worktrees_base, removed_worktrees, warnings)
            continue
        result = discard_worktree(repo, worktree, branch)
        if result["worktree_removed"] and worktree:
            removed_worktrees.append(worktree)
        if result["branch_deleted"] and branch:
            deleted_branches.append(branch)
        warnings.extend(result["errors"])

    # The epic's integration branch exists in each repo on the project, each
    # with its own worktree (see integration_worktree_path).
    # discard_worktree tolerates a repo where it was never made.
    integration_branches = {integration_branch_name(epic_key), footprint["integration_branch"]} - {None}
    # Every path first: which repo's worktree is the `_integration-<epic>`
    # one and which a `-<repo>` sibling is read off the disk, so computing a
    # path after an earlier repo's worktree is gone would name the wrong one.
    by_repo = {repo: integration_worktree_path(worktrees_base, epic_key, repo) for repo in sorted(repos)}
    integration_worktrees = list(by_repo.values())
    for repo, integration_worktree in by_repo.items():
        for branch in sorted(integration_branches):
            result = discard_worktree(repo, integration_worktree, branch)
            if result["worktree_removed"] and integration_worktree not in removed_worktrees:
                removed_worktrees.append(integration_worktree)
            if result["branch_deleted"]:
                deleted_branches.append(branch)
            warnings.extend(result["errors"])

    for integration_worktree in dict.fromkeys([*integration_worktrees, integration_worktree_path(worktrees_base, epic_key)]):
        _remove_dir(integration_worktree, worktrees_base, removed_dirs, warnings)
    for key in footprint["keys"]:
        _remove_dir(os.path.join(worktrees_base, key.lower()), worktrees_base, removed_dirs, warnings)
        _remove_dir(os.path.join(baselines_base, key), baselines_base, removed_dirs, warnings)
        _remove_dir(os.path.join(attachments_base, key), attachments_base, removed_dirs, warnings)

    return {
        "worktrees": removed_worktrees,
        "branches": sorted(set(deleted_branches)),
        "directories": removed_dirs,
        "warnings": warnings,
    }


async def _delete_checkpoints(keys: list[str]) -> int:
    """Clear each story's LangGraph thread (thread_id == story key)."""
    try:
        async with checkpointer_from_url(get_database_url()) as saver:
            for key in keys:
                await saver.adelete_thread(key)
    except Exception as exc:  # noqa: BLE001 — surfaced as a refusal to finish, below
        raise TicketRemovalError(
            f"Could not clear the run checkpoints ({exc}). Nothing was deleted from the board; try again."
        ) from exc
    return len(keys)


async def remove_epic(sm: StateManager, epic_key: str) -> dict[str, Any]:
    """Remove the ticket rooted at `epic_key`. Raises TicketNotFound,
    TicketBusy (a run is in flight) or TicketRemovalError (stopped before the
    rows went)."""
    footprint = await asyncio.to_thread(sm.get_ticket_footprint, epic_key)
    if footprint is None:
        raise TicketNotFound(f"No such epic: {epic_key}")

    claimed: list[str] = []
    try:
        for key in footprint["keys"]:
            if not claim_run(key):
                raise TicketBusy(f"{key} has a run in progress. Let it finish before removing this ticket.")
            claimed.append(key)

        local = await asyncio.to_thread(_clean_local_artifacts, footprint)
        checkpoint_threads = await _delete_checkpoints(footprint["keys"])
        counts = await asyncio.to_thread(sm.delete_ticket_records, epic_key)
    finally:
        for key in claimed:
            release_run(key)

    ticket_filter = await asyncio.to_thread(sm.get_project_ticket_filter, footprint["project_id"])
    with_prs = {p["story_key"] for p in footprint["pull_requests"] if p["number"]}
    pushed_branches = sorted({
        branch for branch, story_key in (
            [(w["branch"], w["story_key"]) for w in footprint["worktrees"]]
            + [(s.get("branch"), s["jira_key"]) for s in footprint["stories"]]
        ) if branch and story_key in with_prs
    })

    report = {
        "epic": epic_key,
        "removed": {
            "stories": counts["stories"],
            "pull_request_records": counts["pull_requests"],
            "activity_events": counts["activity_events"],
            "usage_rows": counts["usage_rows"],
            "run_outcomes": counts["run_outcomes"],
            # Proposed lessons whose only evidence was this ticket are dropped;
            # the rest just stop citing it (state/manager_lesson.py).
            "lessons_detached": counts["lessons_detached"],
            "lessons_dropped": counts["lessons_dropped"],
            "checkpoint_threads": checkpoint_threads,
            **{k: local[k] for k in ("worktrees", "branches", "directories")},
        },
        # Nothing here was touched: elstin's removal is local only.
        "left_on_remote": {
            "pull_requests": [
                {"story_key": p["story_key"], "number": p["number"], "url": p["url"], "state": p["state"]}
                for p in footprint["pull_requests"] if p["number"]
            ],
            "branches_that_may_be_pushed": pushed_branches,
        },
        "ticket_filter_may_readd": bool(ticket_filter and ticket_filter.get("enabled")),
        "warnings": local["warnings"],
    }
    logger.info(
        "removed ticket %s: %d story row(s), %d worktree(s), %d branch(es), %d warning(s)",
        epic_key, counts["stories"], len(local["worktrees"]), len(local["branches"]), len(local["warnings"]),
    )
    return report
