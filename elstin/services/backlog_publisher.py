"""Publishing a PO draft: the framework's write of a backlog into JIRA.

The PO agent writes nothing — it proposes (elstin/agents/roles/product_owner.py) —
so everything that reaches JIRA is made here, by elstin's own code, on a person's
say, behind its own switch (`po_jira_create_enabled`, per project, off by default;
checked again inside `JiraService.create_ticket`). This is AGENTS.md hard
constraint #4's "framework owns JIRA writes", extended from comments and
transitions to creating tickets.

**What it does, in order** — and why in that order:

1. the epic, in brainstorm mode only (a split or a gap works on an existing one);
2. the stories, in dependency order — a story's blockers exist before it does,
   so the board reads sensibly even if it stops half-way;
3. their sub-tasks, under the story each belongs to;
4. the links: `blocker` blocks `blocked`, for every dependency in the draft;
5. `sync_epic_from_jira`, which brings the epic and its new stories onto elstin's
   board. A new epic lands paused (`manual_gate`, exactly as "Add ticket" leaves
   it) and nothing starts without a person resuming it; an epic already on the
   board gains the new stories under its own gate.

**A bug draft** (bug mode) is one ticket: a `Bug`, under the session's epic when
a person named one, else on its own. It goes onto the board the same way — the
epic it is under is synced (gaining the bug), or, with no epic, the bug itself,
which `sync_epic_from_jira` makes its own sole work unit, paused like any new
ticket. It is published whole; `refs` does not apply.

**One story at a time, or all.** `refs` narrows a publish to some of the draft's
stories (a person's Publish button on one story): the same steps, for those
stories only. A brainstorm's epic is made first, by whichever publish comes first,
since every story goes under it; a story's links are made when it is — to the
stories it depends on, which must already exist (the page and
`po_sessions.publish_state` refuse otherwise). The session goes back to `ready`
for the next story, and ends `published` once everything is in JIRA.

**Resumable, and never twice.** After each create the new key is written to the
session's checkpoint (`publish_result`, through StateManager) before the next
begins, and every ticket carries a label naming the session and the draft ref it
came from (`elstin-po-<session>-<ref>`). A publish that failed — JIRA refused the
third create — is pressed again and skips what the checkpoint holds; if the
process died between a create and its checkpoint, the label is what finds the
orphan. A retry never makes a duplicate.

A QA story is labelled `qa` so elstin classifies it the way the draft said
(elstin/services/story_kind.py: an explicit label outranks prose); the *dev*
stories carry no extra label, and the draft's gate warns when elstin would read
one the other way.

Runs synchronously (blocking HTTP, sync SQLAlchemy) and is meant for
`asyncio.to_thread` — elstin/daemon/po_runner.py does that.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from elstin.services.epic_sync import levels_from_dependencies, sync_epic_from_jira
from elstin.services.po_sessions import PO_COMMENT_GUARD, PO_CREATE_GUARD, bug_markdown, epic_markdown, story_markdown
from elstin.services.run_activity import append_activity
from elstin.services.ticket_source import TicketWriteDisabled, TicketWriteError, get_ticket_source
from elstin.state.manager import StateManager
from elstin.state.manager_po import PO_MODE_BRAINSTORM, PO_MODE_BUG, po_run_id

logger = logging.getLogger("elstin.backlog_publisher")

_QA_LABEL = "qa"


class PublishError(RuntimeError):
    """The publish stopped. The message is for the person; what was already
    created is in the session's checkpoint and a second attempt continues from it."""


def marker_label(session_id: int, ref: str) -> str:
    """The label that ties a ticket to the draft part it was made from. Refs are
    `s1`-style; sub-tasks are `s1.2`, which a label may not hold as is."""
    return f"elstin-po-{session_id}-{re.sub(r'[^A-Za-z0-9_-]+', '-', ref)}"


def _qa_labelled(labels: list[str], kind: str) -> list[str]:
    cleaned = [label.strip() for label in labels if label and label.strip()]
    if kind == "qa" and _QA_LABEL not in {label.lower() for label in cleaned}:
        cleaned.append(_QA_LABEL)
    return cleaned


def _story_order(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The draft's stories with each after the ones it depends on, ties in the
    order the draft lists them. A cycle cannot get here (the gate refuses it); if
    one does, `levels_from_dependencies` places what it cannot sort in the last
    level rather than looping."""
    by_ref = {s["ref"]: s for s in stories}
    dependencies = {ref: [d for d in (s.get("depends_on") or []) if d in by_ref and d != ref] for ref, s in by_ref.items()}
    ordered = [ref for level in levels_from_dependencies(dependencies) for ref in sorted(level, key=lambda r: list(by_ref).index(r))]
    return [by_ref[ref] for ref in ordered]


def publish_backlog(
    sm: StateManager,
    session_id: int,
    *,
    refs: list[str] | None = None,
    settle_seconds: float = 2.0,
    sync: Callable[..., Any] = sync_epic_from_jira,
) -> dict[str, Any]:
    """Create the session's draft in JIRA (see the module docstring) — or, with
    `refs`, only those stories. Returns the final checkpoint. Raises PublishError,
    with progress kept, when a create or a link fails; the caller puts the session
    back to `ready`."""
    session = sm.get_po_session(session_id)
    if session is None or session.get("draft") is None:
        raise PublishError("There is no draft to publish.")
    draft: dict[str, Any] = session["draft"]
    project_id = session["project_id"]
    project_key = (session.get("jira_project_key") or "").strip()
    if not project_key:
        raise PublishError("Say which JIRA project the tickets go in.")
    source = get_ticket_source(project_id)
    if getattr(source, "name", "") != "jira":
        raise PublishError("Publishing is only built for JIRA projects.")

    result = session.get("publish_result") or {}
    created: dict[str, str] = dict(result.get("created") or {})
    links: set[str] = set(result.get("links") or [])
    # A second attempt looks for orphans (a create whose checkpoint never landed)
    # before making anything; the first has none to find.
    resuming = bool(result.get("started")) and bool(created or result.get("failed"))

    def fail(step: str, ref: str | None, exc: Exception) -> PublishError:
        message = f"{step}: {exc}"
        sm.record_po_publish(session_id, failed={"step": step, "ref": ref, "error": str(exc)[:1000]})
        return PublishError(message)

    def ensure(ref: str, issue_type: str, summary: str, description: str, parent: str | None, labels: list[str], step: str) -> str:
        if ref in created:
            return created[ref]
        marker = marker_label(session_id, ref)
        key: str | None = None
        if resuming:
            try:
                found = source.search_tickets(f'labels = "{marker}"', max_results=1)
            except Exception as exc:  # noqa: BLE001 — a failed lookup must not become a duplicate
                raise fail(f"Could not check whether {step} was already created", ref, exc) from exc
            key = found[0].key if found else None
            if key:
                logger.info("publish %s: %s was already created as %s", session_id, ref, key)
        if not key:
            try:
                key = source.create_ticket(project_key, issue_type, summary, description, parent_key=parent, labels=[*labels, marker], guard=PO_CREATE_GUARD)
            except (TicketWriteDisabled, TicketWriteError) as exc:
                raise fail(step, ref, exc) from exc
            except Exception as exc:  # noqa: BLE001 — anything else from the source is also "it stopped here"
                raise fail(step, ref, exc) from exc
        created[ref] = key
        sm.record_po_publish(session_id, created={ref: key}, failed=None)
        append_activity(po_run_id(session_id), type="status", phase="po", title=f"Created {key}", detail=f"{ref}: {summary[:200]}")
        return key

    if refs is not None:
        title = f"Publishing {', '.join(refs)} to JIRA"
    else:
        title = "Publishing to JIRA" if not resuming else "Publishing to JIRA — continuing where it stopped"
    append_activity(
        po_run_id(session_id), type="status", phase="po", title=title,
        detail=f"project {project_key}" + (f"; {len(created)} ticket(s) already created" if created else ""),
    )
    mode = session["mode"]
    if mode == PO_MODE_BUG:
        if refs is not None:
            raise PublishError("A bug draft is one ticket and is published whole.")
        bug = draft.get("bug") or {}
        parent = session.get("epic_key") or None
        bug_key = ensure(
            "bug", "Bug", bug.get("summary") or "", bug_markdown(bug), parent,
            _qa_labelled(bug.get("labels") or [], "dev"), f"Creating the bug {str(bug.get('summary') or '')[:50]!r}",
        )
        # With no epic the bug is its own root on the board (its board epic row has its key).
        sm.record_po_publish(session_id, epic_key=parent or bug_key)
        _sync_to_board(sm, session_id, project_id, parent or bug_key, [bug_key], sync, settle_seconds)
        final = sm.get_po_session(session_id) or {}
        return final.get("publish_result") or {}

    epic = draft.get("epic") or {}
    stories = [s for s in draft.get("stories") or [] if isinstance(s, dict)]
    if refs is not None:
        unknown = sorted(set(refs) - {s["ref"] for s in stories})
        if unknown:
            raise PublishError(f"There is no story {', '.join(unknown)} in this draft.")

    # 1. the epic
    if mode == PO_MODE_BRAINSTORM:
        epic_key = ensure(
            "epic", "Epic", epic.get("summary") or "", epic_markdown(epic), None, [], f"Creating the epic {epic.get('summary', '')[:50]!r}",
        )
    else:
        epic_key = session.get("epic_key") or ""
        if not epic_key:
            raise PublishError("This session has no epic to put the stories under.")
    sm.record_po_publish(session_id, epic_key=epic_key)

    # 2. the stories, blockers first
    ordered = [s for s in _story_order(stories) if refs is None or s["ref"] in refs]
    for story in ordered:
        ensure(
            story["ref"], story.get("issue_type") or "Story", story.get("summary") or "", story_markdown(story), epic_key,
            _qa_labelled(story.get("labels") or [], story.get("kind") or "dev"), f"Creating story {story['ref']} ({story.get('summary', '')[:50]!r})",
        )

    # 3. their sub-tasks
    for story in ordered:
        for index, sub in enumerate(story.get("subtasks") or [], start=1):
            if not isinstance(sub, dict) or not (sub.get("summary") or "").strip():
                continue
            ensure(
                f"{story['ref']}.{index}", "Sub-task", sub["summary"], sub.get("description") or "", created[story["ref"]], [],
                f"Creating sub-task {index} of {story['ref']}",
            )

    # 4. the links
    for story in ordered:
        for dependency in story.get("depends_on") or []:
            # A dependency on a draft story that is not in JIRA has nothing to link. A scoped
            # publish is refused over one (po_sessions._scoped_refusal), so this only guards it.
            blocker = created.get(dependency) or (dependency if dependency not in {s["ref"] for s in stories} else None)
            if not blocker or blocker == created[story["ref"]]:
                continue
            blocked = created[story["ref"]]
            link = f"{blocker}>{blocked}"
            if link in links:
                continue
            try:
                source.link_tickets(blocker, blocked, guard=PO_CREATE_GUARD)
            except Exception as exc:  # noqa: BLE001
                raise fail(f"Linking {blocker} as blocking {blocked}", story["ref"], exc) from exc
            links.add(link)
            sm.record_po_publish(session_id, link=link, failed=None)

    # 5. onto the board
    _sync_to_board(sm, session_id, project_id, epic_key, [created[s["ref"]] for s in stories if s["ref"] in created], sync, settle_seconds)
    final = sm.get_po_session(session_id) or {}
    return final.get("publish_result") or {}


def _sync_to_board(
    sm: StateManager, session_id: int, project_id: int, epic_key: str, story_keys: list[str],
    sync: Callable[..., Any], settle_seconds: float,
) -> None:
    """Bring the epic and its new stories onto elstin's board. JIRA's search index
    trails a create by a moment, so a sync that finds fewer of the new stories than
    were made is tried again a few times before it is reported as it stands. Never
    raises: the tickets exist, and a person can sync them with "Add ticket" — what
    failed is recorded and shown."""
    error: str | None = None
    on_board = 0
    for attempt in range(4):
        try:
            sync(epic_key, project_id=project_id)
            error = None
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:500]
            logger.warning("publish %s: syncing %s onto the board failed: %s", session_id, epic_key, exc)
        on_board = sum(1 for key in story_keys if sm.get_story(key) is not None)
        if error is None and on_board >= len(story_keys):
            break
        if attempt < 3 and settle_seconds > 0:
            time.sleep(settle_seconds)
    sm.record_po_publish(
        session_id, synced=error is None and on_board >= len(story_keys), sync_error=error,
        synced_stories=on_board,
    )


def post_suggestion(sm: StateManager, session: dict[str, Any], key: str) -> None:
    """Post an existing story's suggested edit to its ticket as a comment — one
    ticket, on a person's say, never the ticket's text itself ("flag, don't
    overwrite"). Behind `po_jira_comment_enabled`, the PO's own comment gate; a source that has
    it off says so. Recorded, so the same suggestion is not posted twice."""
    draft = session.get("draft") or {}
    row = next((r for r in draft.get("existing") or [] if isinstance(r, dict) and r.get("key") == key), None)
    text = ((row or {}).get("suggested_edit") or "").strip()
    if row is None:
        raise PublishError(f"{key} is not one of this draft's existing stories.")
    if not text:
        raise PublishError(f"There is no suggested edit for {key}.")
    if key in ((session.get("publish_result") or {}).get("comments") or []):
        raise PublishError(f"The suggestion for {key} was already posted.")
    source = get_ticket_source(session["project_id"])
    body = f"Suggested edit, from elstin's PO agent (for a person to apply — this ticket was not changed):\n\n{text}"
    try:
        posted = source.add_comment(key, body, guard=PO_COMMENT_GUARD)
    except Exception as exc:  # noqa: BLE001
        raise PublishError(f"JIRA refused the comment on {key}: {exc}") from exc
    if not posted:
        raise PublishError(
            f"Posting suggestions as comments is switched off for this project ({PO_COMMENT_GUARD}, under PO agent in settings). "
            "Turn it on, or copy the suggestion across by hand."
        )
    sm.record_po_publish(session["id"], comment=key)
