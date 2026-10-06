"""Project CRUD (DELETE removes the project and everything it owns — see
elstin/daemon/project_removal.py), repo attachment (clones the given git URL
into storage this process controls, or attaches a folder already on disk —
see elstin/services/repo_clone.py),
project-scoped config overrides, and the project's saved JIRA ticket
filter. Epics live under a project — see elstin/api/epic_routes.py for the
epic-facing endpoints that were adjusted to require a project_id.

The ticket-filter endpoints are the standing-query counterpart to that
module's one-key POST /api/epics/sync: `/ticket-filter` stores the query,
`/preview` shows what it would ingest without writing anything, and
`/sync` runs one sweep now. The unattended sweeps happen in
elstin/daemon/ticket_sync.py — these routes exist so a human can set the
query up, test it, and not wait for the next cycle. `preview` and `sync`
do call JIRA inline, which is the same deal POST /api/epics/sync already
makes: a bounded, human-triggered fetch, not the long-running work the
daemon owns."""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from jira import JIRAError
from pydantic import BaseModel

from elstin.daemon.project_removal import ProjectBusy, ProjectNotFound, ProjectRemovalError, remove_project
from elstin.tools.reference_folder import remove_reference_copy
from elstin.config.store import (
    PROJECT_OVERRIDABLE_KEYS,
    clear_project_config,
    get_config,
    list_project_config_for_display,
    set_project_config,
)
from elstin.api.epic_routes import _with_stories
from elstin.services.jira_filter import build_jql, preview_filter, sync_project_filter
from elstin.services.lessons import MAX_LESSON_CHARS
from elstin.services.project_guide import MAX_GUIDE_CHARS, GuideError, generate_project_guide, read_repo_guide
from elstin.services.repo_clone import (
    RepoAttachError, RepoCloneError, attach_local_repository, attach_reference_folder, browse_folder, clone_repository,
    scan_folder,
)
from elstin.services.ticket_source import known_sources
from elstin.state.manager import StateManager

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    name: str
    description: str = ""
    # A repo to clone, or folder(s) already on disk to attach — not both.
    # `repo_paths` is several repos picked from one workspace folder
    # (POST /scan-folder); `repo_path` the single-repo shorthand.
    repo_url: str | None = None
    repo_path: str | None = None
    repo_paths: list[str] | None = None
    # Of the paths above, the ones to attach read-only.
    read_only_paths: list[str] | None = None
    # A plain folder to read for context (its CLAUDE.md, docs) — usually the
    # workspace folder the repos above were picked from.
    reference_path: str | None = None


class BrowseFolderRequest(BaseModel):
    # Home when empty.
    path: str | None = None


class ScanFolderRequest(BaseModel):
    path: str
    # Marks the repos already attached to this project, when given.
    project_id: int | None = None


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    ticket_source: str | None = None


class ProjectRepoCreateRequest(BaseModel):
    """Exactly one of `url` (clone it) or `path` (attach a repo already on
    disk) — see elstin/services/repo_clone.py for the difference. `kind`
    "reference" attaches a plain folder read for context, never written
    (path only); `read_only` a git repo no plan may change."""

    url: str | None = None
    path: str | None = None
    label: str | None = None
    kind: Literal["repo", "reference"] = "repo"
    read_only: bool = False


class ProjectRepoUpdateRequest(BaseModel):
    read_only: bool


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


def _attach_and_register(
    sm: StateManager, project_id: int, path: str, *, label: str | None = None, kind: str = "repo", read_only: bool = False,
) -> int:
    forbidden = [get_config("worktrees_base_dir") or "/data/repos"]
    try:
        if kind == "reference":
            folder = attach_reference_folder(path, forbidden_dirs=forbidden)
        else:
            folder = attach_local_repository(path, forbidden_dirs=forbidden)
    except RepoAttachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if any(os.path.realpath(repo["path"]) == folder for repo in sm.list_project_repos(project_id)):
        raise HTTPException(status_code=409, detail=f"{folder} is already attached to this project")
    return sm.add_project_repo(
        project_id, folder, label=label or os.path.basename(folder), kind=kind, read_only=read_only,
    )


def _add_repo(
    sm: StateManager, project_id: int, *, url: str | None, path: str | None, label: str | None = None,
    kind: str = "repo", read_only: bool = False,
) -> int:
    if bool((url or "").strip()) == bool((path or "").strip()):
        raise HTTPException(status_code=400, detail="Give either a repository URL to clone or a folder path to attach")
    if (path or "").strip():
        return _attach_and_register(sm, project_id, path or "", label=label, kind=kind, read_only=read_only)
    if kind == "reference":
        raise HTTPException(status_code=400, detail="A reference folder is attached by path, not cloned")
    return _clone_and_register(sm, project_id, url or "", label=label, read_only=read_only)


def _clone_and_register(sm: StateManager, project_id: int, url: str, *, label: str | None = None, read_only: bool = False) -> int:
    try:
        path = clone_repository(url, dest_base_dir=_clone_dest_base_dir(project_id))
    except RepoCloneError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return sm.add_project_repo(project_id, path, label=label or os.path.basename(path), clone_url=url, read_only=read_only)


@router.get("")
def list_projects() -> list[dict]:
    return StateManager().list_projects()


@router.post("")
def create_project(body: ProjectCreateRequest) -> dict:
    sm = StateManager()
    paths = [p for p in [body.repo_path, *(body.repo_paths or [])] if (p or "").strip()]
    if (body.repo_url or "").strip() and paths:
        raise HTTPException(status_code=400, detail="Give either a repository URL to clone or a folder path to attach")
    # Every folder is checked before the project exists, so a mistyped path
    # does not leave an empty project behind (a clone can only fail by trying).
    forbidden = [get_config("worktrees_base_dir") or "/data/repos"]
    folders: list[str] = []
    try:
        for path in paths:
            folder = attach_local_repository(path or "", forbidden_dirs=forbidden)
            if folder not in folders:
                folders.append(folder)
        read_only = {attach_local_repository(p, forbidden_dirs=forbidden) for p in body.read_only_paths or [] if p.strip()}
        reference = attach_reference_folder(body.reference_path, forbidden_dirs=forbidden) if (body.reference_path or "").strip() else None
    except RepoAttachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project_id = sm.create_project(body.name, description=body.description)
    if (body.repo_url or "").strip():
        _add_repo(sm, project_id, url=body.repo_url, path=None)
    for folder in folders:
        _add_repo(sm, project_id, url=None, path=folder, read_only=folder in read_only)
    if reference:
        _add_repo(sm, project_id, url=None, path=reference, kind="reference")
    return sm.get_project(project_id)


@router.post("/browse-folder")
def browse_folder_route(body: BrowseFolderRequest) -> dict:
    """The subfolders of a folder on the machine elstin runs on, for the UI's
    folder picker (a browser cannot give a page a real filesystem path).
    Read-only, names only; see elstin/services/repo_clone.py's browse_folder."""
    try:
        return browse_folder(body.path)
    except RepoAttachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scan-folder")
def scan_folder_route(body: ScanFolderRequest) -> dict:
    """What attaching a folder would mean, before anything is attached: a
    git repo (attach it as is), or a folder holding repos — listed, so the
    UI can offer which to attach one by one. Read-only; see
    elstin/services/repo_clone.py's scan_folder."""
    try:
        result = scan_folder(body.path, forbidden_dirs=[get_config("worktrees_base_dir") or "/data/repos"])
    except RepoAttachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sm = StateManager()
    attached = {
        os.path.realpath(repo["path"]) for repo in (sm.list_project_repos(body.project_id) if body.project_id else [])
    }
    result["attached"] = result["path"] in attached
    for repo in result["repos"]:
        repo["attached"] = repo["path"] in attached
    return result


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


@router.delete("/{project_id}")
async def delete_project(project_id: int) -> dict:
    """Delete the project and everything it owns in elstin: its tickets (as
    Remove ticket does each), the clones elstin made for it, its settings,
    ticket filter and folders. An attached folder stays on disk, and nothing
    is sent to GitHub or JIRA — see elstin/daemon/project_removal.py. Refused
    (409) while any of its stories has a run in progress."""
    try:
        return await remove_project(StateManager(), project_id)
    except ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProjectRemovalError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
    repo_id = _add_repo(
        sm, project_id, url=body.url, path=body.path, label=body.label, kind=body.kind, read_only=body.read_only,
    )
    return sm.get_project_repo(repo_id)


@router.put("/{project_id}/repos/{repo_id}")
def update_project_repo(project_id: int, repo_id: int, body: ProjectRepoUpdateRequest) -> dict:
    """Mark a git repo read-only, or writable again. A read-only repo is
    read by every run but no plan may change it (elstin/agents/roles/plan_gate.py).
    409 while a story has unfinished work in it; an epic defaulting to it
    moves to the next writable repo. A reference folder is always read-only."""
    sm = StateManager()
    repo = sm.get_project_repo(repo_id)
    if repo is None or repo["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"No such folder: {repo_id}")
    if repo["kind"] == "reference":
        raise HTTPException(status_code=400, detail="A reference folder is always read-only")
    result = sm.set_project_repo_read_only(repo_id, body.read_only)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No such folder: {repo_id}")
    if not result["changed"]:
        blocked = result["blocked_by"]
        raise HTTPException(
            status_code=409,
            detail=(
                f"{', '.join(blocked)} {'has' if len(blocked) == 1 else 'have'} unfinished work in this repo. "
                "Let it merge, or remove that ticket, before making the repo read-only."
            ),
        )
    return {**sm.get_project_repo(repo_id), "epics_moved": result["epics_moved"], "moved_to": result["moved_to"]}


@router.delete("/{project_id}/repos/{repo_id}")
def remove_project_repo(project_id: int, repo_id: int) -> dict:
    """Detach a folder. Epics that defaulted to it move to the project's next
    folder (a default, not a binding — elstin/tools/workspace.py); 409 only
    while a story has unfinished work in it. The folder on disk is left as
    it is. See elstin/state/manager_project.py's remove_project_repo."""
    sm = StateManager()
    repo = sm.get_project_repo(repo_id)
    if repo is None or repo["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"No such folder: {repo_id}")
    result = sm.remove_project_repo(repo_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No such folder: {repo_id}")
    if result["removed"] and repo.get("kind") == "reference":
        remove_reference_copy(get_config("worktrees_base_dir") or "/data/repos", repo_id)
    if not result["removed"]:
        blocked = result["blocked_by"]
        raise HTTPException(
            status_code=409,
            detail=(
                f"{', '.join(blocked)} {'has' if len(blocked) == 1 else 'have'} unfinished work in this folder. "
                "Let it merge, or remove that ticket, before removing the folder."
            ),
        )
    return {"repo_id": repo_id, **result}


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
    the one elstin generated, else none (see elstin/services/project_guide.py)."""
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
    """Writes (or rewrites) elstin's own guide for a repo that ships none.
    Runs whether or not the project's `generate_project_guide` setting is on:
    that setting only decides whether a run does it automatically."""
    sm = StateManager()
    repo = _repo_of(sm, project_id, repo_id)
    if repo.get("kind") == "reference":
        raise HTTPException(status_code=400, detail="elstin writes guides for repos only — a reference folder is read as it is")
    own = read_repo_guide(repo["path"])
    if own:
        raise HTTPException(
            status_code=409, detail=f"This repo already has its own {own[0]}; elstin only writes a guide for one that has none"
        )
    try:
        guide = await generate_project_guide(repo["path"], project_id)
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


class LessonUpdateRequest(BaseModel):
    # Either or both. `text` is a person's edit; `status` is their verdict.
    text: str | None = None
    status: Literal["proposed", "accepted", "rejected"] | None = None


def _lesson_of(sm: StateManager, project_id: int, lesson_id: int) -> dict:
    lesson = sm.get_project_lesson(lesson_id)
    if lesson is None or lesson["project_id"] != project_id:
        raise HTTPException(status_code=404, detail=f"No such lesson: {lesson_id}")
    return lesson


@router.get("/{project_id}/lessons")
def list_lessons(project_id: int, status: Literal["proposed", "accepted", "rejected"] | None = None) -> list[dict]:
    """The lessons elstin proposed from its own runs (elstin/services/lessons.py),
    oldest first; `status` narrows. Only `accepted` ones reach an agent."""
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return sm.list_project_lessons(project_id, status=status)


@router.put("/{project_id}/lessons/{lesson_id}")
def update_lesson(project_id: int, lesson_id: int, body: LessonUpdateRequest) -> dict:
    """Accept, reject or reword a lesson. A lesson is read as authoritative
    once accepted, so it can be edited first — and the edit is checked the
    way a proposed one is (size), not trusted because a person typed it."""
    sm = StateManager()
    _lesson_of(sm, project_id, lesson_id)
    if body.text is None and body.status is None:
        raise HTTPException(status_code=422, detail="Send `text`, `status`, or both")
    if body.text is not None:
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="A lesson cannot be empty")
        if len(text) > MAX_LESSON_CHARS:
            raise HTTPException(status_code=422, detail=f"A lesson is capped at {MAX_LESSON_CHARS} characters; this is {len(text)}")
        sm.update_lesson_text(lesson_id, text)
    if body.status is not None:
        sm.set_lesson_status(lesson_id, body.status)
    return _lesson_of(sm, project_id, lesson_id)


@router.delete("/{project_id}/lessons/{lesson_id}")
def delete_lesson(project_id: int, lesson_id: int) -> dict:
    sm = StateManager()
    _lesson_of(sm, project_id, lesson_id)
    sm.delete_lesson(lesson_id)
    return {"lesson_id": lesson_id, "deleted": True}


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
# One saved JIRA query per project. See elstin/services/jira_filter.py for
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
