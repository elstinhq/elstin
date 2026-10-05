"""Per-story endpoints: the human-gate approval, the story-kind override,
and a per-ticket JIRA prose refresh.

Approval: the reconciler (forge/orchestration/reconciler.py) parks a story
with `ParkForGate` until its key shows up in
`OrchestratorContext.approved_story_keys` — this is how a human clears
plan approval, the only per-story gate: a finished story merges into the
epic branch locally, and the one pull request is the epic's (its own gate
is POST /api/epics/{key}/pr-approval). The next daemon poll cycle picks it
up — no separate "resume" action needed.

Clarification: a plan-approval gate sometimes carries a question instead of
a finished plan — the planner asks when a design decision is genuinely
ambiguous (forge/agents/roles/planner.py). POST /{key}/answer is how a
human resolves it: every pending question must be answered in one call,
which clears the same plan-approval gate /approve does and feeds the
resolved answers back into the next resume instead of asking again.

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

Repo override: an epic is assigned one repo as its default, and a story
inherits it. That default is only where an unprefixed plan path lands and
where the story's first worktree is made — the approved plan decides which
repos a story writes (forge/tools/workspace.py). This is how a human points
one pending story's default at a different repo.

Ticket refresh: epic re-sync deliberately leaves existing summary/
description alone (progress must not be rewritten by a tree walk). This
endpoint is the explicit escape hatch — pull latest JIRA prose for one
story without touching kind, status, or wave.

Prior work: before a story's branch is made, the daemon looks for branches
that already carry its key (forge/services/prior_work.py). Where several
carry diverged work and none contains the others, forge does not guess which
to build on: the story waits. POST /{key}/prior-work is the answer — a
branch the lookup offered for that repo, or `null` to start fresh.

Re-plan: POST /{key}/replan throws away a plan waiting for approval and
plans the story again from scratch — the way to say "not this plan" short
of approving it. The person's answers to earlier questions are kept: they
are decisions, and the fresh run is given them (forge/daemon/main.py).

Retry: POST /{key}/retry recovers a failed story. A run that failed in dev
after planning finished picks up at dev with its plan (forge/daemon/main.py's
arm_resume_after_failure) rather than paying for the planner again, and one
whose review the provider failed to complete (a limit, an outage) picks up
ahead of the gate with dev's work; any other failure starts over from
planning. `?replan=true` forces the fresh plan even then — the way to say the
plan itself is what went wrong. A story the daemon parked on a provider limit
is retried the same way by the daemon itself once the limit lifts.

Guidance: POST /{key}/guidance sends a message to the agent while it works. It
waits in the story's queue and the agent reads it at the start of its next turn
(forge/services/guidance.py); advice, not authority — dev still writes only the
plan's files. 409 when no agent run is in progress to read it.

Plan: the planner's structured plan (feature_summary, plan_markdown,
files_to_create/modify, acceptance_criteria, ...) lives only inside the
story's LangGraph checkpoint (forge/db/models/gameplan.py's docstring on
why forge doesn't duplicate it into Postgres) — once a human clears the
plan-approval gate there was previously no way to read it back short of
scrolling the activity feed. GET /{key}/plan reopens that checkpoint by
thread_id and returns the `plan` key from its state, same mechanism
forge/daemon/main.py's dispatch_story resume path already uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse
from jira import JIRAError
from pydantic import BaseModel, Field

from forge.config.store import resolve_bool_config
from forge.daemon.main import (
    RESUME_NOTES, arm_resume_after_failure, cancel_run, check_story_action, execute_story_action, live_run_keys,
)
from forge.db.engine import get_database_url
from forge.orchestration.graph import checkpointer_from_url, compile_graph
from forge.orchestration.reconciler import Blocked
from forge.services.activity_stream import iter_events
from forge.services.epic_sync import relevel_epic, sync_story_from_jira
from forge.services.guidance import MAX_GUIDANCE_CHARS
from forge.services.run_activity import append_activity, list_activity
from forge.services.usage import get_run_outcome, get_story_usage
from forge.state.manager import StateManager
from forge.state.manager_story import GuidanceQueueFull

logger = logging.getLogger(__name__)

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


class ClarificationAnswer(BaseModel):
    question_id: str
    option_id: str


class PriorBranchPickRequest(BaseModel):
    repo_label: str
    # A branch the prior-work lookup offered for that repo, or None to start
    # fresh from the epic branch.
    branch: str | None = None


class GuidanceRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_GUIDANCE_CHARS)


class ClarificationAnswerRequest(BaseModel):
    answers: list[ClarificationAnswer]


def _story_with_pr(sm: StateManager, story_key: str) -> dict:
    story = sm.get_story(story_key) or {}
    story["pull_request"] = sm.get_pull_request(story_key)
    # thread_id == run_id == story_key by construction (forge/orchestration/
    # graph.py). None until the story's first dispatch. The counters
    # accumulate across every retry under this run_id (forge/services/
    # usage.py's record_run_outcome), so on a story that has retried, this
    # is the run's lifetime total, not just the latest attempt — the
    # distinction the UI actually needs (dev wrote real code vs. the run
    # never got past planning) holds either way: a "run"-stage failure
    # accumulates from the attempt that produced it — a full redispatch
    # replans, and a resume at dev (arm_resume_at_dev) keeps the planner
    # counts it already has and adds the new dev passes.
    story["run_outcome"] = get_run_outcome(story_key) if story.get("thread_id") else None
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


@router.post("/{story_key}/prior-work")
def pick_prior_branch(story_key: str, body: PriorBranchPickRequest) -> dict:
    """Which of the diverged branches found for this ticket its work
    continues from, in one repo — or none (`branch: null`). Only while the
    story has not started: once its branch exists, what it started from is
    history. Nothing is dispatched from here; the story becomes ready like
    any other (the next poll, or Start in a manual-run project)."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if story["status"] not in ("pending", "skipped") or story.get("branch"):
        raise HTTPException(status_code=409, detail=f"{story_key} has already started; its branch exists")
    finding = ((story.get("prior_work") or {}).get("repos") or {}).get(body.repo_label)
    if not finding:
        raise HTTPException(status_code=404, detail=f"No prior work was found for {story_key} in {body.repo_label}")
    if not sm.pick_story_prior_branch(story_key, body.repo_label, body.branch):
        raise HTTPException(status_code=422, detail=f"{body.branch!r} is not a branch found for {story_key} in {body.repo_label}")
    return sm.get_story(story_key)


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


@router.post("/{story_key}/replan")
def replan_story(story_key: str, background_tasks: BackgroundTasks) -> dict:
    """Plan a story waiting for approval again, keeping its answers — see
    this module's docstring."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if story_key in live_run_keys() or not sm.send_back_to_planning(story_key, keep_answers=True):
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is {story['status']} — only a story waiting on plan approval can be re-planned",
        )
    append_activity(story_key, type="status", phase="system", title="Re-plan requested: the plan is made again")
    _maybe_trigger_manual_run(sm, story_key, background_tasks)
    return _story_with_pr(sm, story_key)


@router.post("/{story_key}/answer")
def answer_story_questions(story_key: str, body: ClarificationAnswerRequest, background_tasks: BackgroundTasks) -> dict:
    """A human's answer to the planner's clarification questions (see
    forge/agents/roles/planner.py's CLARIFICATION QUESTIONS rules and
    forge/orchestration/graph.py's `awaiting_clarification`).

    The reconciler cannot tell this state apart from an ordinary
    plan-approval gate — both collapse to "awaiting_plan_approval"
    (forge/daemon/main.py's _GRAPH_STATUS_TO_STORY_STATUS) — so
    set_story_clarification_answers clears the same gate /approve does,
    and the next resume feeds the resolved answers back into the planner
    (forge/daemon/main.py's resume patch) instead of asking again.

    Every pending question must be answered in one call: the planner
    prompt only has an all-or-nothing "clarification answers provided"
    branch (forge/agents/roles/planner.py), so a partial set would either
    be silently ignored or produce a plan reasoning from an answer that was
    never given."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    pending = story.get("clarification_questions") or []
    if not pending:
        raise HTTPException(status_code=409, detail=f"{story_key} has no pending clarification questions")

    by_id = {q["id"]: q for q in pending}
    submitted = {a.question_id: a.option_id for a in body.answers}
    missing = [qid for qid in by_id if qid not in submitted]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing answer(s) for question(s): {', '.join(missing)}")

    resolved: list[dict] = []
    for question_id, option_id in submitted.items():
        question = by_id.get(question_id)
        if question is None:
            raise HTTPException(status_code=422, detail=f"no such question: {question_id}")
        option = next((o for o in question.get("options") or [] if o.get("id") == option_id), None)
        if option is None:
            raise HTTPException(status_code=422, detail=f"{question_id}: no such option {option_id!r}")
        resolved.append({
            "question": question.get("question"),
            "option_label": option.get("label"),
            "option_description": option.get("description"),
        })

    sm.set_story_clarification_answers(story_key, resolved)
    _maybe_trigger_manual_run(sm, story_key, background_tasks)
    return _story_with_pr(sm, story_key)


@router.post("/{story_key}/cancel")
async def cancel_story_run(story_key: str) -> dict:
    """Stop a story's run in flight. The story is left failed ("cancelled"), so
    Retry starts it over and Remove clears it. `async` on purpose: cancelling an
    asyncio task has to happen on the event loop's own thread."""
    sm = StateManager()
    if sm.get_story(story_key) is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if not await cancel_run(story_key):
        raise HTTPException(status_code=409, detail=f"{story_key} has no run in progress")
    return _story_with_pr(sm, story_key)


@router.post("/{story_key}/guidance")
def send_guidance(story_key: str, body: GuidanceRequest) -> dict:
    """Send a message to the agent working on this story. It is read at the start
    of the agent's next turn, so it lands between two steps, not mid-thought. 409
    when there is no agent run in progress in this process to read it — a story
    waiting for approval, merging, or finished has nothing to steer, and a QA run
    has no agent loop (it is a single review call)."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="The message is empty")
    if story.get("kind") == "qa":
        raise HTTPException(status_code=409, detail=f"{story_key} is a QA story: its review has no agent to message")
    if story["status"] != "dispatched" or story_key not in live_run_keys():
        raise HTTPException(status_code=409, detail=f"{story_key} has no agent run in progress to message")
    try:
        pending = sm.add_story_guidance(story_key, text)
    except GuidanceQueueFull:
        raise HTTPException(
            status_code=409,
            detail="The agent has not read the messages already waiting for it — wait for it to catch up",
        ) from None
    append_activity(story_key, type="status", phase="system", title="Message sent to the agent", detail=text)
    return {"story_key": story_key, "pending": pending}


@router.post("/{story_key}/retry")
async def retry_story(story_key: str, background_tasks: BackgroundTasks, replan: bool = False) -> dict:
    """Recovers a story the reconciler halted the whole epic on (any
    "failed" story blocks every level — see forge/orchestration/
    reconciler.py). sm.retry_story picks the recovery path from
    story["failure_stage"]: a full redispatch for a run failure, or just a
    re-attempted merge for an integration-merge conflict — see
    forge/state/manager_story.py. For an auto_run project the next daemon
    poll cycle (up to 15s) picks it up; for a manual-mode project
    _maybe_trigger_manual_run drives it immediately since nothing else will.

    A run failure whose planning is still good — it ended failed in dev — is
    re-armed at dev first (arm_resume_after_failure) and resumed instead of
    redispatched, so the plan is not paid for twice. `replan` skips that.
    `async` because reading the checkpoint is: the blocking calls go through
    `to_thread`, as everywhere else in this module."""
    sm = StateManager()
    story = await asyncio.to_thread(sm.get_story, story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    if story["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"{story_key} is not failed (status={story['status']}) — nothing to retry",
        )
    stage = None
    if not replan and story.get("failure_stage") in (None, "run"):
        epic = await asyncio.to_thread(sm.get_epic, story["epic_key"])
        try:
            stage = await arm_resume_after_failure(sm, epic, story)
        except Exception:  # noqa: BLE001 — a saving, never a reason to leave the story failed
            logger.exception("story %s: could not resume where it stopped — retrying with a fresh plan", story_key)
    await asyncio.to_thread(sm.retry_story, story_key, resume_at_dev=stage is not None)
    if stage:
        title, detail = RESUME_NOTES[stage]
        await asyncio.to_thread(
            append_activity, story_key, type="status", phase="system", title=f"Retry: {title}", detail=detail,
        )
    await asyncio.to_thread(_maybe_trigger_manual_run, sm, story_key, background_tasks)
    return await asyncio.to_thread(_story_with_pr, sm, story_key)


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
    if sm.get_story(story_key) is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    return _story_with_pr(sm, story_key)


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


@router.get("/{story_key}/plan")
async def get_story_plan(story_key: str) -> dict:
    """The planner's plan for this story, read back from its LangGraph
    checkpoint by thread_id (see this module's docstring). `plan` is null
    before a run has been dispatched at all, and also for a "qa" story,
    which never runs a planner node."""
    sm = StateManager()
    story = sm.get_story(story_key)
    if story is None:
        raise HTTPException(status_code=404, detail=f"No such story: {story_key}")
    thread_id = story.get("thread_id")
    if not thread_id:
        return {"story_key": story_key, "plan": None}

    config = {"configurable": {"thread_id": thread_id}}
    # require_plan_approval only affects where the graph would interrupt on
    # an actual invoke — irrelevant to a plain state read.
    async with checkpointer_from_url(get_database_url()) as checkpointer:
        graph = compile_graph(checkpointer, require_plan_approval=False)
        snapshot = await graph.aget_state(config)

    # initial_state() seeds "plan" as {} before the planner node has ever
    # run — falsy either way, so a plain truthiness check covers both "no
    # run yet" and "run started but hasn't reached the planner" the same
    # way as the no-thread_id case above.
    plan = (snapshot.values or {}).get("plan") or None
    return {"story_key": story_key, "plan": plan}
