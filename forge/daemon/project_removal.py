"""Delete a project from forge, locally and completely.

A project owns its tickets, its folders (repos), its settings overrides and
its ticket filter. Deleting it removes all of that from the database and
everything forge put on disk for it:

- each ticket, exactly as "Remove ticket" does (forge/daemon/ticket_removal.py):
  rows, worktrees, local branches, the epic branch in every repo, run
  checkpoints, baselines and attachments;
- the clones forge made for it (a folder added by URL lives under
  `cloned_repos_base_dir/<project id>`, which forge owns);
- the project's rows: settings overrides, ticket filter, folders, project.

Two things it never deletes. A folder that was ATTACHED is someone's own
checkout: forge removes the branches and worktrees it added there (the
ticket removals above) and leaves the folder itself in place. And nothing is
sent to GitHub or JIRA — an epic's pull request stays open and a pushed
branch stays pushed; the report lists them.

Order, and what a failure leaves. A run in flight anywhere in the project is
refused up front, before anything is touched. Then the ticket filter is
dropped and every epic put on hold, so neither the sweep nor the poller
starts new work while tickets are being removed. Tickets go one at a time; a
failure stops there with the project still listed (and the remaining epics
on hold), and deleting it again finishes the job. Clones and the project's
own rows go last.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any

from forge.config.store import get_config
from forge.daemon.main import live_run_keys
from forge.daemon.ticket_removal import TicketBusy, TicketNotFound, TicketRemovalError, remove_epic
from forge.state.manager import StateManager
from forge.state.manager_project import ProjectNotEmpty

logger = logging.getLogger("forge.project_removal")


class ProjectNotFound(LookupError):
    """No project with that id."""


class ProjectBusy(RuntimeError):
    """A story of the project has a run or a merge in flight."""


class ProjectRemovalError(RuntimeError):
    """Deletion stopped part-way; the project is still listed and deleting it
    again continues from where this stopped."""


async def remove_project(sm: StateManager, project_id: int) -> dict[str, Any]:
    """Delete project `project_id` and everything it owns (see the module
    docstring). Raises ProjectNotFound, ProjectBusy or ProjectRemovalError."""
    project = await asyncio.to_thread(sm.get_project, project_id)
    if project is None:
        raise ProjectNotFound(f"No such project: {project_id}")

    epic_keys = await asyncio.to_thread(sm.list_project_epic_keys, project_id)
    running = sorted(set(await asyncio.to_thread(_story_keys, sm, epic_keys)) & live_run_keys())
    if running:
        raise ProjectBusy(
            f"{', '.join(running)} {'has' if len(running) == 1 else 'have'} a run in progress. "
            "Cancel it or let it finish before deleting this project."
        )

    # Nothing new may start while tickets are being removed: no sweep
    # ingesting more, no poll dispatching a story of a ticket not yet gone.
    filter_dropped = await asyncio.to_thread(sm.clear_project_ticket_filter, project_id)
    for key in epic_keys:
        await asyncio.to_thread(sm.set_epic_manual_gate, key, True)

    tickets: list[dict[str, Any]] = []
    for key in epic_keys:
        try:
            tickets.append(await remove_epic(sm, key))
        except TicketNotFound:
            continue  # removed meanwhile
        except TicketBusy as exc:
            raise ProjectBusy(f"{exc} {len(tickets)} of {len(epic_keys)} tickets were removed; delete the project again to finish.") from exc
        except TicketRemovalError as exc:
            raise ProjectRemovalError(
                f"Stopped while removing {key}: {exc} {len(tickets)} of {len(epic_keys)} tickets were removed; "
                "delete the project again to finish."
            ) from exc

    repos = await asyncio.to_thread(sm.list_project_repos, project_id)
    local = await asyncio.to_thread(_remove_clones, project_id, repos)
    try:
        rows = await asyncio.to_thread(sm.delete_project_records, project_id)
    except ProjectNotEmpty as exc:
        raise ProjectRemovalError(f"{exc} — added while the others were being removed. Delete the project again.") from exc

    report = {
        "project_id": project_id,
        "name": project.get("name"),
        "removed": {
            "tickets": [t["epic"] for t in tickets],
            "clones": local["clones"],
            **(rows or {}),
            # Dropped first, above — or by an earlier attempt that stopped part-way.
            "ticket_filters": int(bool(filter_dropped)) + (rows or {}).get("ticket_filters", 0),
        },
        # Someone's own checkouts: forge's branches and worktrees in them are
        # gone (with the tickets), the folders are not.
        "attached_folders_kept": [r["path"] for r in repos if not r.get("clone_url")],
        "left_on_remote": {
            "pull_requests": [pr for t in tickets for pr in t["left_on_remote"]["pull_requests"]],
            "branches_that_may_be_pushed": sorted({b for t in tickets for b in t["left_on_remote"]["branches_that_may_be_pushed"]}),
        },
        "warnings": [w for t in tickets for w in t["warnings"]] + local["warnings"],
    }
    logger.info(
        "deleted project %s (%s): %d ticket(s), %d clone(s), %d warning(s)",
        project_id, project.get("name"), len(tickets), len(local["clones"]), len(report["warnings"]),
    )
    return report


def _story_keys(sm: StateManager, epic_keys: list[str]) -> list[str]:
    keys = list(epic_keys)
    for epic_key in epic_keys:
        keys.extend(story["jira_key"] for story in sm.list_stories_for_epic(epic_key))
    return keys


def _remove_clones(project_id: int, repos: list[dict]) -> dict[str, list[str]]:
    """Delete the clones forge made for this project — each only if it really
    is under the project's own clone folder (`cloned_repos_base_dir/<id>`), so
    a stored path can never send this outside it — then that folder.
    Attached folders are not touched."""
    base = os.path.realpath(os.path.join(get_config("cloned_repos_base_dir") or "/data/cloned-repos", str(project_id)))
    clones: list[str] = []
    warnings: list[str] = []
    for repo in repos:
        if not repo.get("clone_url"):
            continue
        path = os.path.realpath(repo["path"])
        if not _inside(path, base):
            warnings.append(f"not deleting the clone at {repo['path']}: it is not under {base}")
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            if os.path.exists(path):
                warnings.append(f"could not delete the clone at {path}")
                continue
        clones.append(repo["path"])
    if os.path.isdir(base):
        try:
            os.rmdir(base)  # only once empty: anything else in it was not forge's to delete
        except OSError:
            warnings.append(f"left {base} in place: it still holds files that are not this project's clones")
    return {"clones": clones, "warnings": warnings}


def _inside(path: str, base: str) -> bool:
    return path != base and os.path.commonpath([path, base]) == base
