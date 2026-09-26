"""Project CRUD, repo attachment (clones the given git URL into storage
this process controls — see forge/services/repo_clone.py for why),
project-scoped config overrides, and the project's saved JIRA ticket
filter. Epics live under a project — see forge/api/epic_routes.py for the
epic-facing endpoints that were adjusted to require a project_id.

The ticket-filter endpoints are the standing-query counterpart to that
module's one-key POST /api/epics/sync: `/ticket-filter` stores the query,
`/preview` shows what it would ingest without writing anything, and
`/sync` runs one sweep now. The unattended sweeps happen in
forge/daemon/ticket_sync.py — these routes exist so a human can set the
query up, test it, and not wait for the next cycle. `preview` and `sync`
do call JIRA inline, which is the same deal POST /api/epics/sync already
makes: a bounded, human-triggered fetch, not the long-running work the
daemon owns."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from jira import JIRAError
from pydantic import BaseModel

from forge.config.store import (
    PROJECT_OVERRIDABLE_KEYS,
    clear_project_config,
    get_config,
    list_project_config_for_display,
    set_project_config,
)
from forge.api.epic_routes import _with_stories
from forge.services.jira_filter import build_jql, preview_filter, sync_project_filter
from forge.services.project_guide import MAX_GUIDE_CHARS, GuideError, generate_project_guide, read_repo_guide
from forge.services.repo_clone import RepoAttachError, RepoCloneError, attach_local_repository, clone_repository
from forge.services.ticket_source import known_sources
from forge.state.manager import StateManager

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    name: str
    description: str = ""
    # At most one: a repo to clone, or a folder already on disk to attach.
    repo_url: str | None = None
    repo_path: str | None = None


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    ticket_source: str | None = None


class ProjectRepoCreateRequest(BaseModel):
    """Exactly one of `url` (clone it) or `path` (attach a repo already on
    disk) — see forge/services/repo_clone.py for the difference."""

    url: str | None = None
    path: str | None = None
    label: str | None = None


class ProjectConfigUpdate(BaseModel):
    value: str | None = None


class TicketFilterRequest(BaseModel):
    """The whole filter, every time — see StateManager.set_project_ticket_filter
    for why this is a replace rather than a patch."""

    enabled: bool = False
    jira_project_key: str | None = None
    assignee: str | None = None
    statuses: list[str] = []
    labels: list[str] = []
    issue_types: list[str] = []
    extra_jql: str | None = None


class TicketFilterRunRequest(BaseModel):
    """An unsaved filter to preview/sync instead of the saved one, so the
    settings dialog's "Test filter" works before the user commits to it.
    Omit `filter` to use whatever is stored."""

    filter: TicketFilterRequest | None = None


def _clone_dest_base_dir(project_id: int) -> str:
    base = get_config("cloned_repos_base_dir") or "/data/cloned-repos"
    return os.path.join(base, str(project_id))


def _attach_and_register(sm: StateManager, project_id: int, path: str, *, label: str | None = None) -> int:
    worktrees_base = get_config("worktrees_base_dir") or "/data/repos"
    try:
        folder = attach_local_repository(path, forbidden_dirs=[worktrees_base])
    except RepoAttachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if any(os.path.realpath(repo["path"]) == folder for repo in sm.list_project_repos(project_id)):
        raise HTTPException(status_code=409, detail=f"{folder} is already attached to this project")
    return sm.add_project_repo(project_id, folder, label=label or os.path.basename(folder))


def _add_repo(sm: StateManager, project_id: int, *, url: str | None, path: str | None, label: str | None = None) -> int:
    if bool((url or "").strip()) == bool((path or "").strip()):
        raise HTTPException(status_code=400, detail="Give either a repository URL to clone or a folder path to attach")
    if (path or "").strip():
        return _attach_and_register(sm, project_id, path or "", label=label)
    return _clone_and_register(sm, project_id, url or "", label=label)


def _clone_and_register(sm: StateManager, project_id: int, url: str, *, label: str | None = None) -> int:
    try:
        path = clone_repository(url, dest_base_dir=_clone_dest_base_dir(project_id))
    except RepoCloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return sm.add_project_repo(project_id, path, label=label or os.path.basename(path), clone_url=url)


@router.get("")
def list_projects() -> list[dict]:
    return StateManager().list_projects()


@router.post("")
def create_project(body: ProjectCreateRequest) -> dict:
    sm = StateManager()
    if (body.repo_url or "").strip() and (body.repo_path or "").strip():
        raise HTTPException(status_code=400, detail="Give either a repository URL to clone or a folder path to attach")
    if (body.repo_path or "").strip():
        # Checked before the project exists, so a mistyped path does not
        # leave an empty project behind (a clone can only fail by trying).
        try:
            attach_local_repository(body.repo_path or "", forbidden_dirs=[get_config("worktrees_base_dir") or "/data/repos"])
        except RepoAttachError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    project_id = sm.create_project(body.name, description=body.description)
    if (body.repo_url or "").strip() or (body.repo_path or "").strip():
        _add_repo(sm, project_id, url=body.repo_url, path=body.repo_path)
    return sm.get_project(project_id)


@router.get("/{project_id}")
def get_project(project_id: int) -> dict:
    project = StateManager().get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return project


@router.put("/{project_id}")
def update_project(project_id: int, body: ProjectUpdateRequest) -> dict:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    if body.ticket_source is not None and body.ticket_source not in known_sources():
        raise HTTPException(
            status_code=422,
            detail=f"Unknown ticket source {body.ticket_source!r} (available: {', '.join(known_sources())})",
        )
    sm.update_project(project_id, name=body.name, description=body.description, ticket_source=body.ticket_source)
    return sm.get_project(project_id)


@router.get("/{project_id}/repos")
def list_project_repos(project_id: int) -> list[dict]:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return sm.list_project_repos(project_id)


@router.post("/{project_id}/repos")
def add_project_repo(project_id: int, body: ProjectRepoCreateRequest) -> dict:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    repo_id = _add_repo(sm, project_id, url=body.url, path=body.path, label=body.label)
    return sm.get_project_repo(repo_id)


@router.delete("/{project_id}/repos/{repo_id}")
def remove_project_repo(project_id: int, repo_id: int) -> dict:
    sm = StateManager()
    repo = sm.get_project_repo(repo_id)
    if repo is None or repo["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"No such folder: {repo_id}")
    removed = sm.remove_project_repo(repo_id)
    if not removed:
        raise HTTPException(status_code=400, detail="Folder is still assigned to an epic — reassign it first")
    return {"repo_id": repo_id, "removed": True}


class RepoGuideRequest(BaseModel):
    # The guide text; empty discards the generated guide.
    content: str


def _repo_of(sm: StateManager, project_id: int, repo_id: int) -> dict:
    repo = sm.get_project_repo(repo_id)
    if repo is None or repo["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"No such folder: {repo_id}")
    return repo


def _guide_view(repo: dict) -> dict:
    """Which guide a run against this repo would read: the repo's own, else
    the one forge generated, else none (see forge/services/project_guide.py)."""
    own = read_repo_guide(repo["path"])
    if own:
        return {"source": "repo", "filename": own[0], "content": own[1], "generated_at": None}
    if repo.get("generated_guide"):
        return {"source": "generated", "filename": None, "content": repo["generated_guide"], "generated_at": repo["generated_guide_at"]}
    return {"source": "none", "filename": None, "content": None, "generated_at": None}


@router.get("/{project_id}/repos/{repo_id}/guide")
def get_repo_guide(project_id: int, repo_id: int) -> dict:
    return _guide_view(_repo_of(StateManager(), project_id, repo_id))


@router.post("/{project_id}/repos/{repo_id}/guide/generate")
async def generate_repo_guide(project_id: int, repo_id: int) -> dict:
    """Writes (or rewrites) forge's own guide for a repo that ships none.
    Runs whether or not the project's `generate_project_guide` setting is on:
    that setting only decides whether a run does it automatically."""
    sm = StateManager()
    repo = _repo_of(sm, project_id, repo_id)
    own = read_repo_guide(repo["path"])
    if own:
        raise HTTPException(
            status_code=409, detail=f"This repo already has its own {own[0]}; forge only writes a guide for one that has none"
        )
    try:
        guide = await generate_project_guide(repo["path"])
    except GuideError as exc:
        raise HTTPException(status_code=502, detail=f"Could not generate a guide: {exc}") from exc
    sm.set_project_repo_guide(repo_id, guide)
    return _guide_view(_repo_of(sm, project_id, repo_id))


@router.put("/{project_id}/repos/{repo_id}/guide")
def put_repo_guide(project_id: int, repo_id: int, body: RepoGuideRequest) -> dict:
    """Edit or discard the generated guide — it is read as authoritative, so a
    wrong line has to be fixable. Empty content discards it."""
    sm = StateManager()
    repo = _repo_of(sm, project_id, repo_id)
    own = read_repo_guide(repo["path"])
    if own:
        raise HTTPException(status_code=409, detail=f"This repo ships its own {own[0]}; change it in the repo")
    content = body.content.strip()
    if len(content) > MAX_GUIDE_CHARS:
        raise HTTPException(status_code=422, detail=f"A guide is capped at {MAX_GUIDE_CHARS} characters; this is {len(content)}")
    sm.set_project_repo_guide(repo_id, content or None)
    return _guide_view(_repo_of(sm, project_id, repo_id))


@router.get("/{project_id}/config")
def get_project_config_endpoint(project_id: int) -> list[dict]:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return list_project_config_for_display(project_id)


@router.put("/{project_id}/config/{key}")
def set_project_config_endpoint(project_id: int, key: str, body: ProjectConfigUpdate) -> dict:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    if key not in PROJECT_OVERRIDABLE_KEYS:
        raise HTTPException(status_code=400, detail=f"'{key}' is not a project-overridable setting")
    set_project_config(project_id, key, body.value)
    return {"key": key, "updated": True}


@router.delete("/{project_id}/config/{key}")
def clear_project_config_endpoint(project_id: int, key: str) -> dict:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    clear_project_config(project_id, key)
    return {"key": key, "cleared": True}


@router.get("/{project_id}/epics")
def list_project_epics(project_id: int) -> list[dict]:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")

    epics = []
    for epic_key in sm.list_all_epic_keys():
        epic = sm.get_epic(epic_key)
        if epic is None or epic["project_id"] != project_id:
            continue
        epics.append(_with_stories(sm, epic))
    return epics


# -- ticket filter ---------------------------------------------------------
# One saved JIRA query per project. See forge/services/jira_filter.py for
# what a sweep does and does not do (it only ever adds).


def _require_project(project_id: int) -> StateManager:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return sm


def _filter_response(ticket_filter: dict | None) -> dict | None:
    """Adds the compiled JQL so the UI can show exactly what will be sent
    to JIRA rather than making the user imagine it. A filter too empty to
    compile reports `jql: null` instead of failing the read — an
    in-progress filter is a normal thing to have saved."""
    if ticket_filter is None:
        return None
    try:
        jql = build_jql(ticket_filter)
    except ValueError:
        jql = None
    return {**ticket_filter, "jql": jql}


@router.get("/{project_id}/ticket-filter")
def get_ticket_filter(project_id: int) -> dict:
    """`{"filter": null}` means no filter has ever been saved — distinct
    from a saved-but-switched-off one, which is a filter with
    `enabled: false`."""
    sm = _require_project(project_id)
    return {"filter": _filter_response(sm.get_project_ticket_filter(project_id))}


@router.put("/{project_id}/ticket-filter")
def set_ticket_filter(project_id: int, body: TicketFilterRequest) -> dict:
    sm = _require_project(project_id)
    try:
        build_jql(body.model_dump())  # reject an all-empty filter at the door
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved = sm.set_project_ticket_filter(
        project_id,
        enabled=body.enabled,
        jira_project_key=body.jira_project_key,
        assignee=body.assignee,
        statuses=body.statuses,
        labels=body.labels,
        issue_types=body.issue_types,
        extra_jql=body.extra_jql,
    )
    return {"filter": _filter_response(saved)}


@router.delete("/{project_id}/ticket-filter")
def delete_ticket_filter(project_id: int) -> dict:
    """Stops all ingestion for this project. Epics already synced by it are
    left exactly as they are — forgetting the query never unsyncs work."""
    sm = _require_project(project_id)
    return {"project_id": project_id, "cleared": sm.clear_project_ticket_filter(project_id)}


def _run_filter(project_id: int, body: TicketFilterRunRequest | None, runner) -> dict:
    _require_project(project_id)
    unsaved = body.filter.model_dump() if body is not None and body.filter is not None else None
    try:
        return runner(project_id, unsaved)
    except ValueError as exc:  # empty filter, no saved filter, or JIRA not configured
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JIRAError as exc:
        # 400 from JIRA here is almost always a JQL the user typed into the
        # escape hatch, so its text goes back verbatim — that message is the
        # only thing that makes the mistake fixable.
        status = 400 if exc.status_code == 400 else 502
        raise HTTPException(status_code=status, detail=(exc.text or str(exc))) from exc


@router.post("/{project_id}/ticket-filter/preview")
def preview_ticket_filter(project_id: int, body: TicketFilterRunRequest | None = None) -> dict:
    """Dry run: what this filter matches and what a sweep would do with
    each match. Writes nothing."""
    return _run_filter(project_id, body, preview_filter)


@router.post("/{project_id}/ticket-filter/sync")
def run_ticket_filter_sync(project_id: int, body: TicketFilterRunRequest | None = None) -> dict:
    """One sweep now, instead of waiting for the background loop. Works on
    a disabled filter too — `enabled` only governs the unattended sweeps."""
    return _run_filter(project_id, body, sync_project_filter)
