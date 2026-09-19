"""Epic/story views for the dashboard, plus the JIRA-sync endpoint that
registers an epic and its child tickets under a project so the daemon has
something to poll, the per-epic prose refresh (summary/description only),
and the folder-assignment endpoint that's kept deliberately separate —
syncing tickets from JIRA and picking one of the project's folders are
two independent actions (the daemon leaves an epic alone until a folder
is set; see forge/daemon/main.py's poll_once)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from jira import JIRAError
from pydantic import BaseModel

from forge.services.epic_sync import sync_epic_fields_from_jira, sync_epic_from_jira
from forge.services.usage import get_epic_usage
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
    return story


@router.get("")
def list_epics() -> list[dict]:
    sm = StateManager()
    epics = []
    for epic_key in sm.list_all_epic_keys():
        epic = sm.get_epic(epic_key)
        if epic is None:
            continue
        epic["stories"] = [_story_with_pr(sm, s["jira_key"]) for s in sm.list_stories_for_epic(epic_key)]
        epics.append(epic)
    return epics


@router.get("/{epic_key}")
def get_epic(epic_key: str) -> dict:
    sm = StateManager()
    epic = sm.get_epic(epic_key)
    if epic is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    epic["stories"] = [_story_with_pr(sm, s["jira_key"]) for s in sm.list_stories_for_epic(epic_key)]
    return epic


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


@router.put("/{epic_key}/gate")
def set_epic_gate(epic_key: str, body: EpicGateRequest) -> dict:
    sm = StateManager()
    if sm.get_epic(epic_key) is None:
        raise HTTPException(status_code=404, detail=f"No such epic: {epic_key}")
    sm.set_epic_manual_gate(epic_key, body.manual_gate)
    return get_epic(epic_key)
