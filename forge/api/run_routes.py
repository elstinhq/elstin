"""Per-story endpoints: the human-gate approval, the story-kind override,
and a per-ticket JIRA prose refresh.

Approval: the reconciler (forge/orchestration/reconciler.py) parks a story
with `ParkForGate` until its key shows up in
`OrchestratorContext.approved_story_keys` — this is how a human clears
that, whichever gate currently applies (plan-approval, PR-open, or
PR-review; a story only ever waits on one at a time). The next daemon poll
cycle picks it up — no separate "resume" action needed.

Kind override: forge guesses at sync time whether a ticket is
implementation or validation work (forge/services/story_kind.py), and that
guess reads free-text prose, so it can be wrong. This is how a human fixes
it. Changing a kind also re-levels the epic, because the two are not
independent — a QA story must run after all implementation work, so
reclassifying without moving the story's wave would leave it scheduled to
validate code that doesn't exist yet.

Activity: the story detail dialog shows what the agents are doing right
now. Two endpoints serve it — GET /{key}/activity is a plain snapshot, and
GET /{key}/stream is the live one the UI actually uses: an SSE connection
that replays the run's history, then pushes each new event the moment it
is written (forge/services/activity_stream.py) instead of leaving the UI a
poll interval behind the agent.

Repo override: an epic is assigned one repo as its default, but a
project whose repos are split by concern can carry an API story and a UI
story under one epic. This is how a human points one story at a different
repo — the one that gets its worktree, branch, commit and pull request.
See forge/tools/workspace.py.

Ticket refresh: epic re-sync deliberately leaves existing summary/
description alone (progress must not be rewritten by a tree walk). This
endpoint is the explicit escape hatch — pull latest JIRA prose for one
story without touching kind, status, or wave.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from jira import JIRAError
from pydantic import BaseModel

from forge.config.store import resolve_bool_config
from forge.daemon.main import check_story_action, execute_story_action
from forge.orchestration.reconciler import Blocked
from forge.services.activity_stream import iter_events
from forge.services.epic_sync import relevel_epic, sync_story_from_jira
from forge.services.run_activity import list_activity
from forge.services.usage import get_story_usage
from forge.state.manager import StateManager

router = APIRouter(prefix="/api/stories", tags=["stories"])

_VALID_KINDS = ("dev", "qa")


class StoryRepoRequest(BaseModel):
    # Null clears the override, so the story goes back to the epic's repo.
    project_repo_id: int | None = None


class StoryKindRequest(BaseModel):
    kind: str


class ExternalChangeRequest(BaseModel):
    # accept: take the new text, keep the plan. replan: take it and start
    # over. dismiss: keep the current text and stop flagging this version.
    action: Literal["accept", "replan", "dismiss"]


def _story_with_pr(sm: StateManager, story_key: str) -> dict:
    story = sm.get_story(story_key) or {}
    story["pull_request"] = sm.get_pull_request(story_key)
    return story


def _maybe_trigger_manual_run(sm: StateManager, story_key: str, background_tasks: BackgroundTasks) -> None:
    """For a project with auto_run_enabled off, poll_once's automatic scan
    never visits this epic again (see forge/daemon/main.py) — an approve or
    retry has to drive the next step itself, or a cleared gate would just
    sit forever with nothing left to pick it up. For an auto_run project
    this is a no-op: the existing poll loop already covers it within 15s,
    so nothing here changes that project's behavior."""
    story = sm.get_story(story_key)
    if story is None:
        return
    epic = sm.get_epic(story["epic_key"])
    if resolve_bool_config("auto_run_enabled", epic["project_id"]):
        return
    result = check_story_action(sm, epic, story_key)
    if not isinstance(result, Blocked):
        background_tasks.add_task(execute_story_action, sm, epic, result)


@router.post("/{story_key}/start")
def start_story(story_key: str, background_tasks: BackgroundTasks) -> dict:
    """The manual-mode Start button: dispatches (or resumes/opens-PR/merges,
    whichever the reconciler says is next for this story) right now, rather
    than waiting for poll_once's automatic scan — which never runs for a
    project with auto_run_enabled off (forge/config/store.py). Works the
    same way for an auto_run project too: a plain accelerant, since the
    poll loop would reach the same story within 15s anyway."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    epic = sm.get_epic(story["epic_key"])
    result = check_story_action(sm, epic, story_key)
    if isinstance(result, Blocked):
        raise HTTPException(status_code=409, detail=f"{story_key} is not ready to start: {result.reason}")
    background_tasks.add_task(execute_story_action, sm, epic, result)
    return {"story_key": story_key, "triggered": type(result).__name__}


@router.post("/{story_key}/approve")
def approve_story(story_key: str, background_tasks: BackgroundTasks) -> dict:
    sm = StateManager()
    if sm.get_story(story_key) is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    sm.set_story_gate_approved(story_key, True)
    _maybe_trigger_manual_run(sm, story_key, background_tasks)
    return {"story_key": story_key, "gate_approved": True}


@router.post("/{story_key}/retry")
def retry_story(story_key: str, background_tasks: BackgroundTasks) -> dict:
    """Recovers a story the reconciler halted the whole epic on (any
    "failed" story blocks every level — see forge/orchestration/
    reconciler.py). sm.retry_story picks the recovery path from
    story["failure_stage"]: a full redispatch for a run failure, or just a
    re-attempted merge for an integration-merge conflict — see
    forge/state/manager_story.py. For an auto_run project the next daemon
    poll cycle (up to 15s) picks it up; for a manual-mode project
    _maybe_trigger_manual_run drives it immediately since nothing else will."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if story["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is not failed (status={story['status']}) — nothing to retry",
        )
    sm.retry_story(story_key)
    _maybe_trigger_manual_run(sm, story_key, background_tasks)
    return _story_with_pr(sm, story_key)


@router.post("/{story_key}/external-change")
def resolve_external_change(story_key: str, body: ExternalChangeRequest, background_tasks: BackgroundTasks) -> dict:
    """A human's answer to a ticket that changed in its source after forge
    planned or started the story — see forge/services/external_sync.py for
    why it was held rather than applied."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if story["external_pending"] is None:
        raise HTTPException(status_code=409, detail=f"{story_key} has no changed-in-source text waiting")
    try:
        sm.resolve_external_change(story_key, body.action)
    except ValueError as exc:  # a re-plan was asked for while work is in flight
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if body.action == "replan":
        # A re-planned story goes back to pending; for a manual-mode project
        # nothing else will start it (same as retry).
        _maybe_trigger_manual_run(sm, story_key, background_tasks)
    return _story_with_pr(sm, story_key)


@router.put("/{story_key}/kind")
def set_story_kind(story_key: str, body: StoryKindRequest) -> dict:
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if body.kind not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {_VALID_KINDS}")
    if story["parent_key"]:
        # A sub-task is never dispatched on its own, so it has no route to
        # choose between — its kind is inherited from its parent and only
        # the parent's is meaningful.
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is a sub-task of {story['parent_key']} — set the kind on the parent story instead",
        )
    if story["status"] != "pending":
        # Reclassifying in-flight work would move a story's wave underneath
        # a run that is already using it. Refuse rather than silently
        # producing an epic whose levels disagree with what has run.
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is already {story['status']} — only a pending story can be reclassified",
        )

    sm.set_story_kind(story_key, body.kind)
    levels = relevel_epic(story["epic_key"])
    return {"story_key": story_key, "kind": body.kind, "epic_key": story["epic_key"], "levels": levels}


@router.put("/{story_key}/repo")
def set_story_repo(story_key: str, body: StoryRepoRequest) -> dict:
    """Point one story at a specific repo on its project, or clear the
    override with a null so it inherits the epic's."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if story["parent_key"]:
        # A sub-task is never dispatched, so it has no worktree of its own
        # to place — it rides along with its parent's.
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is a sub-task of {story['parent_key']} — set the repo on the parent story instead",
        )
    if story["status"] != "pending":
        # The worktree, branch and any open PR already live in the old
        # repo. Moving the target underneath them would orphan all three.
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is already {story['status']} — only a pending story can change repo",
        )

    epic = sm.get_epic(story["epic_key"])
    if body.project_repo_id is not None:
        project_repo = sm.get_project_repo(body.project_repo_id)
        if project_repo is None or project_repo["project_id"] != epic["project_id"]:
            raise HTTPException(
                status_code=400,
                detail="project_repo_id must be a repo belonging to this story's project",
            )

    sm.set_story_project_repo(story_key, body.project_repo_id)
    return {"story_key": story_key, **{k: sm.get_story(story_key)[k] for k in ("project_repo_id", "repo")}}


@router.post("/{story_key}/sync")
def sync_story(story_key: str) -> dict:
    """Refresh summary/description from JIRA for one already-synced story."""
    try:
        sync_story_from_jira(story_key)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail.startswith("No such ") else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    except JIRAError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404, detail=f"JIRA issue {story_key} not found or not accessible"
            ) from exc
        raise HTTPException(status_code=502, detail=(exc.text or str(exc))) from exc
    return _story_with_pr(StateManager(), story_key)


@router.get("/{story_key}")
def get_story(story_key: str) -> dict:
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    return story


@router.get("/{story_key}/activity")
def get_story_activity(story_key: str, limit: int = 300) -> list[dict]:
    """The daemon's persisted agent events for this story's run — polled by
    the UI's story detail dialog. run_id == thread_id == story_key by
    construction (forge/daemon/main.py), so this is a thin read of the
    activity_events table (forge/services/run_activity.py). `limit` (most
    recent N, chronological order) keeps a long-running story's poll bounded
    — the underlying table has no cap, unlike the ring buffer this replaced."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    run_id = story.get("thread_id") or story_key
    return list_activity(run_id, limit=limit)


# How long a browser waits before reconnecting a dropped stream. Sent once
# up front so the reconnect is quick without being a hot loop — EventSource
# defaults to a few seconds anyway, but leaving it implicit means the
# interval varies by browser.
_RETRY_MS = 3000


def _sse_frame(event: dict) -> str:
    """One event in SSE wire format.

    The `id:` line is what makes reconnects seamless: the browser echoes
    the last one it saw back as a Last-Event-ID header, and the stream
    resumes from there rather than replaying the whole run (or, worse,
    silently skipping whatever landed while the socket was down).
    """
    return f"id: {event['id']}\ndata: {json.dumps(event, default=str)}\n\n"


def _parse_last_event_id(raw: str | None) -> int:
    """Client-supplied, so treat it as untrusted: anything unparseable means
    "start from the beginning", never a 500."""
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


@router.get("/{story_key}/stream")
async def stream_story_activity(story_key: str, request: Request, since_id: int = 0) -> StreamingResponse:
    """Live activity for this story's run, as Server-Sent Events.

    Replays what has already happened (from `since_id`, or the
    Last-Event-ID header on a reconnect), then stays open and pushes each
    new event as the daemon writes it. A comment-only heartbeat goes out
    during quiet stretches — a run can spend minutes inside one agent turn,
    and without traffic an idle connection is indistinguishable from a dead
    one to anything in between.

    The generator ends when the client disconnects: the heartbeat write
    fails, which closes the async generator and unregisters the subscriber
    (forge/services/activity_stream.py). Nothing here has to poll
    request.is_disconnected() to notice.
    """
    story = await asyncio.to_thread(StateManager().get_story, story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    run_id = story.get("thread_id") or story_key
    cursor = max(since_id, _parse_last_event_id(request.headers.get("last-event-id")))

    async def publisher() -> AsyncIterator[str]:
        yield f"retry: {_RETRY_MS}\n\n"
        async for event in iter_events(run_id, since_id=cursor):
            # A bare comment line: valid SSE, ignored by EventSource, and
            # enough of a write to detect a client that has gone away.
            yield ": keep-alive\n\n" if event is None else _sse_frame(event)

    return StreamingResponse(
        publisher(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Nginx and friends buffer proxied responses by default, which
            # would hold events back until the buffer fills — the exact
            # latency this endpoint exists to remove.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{story_key}/usage")
def get_story_usage_route(story_key: str) -> dict:
    """Per-phase (planner/dev/verifier/qa) token/cache/cost/time breakdown
    for this story's run — the story detail dialog's Usage panel."""
    sm = StateManager()
    if sm.get_story(story_key) is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    return get_story_usage(story_key)
