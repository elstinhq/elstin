"""PO sessions over HTTP: a person and the PO agent drafting a backlog.

The API never does the slow part. Starting a session, or sending it a message,
only records it and queues a turn; elstin/daemon/po_runner.py runs the agent on
the next poll and the draft appears when the turn ends. Everything that has to
be live reaches the page through the session's activity stream
(`GET /api/po-sessions/{id}/stream`, the same SSE code a story's feed uses,
keyed by run id `po-<id>`), and the rest by polling the session.

Routes (the project's own are under `/api/projects/{id}`):

- `POST /projects/{id}/po-sessions` — start one from a brief, optionally on an
  existing epic. The *server* picks the mode (elstin/services/po_sessions.py's
  `select_mode`) from what the epic already has.
- `GET /projects/{id}/po-mode` — the mode a given epic would get, for the start
  dialog's note, without starting anything.
- `GET /projects/{id}/po-sessions` — the project's sessions.
- `GET /po-sessions/{id}` — the session: draft, problems, questions, messages.
- `POST /po-sessions/{id}/messages` — a message to the agent. Delivered live to a
  turn that is running (it reads it before its next step); otherwise it queues a
  revision. `POST /po-sessions/{id}/answer` answers the agent's open questions
  and queues a revision that uses them.
- `POST /po-sessions/{id}/publish` — create the draft in JIRA. 409 with the reason
  unless the session is `ready` and the project allows creating tickets
  (`po_jira_create_enabled`); runs in the background. `POST .../suggestions/post`
  posts one existing ticket's suggested edit as a comment (`po_jira_comment_enabled`).
  `POST .../questions/dismiss` lets a draft stand on the agent's own guesses.
- `PUT /po-sessions/{id}/draft` — a person's edit. The shape is checked; the
  version it was based on must still be the current one (409 otherwise).
- `GET /po-sessions/{id}/export` — the draft as markdown or JSON, which works
  whether or not elstin may write to JIRA.
- `POST /po-sessions/{id}/cancel`, `DELETE /po-sessions/{id}`, and
  `GET /po-sessions/{id}/stream`.
"""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from elstin.agents.roles.po_gate import carry_verification
from elstin.api.run_routes import sse_response
from elstin.daemon.po_runner import cancel_po_session_task, start_publish
from elstin.services.backlog_publisher import PublishError, post_suggestion
from elstin.services.po_sessions import (
    PoModeError, PoSourceUnavailable, build_context, diff_paths, draft_to_markdown, inspect_epic, publish_counts,
    publish_state, published_edit_conflicts, resolve_jira_project_key, story_publish_states, structure_problems,
    top_level_stories, validate_draft_edit,
)
from elstin.state.manager import StateManager
from elstin.state.manager_po import (
    PO_AWAITING_INPUT, PO_DISCARDED, PO_FAILED, PO_PUBLISHED, PO_PUBLISHING, PO_READY, PO_RUNNING, PoSessionConflict, po_run_id,
)

router = APIRouter(prefix="/api", tags=["po"])

MAX_BRIEF_CHARS = 20_000
# A message to the agent, and how many it may have waiting unread — the same
# bounds a story's guidance has (elstin/services/guidance.py).
MAX_MESSAGE_CHARS = 4_000
MAX_UNREAD_MESSAGES = 10


class StartSessionRequest(BaseModel):
    brief: str
    epic_key: str | None = None
    # Where tickets are created. Defaults from the epic's key, then the
    # project's ticket filter; a brainstorm with neither needs it typed.
    jira_project_key: str | None = None


class MessageRequest(BaseModel):
    message: str


class AnswerInput(BaseModel):
    question_id: str
    # One of the question's options, or the person's own words.
    option_id: str | None = None
    text: str | None = None


class AnswerRequest(BaseModel):
    answers: list[AnswerInput]


class PublishRequest(BaseModel):
    # Where the tickets go, when the session did not already know.
    jira_project_key: str | None = None
    # Publish only these stories (and, in a brainstorm, the epic they go under)
    # rather than everything still not in JIRA.
    refs: list[str] | None = None


class SuggestionRequest(BaseModel):
    key: str


class SaveDraftRequest(BaseModel):
    draft: dict[str, Any]
    # The draft version this edit was made over (409 when it has moved on).
    version: int


def _session_or_404(session_id: int, *, messages: bool = False) -> dict[str, Any]:
    session = StateManager().get_po_session(session_id, messages=messages)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No such PO session: {session_id}")
    return session


def _view(session: dict[str, Any]) -> dict[str, Any]:
    """A session as the page shows it: the stored row, plus what is wrong with
    the draft (blocking `problems`, advisory `warnings`) and what publishing it
    would create."""
    defects = structure_problems(session)
    context = session.get("context") or {}
    return {
        **{key: value for key, value in session.items() if key != "context"},
        "epic_summary": context.get("epic_summary") or "",
        "existing_stories": [{"key": key, "summary": summary} for key, summary in context.get("existing") or []],
        "problems": [d for d in defects if d.get("blocking", True)],
        "warnings": [d for d in defects if not d.get("blocking", True)],
        "counts": publish_counts(session),
        "publish": publish_state(session, defects=defects),
        "story_publish": story_publish_states(session, defects),
    }


def _summary(session: dict[str, Any]) -> dict[str, Any]:
    draft = session.get("draft") or {}
    brief = session["brief"].strip().replace("\n", " ")
    return {
        "id": session["id"], "project_id": session["project_id"], "epic_key": session["epic_key"],
        "mode": session["mode"], "status": session["status"], "error": session["error"],
        "brief": brief[:200] + ("…" if len(brief) > 200 else ""),
        "title": (draft.get("epic") or {}).get("summary") or None,
        "stories": len(draft.get("stories") or []), "cost_usd": session["cost_usd"],
        "created_at": session["created_at"], "updated_at": session["updated_at"],
    }


# -- starting ---------------------------------------------------------------------------------


@router.get("/projects/{project_id}/po-mode")
def preview_po_mode(project_id: int, epic_key: str | None = None) -> dict[str, Any]:
    """The mode a session on `epic_key` would get (or `brainstorm` with none),
    and what it found — read from the ticket source, nothing started."""
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    if not (epic_key or "").strip():
        return {"mode": "brainstorm", "epic_key": None, "epic_summary": None, "stories": 0}
    try:
        key, mode, tree, _source = inspect_epic(project_id, epic_key)
    except PoSourceUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PoModeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    root = next(ticket for ticket in tree if ticket.key == key)
    return {"mode": mode, "epic_key": key, "epic_summary": root.summary, "stories": len(top_level_stories(key, tree))}


@router.post("/projects/{project_id}/po-sessions", status_code=201)
def start_po_session(project_id: int, body: StartSessionRequest) -> dict[str, Any]:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    brief = body.brief.strip()
    if not brief:
        raise HTTPException(status_code=422, detail="Write a brief: what is this backlog for?")
    if len(brief) > MAX_BRIEF_CHARS:
        raise HTTPException(status_code=422, detail=f"The brief is over {MAX_BRIEF_CHARS} characters; shorten it.")

    epic_key: str | None = None
    tree: list = []
    mode = "brainstorm"
    if (body.epic_key or "").strip():
        try:
            epic_key, mode, tree, _source = inspect_epic(project_id, body.epic_key or "")
        except PoSourceUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except PoModeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        on_board = sm.get_epic(epic_key)
        if on_board is not None and on_board.get("project_id") != project_id:
            raise HTTPException(status_code=409, detail=f"{epic_key} belongs to a different project on elstin's board.")

    jira_project_key = resolve_jira_project_key(body.jira_project_key, epic_key, sm.get_project_ticket_filter(project_id))
    session = sm.create_po_session(
        project_id, brief=brief, mode=mode, epic_key=epic_key, jira_project_key=jira_project_key,
        context=build_context(epic_key, tree),
    )
    return _view(session)


# -- reading ------------------------------------------------------------------------------------


@router.get("/projects/{project_id}/po-sessions")
def list_po_sessions(project_id: int, include_discarded: bool = False) -> list[dict[str, Any]]:
    sm = StateManager()
    if sm.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"No such project: {project_id}")
    return [_summary(s) for s in sm.list_po_sessions(project_id, include_discarded=include_discarded)]


@router.get("/po-sessions/{session_id}")
def get_po_session(session_id: int) -> dict[str, Any]:
    return {**_view(_session_or_404(session_id)), "messages": StateManager().list_po_messages(session_id)}


@router.get("/po-sessions/{session_id}/export")
def export_po_session(session_id: int, format: str = "markdown"):
    session = _session_or_404(session_id)
    if session.get("draft") is None:
        raise HTTPException(status_code=409, detail="There is no draft to export yet.")
    if format == "json":
        return {"mode": session["mode"], "epic_key": session["epic_key"], "draft": session["draft"], "questions": session["questions"]}
    if format != "markdown":
        raise HTTPException(status_code=422, detail="format must be markdown or json")
    return PlainTextResponse(draft_to_markdown(session), media_type="text/markdown; charset=utf-8")


@router.get("/po-sessions/{session_id}/stream")
async def stream_po_session(session_id: int, request: Request, since_id: int = 0) -> StreamingResponse:
    """The session's live activity (research, the draft gate's verdicts, the
    agent's reasoning), as the story feed streams a run's."""
    if await asyncio.to_thread(StateManager().get_po_session, session_id) is None:
        raise HTTPException(status_code=404, detail=f"No such PO session: {session_id}")
    return sse_response(po_run_id(session_id), request, since_id)


# -- editing ---------------------------------------------------------------------------------------


@router.put("/po-sessions/{session_id}/draft")
def save_po_draft(session_id: int, body: SaveDraftRequest) -> dict[str, Any]:
    session = _session_or_404(session_id)
    errors = validate_draft_edit(body.draft, session)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    # What a person did not touch keeps its tick: they cannot re-run the checks
    # that need the agent's own reads.
    merged = carry_verification(body.draft, session.get("draft"))
    paths = diff_paths(session.get("draft"), merged)
    if not paths and body.version == session["draft_version"]:
        return _view(session)
    conflicts = published_edit_conflicts(session, paths)
    if conflicts:
        raise HTTPException(status_code=409, detail=conflicts[0])
    try:
        saved = StateManager().save_po_draft(session_id, merged, expected_version=body.version, edited_paths=paths)
    except PoSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(saved)


# -- talking to the agent -----------------------------------------------------------------------------


@router.post("/po-sessions/{session_id}/messages")
def send_po_message(session_id: int, body: MessageRequest) -> dict[str, Any]:
    """A person's message. A turn that is running reads it live, between two of
    its steps; with nothing running it queues a revision of the draft."""
    session = _session_or_404(session_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Write a message first.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=422, detail=f"A message is at most {MAX_MESSAGE_CHARS} characters.")
    if session["status"] in (PO_DISCARDED, PO_PUBLISHING, PO_PUBLISHED):
        raise HTTPException(status_code=409, detail=f"The session is {session['status']}; start a new one to keep going.")
    sm = StateManager()
    unread = [m for m in sm.list_po_messages(session_id) if m["role"] == "person" and not m["delivered"]]
    if len(unread) >= MAX_UNREAD_MESSAGES:
        raise HTTPException(
            status_code=409, detail=f"{len(unread)} messages are already waiting for the agent to read; wait for it to catch up.",
        )
    sm.add_po_message(session_id, text)
    # `queued` already has a turn coming and `running` is reading messages as it goes.
    after = sm.queue_po_turn(session_id, from_statuses=(PO_READY, PO_AWAITING_INPUT, PO_FAILED)) or session
    return {**_view(after), "delivery": "live" if after["status"] == PO_RUNNING else "queued"}


@router.post("/po-sessions/{session_id}/answer")
def answer_po_questions(session_id: int, body: AnswerRequest) -> dict[str, Any]:
    """Answer the agent's open questions — every one, by choosing an option or
    writing an answer — and queue a revision that uses them."""
    session = _session_or_404(session_id)
    questions = {q["id"]: q for q in session["questions"] if isinstance(q, dict)}
    if not questions:
        raise HTTPException(status_code=409, detail="The agent has no open questions.")
    given = {a.question_id: a for a in body.answers}
    unknown = sorted(set(given) - set(questions))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Not questions of this session: {', '.join(unknown)}.")
    missing = sorted(set(questions) - set(given))
    if missing:
        raise HTTPException(status_code=422, detail=f"Answer every question; still open: {', '.join(missing)}.")

    resolved: list[dict[str, Any]] = []
    for question_id, question in questions.items():
        answer = given[question_id]
        written = (answer.text or "").strip()
        if answer.option_id:
            option = next((o for o in question.get("options") or [] if o.get("id") == answer.option_id), None)
            if option is None:
                raise HTTPException(status_code=422, detail=f"{answer.option_id!r} is not an option of question {question_id}.")
            text = option.get("label") or ""
            if option.get("description"):
                text += f" — {option['description']}"
            if written:
                text += f" ({written})"
        elif written:
            text = written
        else:
            raise HTTPException(status_code=422, detail=f"Choose an option or write an answer for question {question_id}.")
        resolved.append({"id": question_id, "question": question.get("question") or "", "answer": text[:MAX_MESSAGE_CHARS]})
    try:
        saved = StateManager().set_po_answers(session_id, resolved)
    except PoSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(saved)


# -- publishing --------------------------------------------------------------------------------------------


@router.post("/po-sessions/{session_id}/questions/dismiss")
def dismiss_po_questions(session_id: int) -> dict[str, Any]:
    """Let the draft stand on the agent's own best guesses: its open questions are
    dropped, so the session is `ready` and can be published."""
    _session_or_404(session_id)
    try:
        return _view(StateManager().dismiss_po_questions(session_id))
    except PoSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/po-sessions/{session_id}/publish")
async def publish_po_session(session_id: int, body: PublishRequest | None = None) -> dict[str, Any]:
    """Create the draft's epic (a brainstorm only), stories, sub-tasks and links in
    JIRA, then sync them onto the board. With `refs`, only those stories (and the
    epic, if it is not made yet): the session is `ready` again afterwards, for the
    next story. It runs in the background: this answers as soon as it has started,
    and the session's `publish_result` fills in as tickets are made. A publish that
    stops is pressed again and continues."""
    sm = StateManager()
    session = await asyncio.to_thread(_session_or_404, session_id)
    typed = ((body.jira_project_key if body else None) or "").strip().upper() or None
    if body and body.refs is not None and not body.refs:
        raise HTTPException(status_code=422, detail="Name at least one story to publish, or leave `refs` out to publish them all.")
    refs = list(dict.fromkeys(body.refs)) if body and body.refs else None
    state = await asyncio.to_thread(partial(publish_state, session, jira_project_key=typed, refs=refs))
    if not state["allowed"]:
        raise HTTPException(status_code=409, detail=state["reason"])
    try:
        saved = await asyncio.to_thread(sm.begin_po_publish, session_id, jira_project_key=typed)
    except PoSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Without `refs` the call is the one it always was.
    await (start_publish(sm, session_id, refs) if refs else start_publish(sm, session_id))
    return await asyncio.to_thread(_view, saved)


@router.post("/po-sessions/{session_id}/suggestions/post")
async def post_po_suggestion(session_id: int, body: SuggestionRequest) -> dict[str, Any]:
    """Post one existing ticket's suggested edit as a comment on it. One at a
    time and on a person's say; the ticket's own text is never changed."""
    sm = StateManager()
    session = await asyncio.to_thread(_session_or_404, session_id)
    if session["status"] in (PO_DISCARDED, PO_PUBLISHING) or session["status"] in ("queued", PO_RUNNING):
        raise HTTPException(status_code=409, detail=f"The session is {session['status']}; wait for it to settle first.")
    if session["mode"] != "gap":
        raise HTTPException(status_code=409, detail="Suggested edits belong to gap analysis.")
    try:
        await asyncio.to_thread(post_suggestion, sm, session, body.key.strip())
    except PublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await asyncio.to_thread(lambda: _view(sm.get_po_session(session_id) or session))


# -- ending ------------------------------------------------------------------------------------------


@router.post("/po-sessions/{session_id}/cancel")
async def cancel_po_session(session_id: int) -> dict[str, Any]:
    """Discard the session and stop its turn if one is running. `async` on
    purpose: a task can only be cancelled from the event loop's thread."""
    sm = StateManager()
    try:
        session = await asyncio.to_thread(sm.discard_po_session, session_id)
    except PoSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # The status moves first, so the cancelled turn finds itself no longer running
    # and does not put itself back in the queue.
    await cancel_po_session_task(session_id)
    return _view(session)


@router.delete("/po-sessions/{session_id}")
async def delete_po_session(session_id: int) -> dict[str, Any]:
    sm = StateManager()
    session = await asyncio.to_thread(sm.get_po_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No such PO session: {session_id}")
    if session["status"] not in (PO_PUBLISHING, PO_PUBLISHED, PO_DISCARDED):
        # Stop what is running first, so the turn's result does not land in a deleted row.
        await asyncio.to_thread(sm.discard_po_session, session_id)
        await cancel_po_session_task(session_id)
    try:
        removed = await asyncio.to_thread(sm.delete_po_session, session_id)
    except PoSessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": session_id, "removed": removed or {}}
