"""Epic/story views for the dashboard, plus the JIRA-sync endpoint that
registers an epic and its child tickets under a project so the daemon has
something to poll, the per-epic prose refresh (summary/description only),
the folder-assignment endpoint, and "look again" for prior work (branches
that already carry a ticket's work — forge/services/prior_work.py). A new
epic gets a default repo
assigned automatically (forge/services/epic_sync.py, forge/daemon/main.py's
poll_once backfills any that still slip through) — the endpoint below only
exists for a human override, never a required setup step."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from jira import JIRAError
from pydantic import BaseModel

from forge.daemon.ticket_removal import TicketBusy, TicketNotFound, TicketRemovalError, remove_epic as remove_epic_ticket
from forge.orchestration.reconciler import display_epic_phase
from forge.services.epic_sync import sync_epic_fields_from_jira, sync_epic_from_jira
from forge.services.usage import get_epic_usage, get_run_outcome
from forge.state.manager import StateManager

router = APIRouter(prefix="/api/epics", tags=["epics"])


class EpicSyncRequest(BaseModel):
    jira_key: str
    project_id: int


class EpicRepoRequest(BaseModel):
    project_repo_id: int


class EpicGateRequest(BaseModel):
    manual_gate: bool


def _story_with_pr(sm: StateManager, story_key: str) -> dict:
    story = sm.get_story(story_key) or {}
    story["pull_request"] = sm.get_pull_request(story_key)
    # thread_id == run_id == story_key by construction (forge/orchestration/
    # graph.py). None until the story's first dispatch. Lets the dashboard
    # tell "dev wrote real code, the build/verify step failed" apart from
    # "this never got past planning" on a failed story (see
    # forge/services/usage.py's record_run_outcome for the accumulate-
    # across-retries caveat on these counters).
    story["run_outcome"] = get_run_outcome(story_key) if story.get("thread_id") else None
    return story


def _with_stories(sm: StateManager, epic: dict) -> dict:
    """The epic as the dashboard shows it: its stories attached, and its
    phase read off them (see display_epic_phase for why the stored one can't
    be shown as is)."""
    epic["stories"] = [_story_with_pr(sm, s["jira_key"]) for s in sm.list_stories_for_epic(epic["jira_key"])]
    # The epic's own pull request, one per repo — the only one forge opens.
    epic["pull_requests"] = sm.list_epic_pull_requests(epic["jira_key"])
    epic["phase"] = display_epic_phase(epic["phase"], (s.get("status") for s in epic["stories"]))
    return epic


@router.get("")
def list_epics() -> list[dict]:
    sm = StateManager()
    epics = []
    for epic_key in sm.list_all_epic_keys():
        epic = sm.get_epic(epic_key)
        if epic is None:
            continue
        epics.append(_with_stories(sm, epic))
    return epics


@router.get("/{epic_key}")
def get_epic(epic_key: str) -> dict:
    sm = StateManager()
    epic = sm.get_epic(epic_key)
    if epic is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    return _with_stories(sm, epic)


@router.get("/{epic_key}/usage")
def get_epic_usage_route(epic_key: str) -> dict:
    """Token/cache/cost/time rollup for every top-level story in the epic,
    grouped by wave — the epic detail page's Usage panel fetches this
    on-demand (not on the fast poll cycle GET /api/epics/{epic_key} uses,
    since this scans every story's usage rows)."""
    sm = StateManager()
    if sm.get_epic(epic_key) is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    return get_epic_usage(epic_key)


@router.post("/sync")
def sync_epic(body: EpicSyncRequest) -> dict:
    sm = StateManager()
    if sm.get_project(body.project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {body.project_id}")
    try:
        report = sync_epic_from_jira(body.jira_key, project_id=body.project_id)
    except ValueError as exc:  # JIRA not configured, auth failed, or epic belongs to another project
        status = 409 if "different project" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except JIRAError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404, detail=f"JIRA issue {body.jira_key} not found or not accessible"
            ) from exc
        raise HTTPException(status_code=502, detail=(exc.text or str(exc))) from exc
    # By the key the epic is stored under — what was typed may be a URL.
    return get_epic(report.epic_key)


@router.post("/{epic_key}/sync")
def sync_epic_fields(epic_key: str) -> dict:
    """Refresh summary/description from JIRA for one already-synced epic.
    Does not walk children — use POST /api/epics/sync for that."""
    try:
        sync_epic_fields_from_jira(epic_key)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail.startswith("No such ") else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    except JIRAError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404, detail=f"JIRA issue {epic_key} not found or not accessible"
            ) from exc
        raise HTTPException(status_code=502, detail=(exc.text or str(exc))) from exc
    return get_epic(epic_key)


@router.put("/{epic_key}/repo")
def set_epic_repo(epic_key: str, body: EpicRepoRequest) -> dict:
    sm = StateManager()
    epic = sm.get_epic(epic_key)
    if epic is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    project_repo = sm.get_project_repo(body.project_repo_id)
    if project_repo is None or project_repo["project_id"] != epic["project_id"]:
        raise HTTPException(status_code=400, detail="That folder does not belong to this epic's project")
    sm.set_epic_project_repo(epic_key, body.project_repo_id)
    return get_epic(epic_key)


@router.delete("/{epic_key}")
async def remove_epic(epic_key: str) -> dict:
    """Remove the ticket from forge, locally and completely: its rows, its
    worktrees and local branches, its run checkpoints. Nothing is sent to
    GitHub or JIRA — see forge/daemon/ticket_removal.py for what is and is not
    touched, and what the report lists as left behind."""
    try:
        return await remove_epic_ticket(StateManager(), epic_key)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TicketBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TicketRemovalError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{epic_key}/prior-work/refresh")
def refresh_prior_work(epic_key: str) -> dict:
    """Look again for work that already exists — e.g. after a branch was
    pushed since the epic was added. Marks the lookup of every story that
    has not started (and the epic's own, until its branch exists) for a
    fresh one; the next daemon poll looks them up again, before any of them
    is dispatched. A branch a person picked stays picked where it still
    applies. A story already underway keeps the branch it started from."""
    sm = StateManager()
    epic = sm.get_epic(epic_key)
    if epic is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    for story in sm.list_stories_for_epic(epic_key):
        prior = story.get("prior_work")
        if story["status"] == "pending" and not story.get("branch") and prior is not None:
            sm.set_story_prior_work(story["jira_key"], {"stale": True, "picks": prior.get("picks") or {}})
    if not epic.get("integration_branch"):
        sm.set_epic_prior_work(epic_key, None)
    return get_epic(epic_key)


@router.put("/{epic_key}/gate")
def set_epic_gate(epic_key: str, body: EpicGateRequest) -> dict:
    sm = StateManager()
    if sm.get_epic(epic_key) is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    sm.set_epic_manual_gate(epic_key, body.manual_gate)
    return get_epic(epic_key)
