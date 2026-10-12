"""The daemon's side of PO sessions: pick up a queued turn, gather what the PO
agent needs, run it, and save what came back.

The API only creates a session or queues another turn
(elstin/api/po_routes.py); everything that takes minutes happens here, in the
same process as the rest of the daemon, started by `poll_once` the way a story's
run is. The reconciler is not involved — a PO session is not a story, has no
dependencies and dispatches nothing — so it stays pure.

**One turn at a time** (`run_po_sessions` takes nothing while one is running): a
turn is a model reading a codebase, and a laptop running stories in parallel
does not need a second one on top. A queued session waits for the next poll.

**What a turn is given** (`_gather`), assembled here because the daemon owns the
database and the credentials, the same separation the QA brief follows (AGENTS.md
hard constraint #3):

- the epic and its stories from the ticket source, and elstin's own board for it;
- the Confluence pages the epic (or the brief) links to, and a Confluence client
  for the agent's own searches when the project has one;
- the project's guide and accepted lessons, and a codebase skeleton;
- on a revision: the current draft — which a person may have edited — the
  conversation, the answers to its questions and whatever the person wrote since
  the last turn.

Reads go to the epic branch where the epic is on the board (that is where the
stories already built live), else to the project's clones.

**Publishing** (`start_publish`) is the other thing this module runs: the draft
into JIRA (elstin/services/backlog_publisher.py), as a background task of its own,
started by the API on a person's say so. It runs in a thread (blocking HTTP) and
keeps its progress in the session's checkpoint, so a publish that stops — JIRA
refused a create, or elstin restarted — leaves the session `ready` with the reason,
and pressing Publish again continues without repeating anything.

**Crash and shutdown.** A session left `running` by a daemon that died is queued
again (`requeue_orphaned_po_sessions`), and so is one a shutdown interrupted; one
left `publishing` goes back to `ready` with a note (`recover_interrupted_publishes`).
A person's discard cancels the task; the result of a turn that finds its session no
longer running is dropped.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from functools import partial
from typing import Any

from elstin.agents.roles.po_gate import DraftContext
from elstin.agents.roles.po_loop import PoLoopError, render_person_message, run_product_owner
from elstin.agents.roles.product_owner import PoInputs
from elstin.config.store import get_config
from elstin.services.confluence_service import ConfluenceService, page_ids_in
from elstin.services.backlog_publisher import PublishError, publish_backlog
from elstin.services.po_sessions import (
    build_context, created_tickets, draft_context, draft_for_storage, keep_published, render_epic_context,
)
from elstin.services.project_guide import project_guide_for_run
from elstin.services.run_activity import append_activity
from elstin.services.ticket_source import get_ticket_source, ticket_source_configured
from elstin.services.tracing import traced_operation
from elstin.services.usage import story_spend_usd
from elstin.state.manager import StateManager
from elstin.state.manager_po import po_run_id
from elstin.tools.codebase_skeleton import build_workspace_skeleton
from elstin.tools.po_tools import PoResearch, render_board

logger = logging.getLogger("elstin.po_runner")

# The turns running in this process, by session id. In-process like the story
# run registry: the daemon is one process.
_tasks: dict[int, asyncio.Task] = {}
# The publishes running in this process, by session id.
_publish_tasks: dict[int, asyncio.Task] = {}

_MAX_LINKED_PAGES = 5
_LINKED_PAGE_CHARS = 6_000
_MAX_SKELETON_CHARS = 12_000
_MAX_BRIEF_CHARS = 20_000


def live_po_session_ids() -> frozenset[int]:
    return frozenset(_tasks)


async def run_po_sessions(sm: StateManager) -> list[asyncio.Task]:
    """Start the next queued session's turn, unless one is already running.
    Called from `poll_once`; returns the task it started (for tests to await)."""
    try:
        requeued = await asyncio.to_thread(sm.requeue_orphaned_po_sessions, live_po_session_ids())
        if requeued:
            logger.warning("PO sessions %s were running with nobody working on them — queued again", requeued)
    except Exception:  # noqa: BLE001 — retried next poll
        logger.exception("could not requeue orphaned PO sessions")
        return []
    try:
        recovered = await asyncio.to_thread(sm.recover_interrupted_publishes, frozenset(_publish_tasks))
        if recovered:
            logger.warning("PO sessions %s were publishing with nobody working on them — back to ready", recovered)
    except Exception:  # noqa: BLE001 — retried next poll
        logger.exception("could not recover interrupted PO publishes")
    if _tasks:
        return []
    try:
        session = await asyncio.to_thread(sm.claim_next_po_session)
    except Exception:  # noqa: BLE001
        logger.exception("could not claim a queued PO session")
        return []
    if session is None:
        return []
    session_id = session["id"]
    task = asyncio.create_task(_run_turn(sm, session), name=f"po-session:{session_id}")
    _tasks[session_id] = task
    task.add_done_callback(lambda _task, sid=session_id: _tasks.pop(sid, None))
    return [task]


async def cancel_po_session_task(session_id: int, *, wait_seconds: float = 30.0) -> bool:
    """Stop `session_id`'s turn if one is running here. Must run on the event
    loop's thread (cancelling a task from another is not safe), which is why
    the API route that calls it is `async`. True when there was one to stop."""
    task = _tasks.get(session_id)
    if task is None or task.done():
        return False
    task.cancel()
    # `wait` rather than awaiting the task: it was cancelled on purpose, and how
    # it unwinds is its own business — only that it has, or that we gave up.
    await asyncio.wait({task}, timeout=wait_seconds)
    return True


async def start_publish(sm: StateManager, session_id: int, refs: list[str] | None = None) -> asyncio.Task:
    """Publish `session_id`'s draft — or only the stories `refs` names — in the
    background. The session must already be `publishing` (StateManager.begin_po_publish
    — the API does that, so a second press is refused before it gets here)."""
    coroutine = _run_publish(sm, session_id, refs) if refs is not None else _run_publish(sm, session_id)
    task = asyncio.create_task(coroutine, name=f"po-publish:{session_id}")
    _publish_tasks[session_id] = task
    task.add_done_callback(lambda _task, sid=session_id: _publish_tasks.pop(sid, None))
    return task


async def _run_publish(sm: StateManager, session_id: int, refs: list[str] | None = None) -> None:
    run_id = po_run_id(session_id)
    try:
        await asyncio.to_thread(publish_backlog, sm, session_id, **({"refs": refs} if refs is not None else {}))
    except asyncio.CancelledError:
        # Shutdown. The thread finishes what it was doing on its own; the session is
        # recovered as interrupted by the next daemon, and the checkpoint is intact.
        raise
    except PublishError as exc:
        await asyncio.to_thread(sm.fail_po_publish, session_id, str(exc))
        append_activity(run_id, type="status", phase="po", title="Publishing stopped", detail=str(exc)[:1000])
        logger.warning("PO session %s: publishing stopped: %s", session_id, exc)
        return
    except Exception as exc:  # noqa: BLE001 — never the poll loop's problem
        logger.exception("PO session %s: publishing failed", session_id)
        await asyncio.to_thread(sm.fail_po_publish, session_id, f"Unexpected error: {exc}")
        append_activity(run_id, type="status", phase="po", title="Publishing stopped", detail=f"Unexpected error: {exc}"[:1000])
        return
    saved = await asyncio.to_thread(partial(sm.finish_po_publish, session_id, scoped=refs is not None))
    if saved is not None:
        created = (saved.get("publish_result") or {}).get("created") or {}
        append_activity(
            run_id, type="status", phase="po",
            title="Published" if saved["status"] == "published" else f"Published {', '.join(refs or [])}",
            detail=f"{len(created)} ticket(s) created in JIRA" + (f"; epic {saved['publish_result'].get('epic_key')}" if saved.get("publish_result") else ""),
        )


def live_publish_session_ids() -> frozenset[int]:
    return frozenset(_publish_tasks)


async def cancel_po_work() -> None:
    """Shutdown: cancel every turn and publish and wait for each to unwind (a turn
    puts its session back in the queue; a publish is recovered by the next daemon)."""
    tasks = [*_tasks.values(), *_publish_tasks.values()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# -- one turn -------------------------------------------------------------------------------


async def _run_turn(sm: StateManager, session: dict[str, Any]) -> None:
    session_id, project_id = session["id"], session["project_id"]
    run_id = po_run_id(session_id)
    try:
        with traced_operation(
            project_id, name=f"elstin:po:{session_id}", tags=["elstin", "po"],
            metadata={"session_id": session_id, "project_id": project_id, "mode": session["mode"], "epic_key": session.get("epic_key")},
        ):
            await _draft(sm, session)
    except asyncio.CancelledError:
        # A shutdown puts the turn back; a person's discard has already moved the
        # session out of `running`, so there is nothing to put back.
        await asyncio.to_thread(sm.requeue_orphaned_po_sessions, live_po_session_ids() - {session_id})
        raise
    except PoLoopError as exc:
        await _fail(sm, session_id, run_id, str(exc))
    except Exception as exc:  # noqa: BLE001 — a turn's failure belongs to its session, never to the poll loop
        logger.exception("PO session %s: the turn failed", session_id)
        await _fail(sm, session_id, run_id, f"Unexpected error: {exc}")


async def _fail(sm: StateManager, session_id: int, run_id: str, message: str) -> None:
    cost = await asyncio.to_thread(story_spend_usd, run_id)
    saved = await asyncio.to_thread(sm.fail_po_session, session_id, message, cost_usd=cost)
    if saved is not None:
        append_activity(run_id, type="status", phase="po", title="The PO turn failed", detail=message[:1000])
    logger.warning("PO session %s failed: %s", session_id, message)


async def _draft(sm: StateManager, session: dict[str, Any]) -> None:
    session_id, project_id = session["id"], session["project_id"]
    run_id = po_run_id(session_id)
    gathered = await _gather(sm, session)

    def take_messages() -> list[str]:
        taken = sm.take_po_messages(session_id)
        if taken:
            append_activity(
                run_id, type="status", phase="po",
                title=f"The agent read {len(taken)} message{'s' if len(taken) != 1 else ''} from a person",
            )
        return [render_person_message(message["text"]) for message in taken]

    result = await run_product_owner(
        inputs=gathered.inputs, session_id=session_id, run_id=run_id, project_id=project_id, ctx=gathered.ctx,
        research=gathered.research, project_path=gathered.project_path, repo_roots=gathered.read_roots,
        default_label=gathered.default_label, read_only_labels=gathered.read_only,
        ticket_source=gathered.source, jira_project_key=session.get("jira_project_key"),
        confluence=gathered.confluence, linked_page_ids=frozenset(gathered.linked_page_ids),
        board_reader=gathered.board_reader, pending_messages=take_messages,
        explore=session.get("draft") is None, spent_usd=session.get("cost_usd") or 0.0,
    )

    cost = await asyncio.to_thread(story_spend_usd, run_id)
    reply = result.reply or _default_reply(result.draft)
    # What is already in JIRA stays as it was made, whatever the revision did to it.
    revised = keep_published(result.draft, session.get("draft"), session)
    saved = await asyncio.to_thread(
        sm.finish_po_turn, session_id, draft=draft_for_storage(revised), questions=result.questions,
        reply=reply, cost_usd=cost,
        context=build_context(session.get("epic_key"), gathered.tree, set(gathered.research.jira)),
    )
    if saved is None:
        logger.info("PO session %s is no longer running — its result is dropped", session_id)
        return
    stories = len(result.draft.get("stories") or [])
    bug = result.draft.get("bug") if isinstance(result.draft.get("bug"), dict) else None
    append_activity(
        run_id, type="status", phase="po",
        title="Draft ready" if not result.questions else "Draft ready — the agent has questions",
        detail=(
            f"one bug, cause in {bug.get('cause_in')} at {bug.get('confidence')} confidence" if bug is not None
            else f"{stories} proposed stor{'y' if stories == 1 else 'ies'}"
        )
        + (f"; {len([d for d in result.defects if d.get('blocking', True)])} problem(s) the gate could not clear" if any(d.get("blocking", True) for d in result.defects) else ""),
    )


def _default_reply(draft: dict[str, Any]) -> str:
    if isinstance(draft.get("bug"), dict):
        return "Here is the bug as I traced it. Edit it, or tell me what you know that changes the picture."
    stories = len(draft.get("stories") or [])
    return f"Here is the draft: {stories} proposed stor{'y' if stories == 1 else 'ies'}. Edit it, or tell me what to change."


class _Gathered:
    def __init__(self) -> None:
        self.inputs: PoInputs
        self.ctx: DraftContext
        self.research = PoResearch()
        self.tree: list = []
        self.source: Any = None
        self.confluence: Any = None
        self.linked_page_ids: list[str] = []
        self.board_reader = None
        self.project_path = ""
        self.read_roots: dict[str, str] = {}
        self.default_label = ""
        self.read_only: list[str] = []


async def _gather(sm: StateManager, session: dict[str, Any]) -> _Gathered:
    """Everything a turn is given. Raises PoLoopError for what makes the turn
    impossible (an epic that cannot be read, a project with no folders)."""
    # Imported here: daemon/main.py imports this module to call it from poll_once.
    from elstin.daemon.main import _read_roots, _workspace_roots

    out = _Gathered()
    session_id, project_id, epic_key = session["id"], session["project_id"], session.get("epic_key")
    run_id = po_run_id(session_id)

    # -- the ticket source and the epic ----------------------------------------------------
    if await asyncio.to_thread(ticket_source_configured, project_id):
        try:
            out.source = await asyncio.to_thread(get_ticket_source, project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PO session %s: the ticket source is unavailable: %s", session_id, exc)
    epic_context, shown = "", {}
    root = None
    if epic_key:
        if out.source is None:
            raise PoLoopError(
                f"This session works on the epic {epic_key}, and the project's ticket source is not configured, "
                "so the epic cannot be read. Set the connection at /settings or in the project's settings."
            )
        try:
            out.tree = await asyncio.to_thread(out.source.get_ticket_tree, epic_key)
        except Exception as exc:  # noqa: BLE001
            raise PoLoopError(f"Could not read {epic_key} from {out.source.name}: {exc}") from exc
        root = next((t for t in out.tree if t.key == epic_key), None)
        if root is None:
            raise PoLoopError(f"{epic_key} was not found in {out.source.name}.")
        epic_context, shown = render_epic_context(epic_key, out.tree)
        for key, text in shown.items():
            out.research.saw_jira(key, text)

    # -- Confluence -----------------------------------------------------------------------------
    linked_text = ""
    capabilities: list[str] = []
    if out.source is not None and out.source.name == "jira":
        capabilities.append("JIRA (jira_search, jira_get_issue)")
    if await asyncio.to_thread(ConfluenceService.is_configured, project_id):
        try:
            out.confluence = await asyncio.to_thread(ConfluenceService, project_id)
            capabilities.append("Confluence (confluence_search, confluence_get_page)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("PO session %s: Confluence is unavailable: %s", session_id, exc)
    if out.confluence is not None:
        # Pages a person pointed at: the epic's own text, and the brief.
        out.linked_page_ids = page_ids_in(f"{(root.description if root else '') or ''}\n{session['brief']}")[:_MAX_LINKED_PAGES]
        blocks: list[str] = []
        for page_id in out.linked_page_ids:
            try:
                page = await asyncio.to_thread(out.confluence.get_page, page_id, allow_ids=out.linked_page_ids)
            except Exception as exc:  # noqa: BLE001 — one unreadable page is not a reason to stop
                blocks.append(f"(page {page_id} could not be read: {exc})")
                continue
            rendered = f"{page.title} [{page.space}] (page {page.id})\n{page.url}\n\n{page.text}"
            out.research.saw_page(page.id, rendered)
            blocks.append(rendered if len(rendered) <= _LINKED_PAGE_CHARS else rendered[:_LINKED_PAGE_CHARS] + "\n…[truncated]")
        linked_text = "\n\n---\n\n".join(blocks)

    # -- the board ------------------------------------------------------------------------------
    epic_row = await asyncio.to_thread(sm.get_epic, epic_key) if epic_key else None
    board_context = ""
    if epic_row is not None and epic_row.get("project_id") == project_id:
        board_context = render_board(epic_key, await asyncio.to_thread(_board_rows, sm, project_id, epic_key))
    out.board_reader = lambda key: _board_rows(sm, project_id, key)

    # -- the code ---------------------------------------------------------------------------------
    default_repo = (epic_row or {}).get("repo") if epic_row else None
    default_label, roots, read_only, _reference = await asyncio.to_thread(
        _workspace_roots, sm, {"project_id": project_id}, default_repo,
    )
    if not roots:
        raise PoLoopError(
            "This project has no folders yet, so there is no code to read. Add a repo or a reference folder to the "
            "project, then send a message to try again."
        )
    base = get_config("worktrees_base_dir") or "/data/repos"
    # The epic branch where the epic is on the board: that is where its merged stories' code is.
    out.read_roots = _read_roots(epic_row, roots, {}, base) if epic_row is not None else dict(roots)
    out.default_label = default_label
    out.read_only = read_only
    out.project_path = out.read_roots.get(default_label) or next(iter(out.read_roots.values()))

    guide = await project_guide_for_run(sm, project_id, roots)
    try:
        skeleton = await asyncio.to_thread(
            build_workspace_skeleton, out.project_path, out.read_roots, default_label=default_label,
            max_chars=_MAX_SKELETON_CHARS,
        )
    except Exception as exc:  # noqa: BLE001 — a map is a head start, never a reason not to draft
        logger.warning("PO session %s: could not build the codebase skeleton: %s", session_id, exc)
        skeleton = ""

    # -- the person's side: the brief, or the revision's context --------------------------------
    messages = await asyncio.to_thread(sm.list_po_messages, session_id)
    new_messages = await asyncio.to_thread(sm.take_po_messages, session_id)
    new_ids = {m["id"] for m in new_messages}
    person_brief = session["brief"][:_MAX_BRIEF_CHARS]
    revise: dict[str, Any] | None = None
    if session.get("draft") is not None:
        history = [m for m in messages[1:] if m["id"] not in new_ids]
        revise = {
            "draft": _without_marks(session["draft"]), "version": session["draft_version"],
            "edited_by_person": session["edited_by_person"], "person_edits": session["person_edits"],
            "history": history, "answers": session.get("answers") or [],
            "new_messages": [m["text"] for m in new_messages],
            # Stories (and a brainstorm's epic) a person has already published: final.
            "published": {ref: key for ref, key in created_tickets(session).items() if "." not in ref},
        }
    elif new_messages:
        # No draft yet (an earlier turn failed): what the person has added since is part of the brief.
        person_brief += "\n\nMore from the person since:\n" + "\n\n".join(m["text"] for m in new_messages)

    out.inputs = PoInputs(
        mode=session["mode"], person_brief=person_brief, epic_key=epic_key, epic_context=epic_context,
        board_context=board_context, linked_pages=linked_text, guide=guide, skeleton=skeleton, revise=revise,
        capabilities=capabilities,
    )
    fresh = build_context(epic_key, out.tree, set(out.research.jira))
    out.ctx = draft_context({**session, "context": fresh})
    append_activity(
        run_id, type="status", phase="po", title="Gathered what the agent starts from",
        detail=", ".join(filter(None, [
            f"epic {epic_key} with {len(out.ctx.existing)} stor{'y' if len(out.ctx.existing) == 1 else 'ies'}" if epic_key else "",
            f"{len(out.linked_page_ids)} linked Confluence page(s)" if out.linked_page_ids else "",
            f"{len(roots)} folder(s)",
        ])),
    )
    return out


def _without_marks(draft: dict[str, Any]) -> dict[str, Any]:
    """The draft as the model should see it: the server's `verified` marks are
    not part of the schema it answers in, and would be copied back."""
    shown = copy.deepcopy(draft)
    for group in ("stories", "existing"):
        for row in shown.get(group) or []:
            for item in row.get("evidence") or [] if isinstance(row, dict) else []:
                if isinstance(item, dict):
                    item.pop("verified", None)
    return shown


def _board_rows(sm: StateManager, project_id: int, epic_key: str) -> list[dict[str, Any]]:
    """elstin's board for one epic of this project, as the agent's `board_status`
    tool and the brief show it: the epic (its phase and pull requests) first,
    then each top-level story with elstin's status beside the source's."""
    epic = sm.get_epic(epic_key)
    if epic is None or epic.get("project_id") != project_id:
        return []
    rows: list[dict[str, Any]] = [{
        "key": epic_key, "summary": epic.get("summary"), "status": epic.get("phase"),
        "pull_requests": [
            {"repo": pr.get("repo_label"), "url": pr.get("url"), "state": pr.get("state")}
            for pr in sm.list_epic_pull_requests(epic_key)
        ],
    }]
    for story in sorted(sm.list_stories_for_epic(epic_key), key=lambda s: s["jira_key"]):
        if story.get("parent_key"):
            continue
        rows.append({
            "key": story["jira_key"], "summary": story.get("summary"), "status": story.get("status"),
            "kind": story.get("kind"), "parked_reason": story.get("parked_reason"),
            "depends_on": story.get("depends_on") or [], "person_actions": story.get("person_actions") or [],
            "jira_status": story.get("external_status"),
        })
    return rows
