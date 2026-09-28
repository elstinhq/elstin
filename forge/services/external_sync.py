"""Keeping the board in step with the source it was pulled from.

The board (forge's Postgres) is what the agents read; the source (JIRA, …)
owns what a ticket *says*. Every walk of an epic's tree — the manual sync,
the background sweep — passes the tickets it just read through
`reconcile_epic`, which decides, per ticket, what that means for the board:

**Content.** A ticket whose text differs from what forge last resolved is
either applied or held. It is applied straight away when nothing depends on
the old text — a story that has not been planned, a finished one, a
sub-task (only ever read into its parent's brief), an epic. It is *held*
when a plan or run was built on the old text: the new text is stored on the
story as `external_pending` and a human chooses to apply it, apply it and
re-plan, or dismiss it (forge/state/manager_external.py). Silently rewriting
a description under a running agent would leave the plan and the ticket
disagreeing with no one told.

A change to a sub-task holds its parent the same way, for the same reason:
the sub-task's text is part of the parent's brief.

**Closure.** A ticket the source reports done, closed, or gone stops being
dispatched. A story that has not started is marked `skipped` (which the
reconciler already counts as resolved, so later levels are not blocked
forever) with parked_reason `closed_externally`, which is also how a reopen
knows the skip was ours to undo. A story already in flight is only flagged —
it finishes, and the flag says the ticket was closed underneath it. A closed
epic is put on hold (manual_gate) and not released automatically.

**On hold.** A story whose status is one of `ticket_hold_statuses` (On Hold,
Blocked, … — JIRA files those under To Do, so they are not closed) is left
alone the same way while it has not started: `skipped` with parked_reason
`on_hold_in_source`, which is how it knows to come back to `pending` once the
status moves on. A plan waiting for approval is dropped with it — the ticket
will likely change while it waits. A story already in flight finishes.

**Gone.** A story the walk did not return is only treated as gone after the
source confirms it (`is_missing`), and never when the walk hit the source's
size cap — a truncated tree or a failed child search must not read as
"deleted", because the cost of being wrong is skipping real work.

A sweep never touches orchestration state it does not own: kind, gates,
worktrees, PRs and levels stay exactly as they were.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from forge.config.store import resolve_config
from forge.services.ticket_source import Ticket, TicketSource, content_hash
from forge.state.manager import StateManager

logger = logging.getLogger("forge.external_sync")

CLOSED_EXTERNALLY = "closed_externally"
ON_HOLD_IN_SOURCE = "on_hold_in_source"
GONE_STATUS = "deleted or no longer visible"

# Statuses where no plan or run depends on the ticket text.
_SETTLED = frozenset({"merged", "done", "skipped"})
# Statuses where nothing has been done for the story yet.
_NOT_STARTED = frozenset({"pending", "awaiting_plan_approval"})


@dataclass
class ReconcileReport:
    # The board key of the epic that was walked. What a person typed to add it
    # (a URL, owner/repo#1) is not the key it is stored under.
    epic_key: str = ""
    applied: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)  # newly skipped
    closed_in_flight: list[str] = field(default_factory=list)  # flagged only
    reopened: list[str] = field(default_factory=list)
    on_hold: list[str] = field(default_factory=list)  # newly set aside
    off_hold: list[str] = field(default_factory=list)  # back to pending

    _FIELDS = ("applied", "flagged", "closed", "closed_in_flight", "reopened", "on_hold", "off_hold")

    def merge(self, other: "ReconcileReport") -> None:
        for name in self._FIELDS:
            getattr(self, name).extend(getattr(other, name))

    def changed(self) -> bool:
        return any(getattr(self, name) for name in self._FIELDS)

    def to_dict(self) -> dict:
        return {name: list(getattr(self, name)) for name in self._FIELDS}


def _same(local: str | None, incoming: str | None) -> bool:
    return (local or "").strip() == (incoming or "").strip()


def _applies_directly(story: dict) -> bool:
    if story["parent_key"]:
        return True
    if story["status"] in _SETTLED:
        return True
    return story["status"] == "pending" and not story["thread_id"]


def _changed_fields(row: dict, ticket: Ticket) -> dict:
    changed: dict = {}
    if not _same(row["summary"], ticket.summary):
        changed["summary"] = ticket.summary
    if not _same(row["description"], ticket.description):
        changed["description"] = ticket.description
    return changed


def _is_own_edit(pending: dict | None) -> bool:
    return bool(pending) and ("summary" in pending or "description" in pending)


def _reconcile_content(
    sm: StateManager, kind: str, row: dict, ticket: Ticket, report: ReconcileReport, *, sub_reasons: list[str] | None = None,
) -> bool:
    """Returns True when the ticket's own content was found to differ from
    what forge last resolved (whether it was applied or held)."""
    key = row["jira_key"]
    incoming = content_hash(ticket.summary, ticket.description)
    stored = row["external_hash"]
    differs = _changed_fields(row, ticket)
    sub_reasons = sub_reasons or []

    resolved = stored == incoming or (stored is None and not differs)
    if resolved:
        sm.mark_external_seen(kind, key, baseline_hash=incoming, external_status=ticket.status)
        pending = row.get("external_pending")
        if kind == "story" and _is_own_edit(pending) and not sub_reasons:
            # The source went back to the version forge already resolved.
            sm.set_story_external_pending(key, None)
        elif kind == "story" and sub_reasons and not _applies_directly(row):
            _hold(sm, key, {}, incoming, ["sub-task changed: " + ", ".join(sub_reasons)], report)
        return False

    if kind == "epic" or _applies_directly(row) or not differs:
        sm.apply_external_content(kind, key, summary=ticket.summary, description=ticket.description or None, content_hash=incoming)
        sm.mark_external_seen(kind, key, external_status=ticket.status)
        report.applied.append(key)
        return True

    reasons = ["edited in the source"] + (["sub-task changed: " + ", ".join(sub_reasons)] if sub_reasons else [])
    _hold(sm, key, differs, incoming, reasons, report)
    return True


def _hold(sm: StateManager, key: str, differs: dict, incoming: str, reasons: list[str], report: ReconcileReport) -> None:
    sm.set_story_external_pending(key, {**differs, "hash": incoming, "reasons": reasons})
    if key not in report.flagged:
        report.flagged.append(key)
    logger.info("story %s: source changed after planning — held for review (%s)", key, "; ".join(reasons))


def _reconcile_closure(
    sm: StateManager, kind: str, row: dict, *, closed: bool, status_text: str | None, report: ReconcileReport,
) -> None:
    key = row["jira_key"]
    previous = sm.set_external_closed(kind, key, closed=closed, status_text=status_text)

    if kind == "epic":
        if closed and not previous:
            sm.set_epic_manual_gate(key, True)
            report.closed.append(key)
            logger.info("epic %s closed in the source — put on hold", key)
        return

    if closed and not previous:
        if row["status"] in _NOT_STARTED or _held(row):
            sm.set_story_status(key, "skipped", parked_reason=CLOSED_EXTERNALLY)
            report.closed.append(key)
            logger.info("story %s closed in the source before it started — skipped", key)
        else:
            report.closed_in_flight.append(key)
            logger.warning("story %s closed in the source while %s — left to finish", key, row["status"])
    elif previous and not closed:
        report.reopened.append(key)
        if row["status"] == "skipped" and row["parked_reason"] == CLOSED_EXTERNALLY:
            sm.set_story_status(key, "pending")
            logger.info("story %s reopened in the source — back to pending", key)


def hold_statuses(project_id: int | None) -> frozenset[str]:
    """The `ticket_hold_statuses` setting for a project, lower-cased."""
    raw = resolve_config("ticket_hold_statuses", project_id) or ""
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _held(row: dict) -> bool:
    return row["status"] == "skipped" and row.get("parked_reason") == ON_HOLD_IN_SOURCE


def _reconcile_hold(
    sm: StateManager, row: dict, *, status_text: str | None, closed: bool, holds: frozenset[str], report: ReconcileReport,
) -> None:
    """Set a story aside while the source has it on hold, and bring it back
    when it no longer does — see the module docstring. Closure wins: a
    closed ticket is `closed_externally`, not held."""
    key = row["jira_key"]
    on_hold = not closed and (status_text or "").strip().lower() in holds
    if on_hold and row["status"] in _NOT_STARTED:
        sm.set_story_status(key, "skipped", parked_reason=ON_HOLD_IN_SOURCE)
        report.on_hold.append(key)
        logger.info("story %s is %s in the source — set aside until it is not", key, status_text)
    elif not on_hold and not closed and _held(row):
        sm.set_story_status(key, "pending")
        report.off_hold.append(key)
        logger.info("story %s is no longer on hold in the source (%s) — back to pending", key, status_text)


def reconcile_epic(
    sm: StateManager,
    source: TicketSource,
    *,
    epic_key: str,
    root: Ticket,
    top_level: list[Ticket],
    subtasks: list[Ticket],
    tickets: list[Ticket],
) -> ReconcileReport:
    """`tickets` is the whole walk (root included); `top_level`/`subtasks` are
    its split into dispatchable stories and context — see epic_sync."""
    report = ReconcileReport(epic_key=epic_key)

    epic = sm.get_epic(epic_key)
    holds = hold_statuses(epic["project_id"] if epic else None)
    if epic is not None:
        _reconcile_content(sm, "epic", epic, root, report)
        _reconcile_closure(sm, "epic", epic, closed=root.closed, status_text=root.status, report=report)

    # Sub-tasks first: a change to one is a reason to hold its parent.
    sub_reasons: dict[str, list[str]] = {}
    for ticket in subtasks:
        row = sm.get_story(ticket.key)
        if row is None:
            continue
        if _reconcile_content(sm, "story", row, ticket, report) and row["parent_key"]:
            sub_reasons.setdefault(row["parent_key"], []).append(ticket.external_id or ticket.key)

    for ticket in top_level:
        row = sm.get_story(ticket.key)
        if row is None:
            continue
        _reconcile_content(sm, "story", row, ticket, report, sub_reasons=sub_reasons.get(ticket.key))
        row = sm.get_story(ticket.key)  # content pass does not touch status, but stay honest
        _reconcile_closure(sm, "story", row, closed=ticket.closed, status_text=ticket.status, report=report)
        _reconcile_hold(
            sm, sm.get_story(ticket.key), status_text=ticket.status, closed=ticket.closed, holds=holds, report=report,
        )

    _reconcile_gone(sm, source, epic_key, tickets, report)
    return report


def _reconcile_gone(
    sm: StateManager, source: TicketSource, epic_key: str, tickets: list[Ticket], report: ReconcileReport,
) -> None:
    cap = getattr(source, "max_tree_tickets", None)
    if cap is not None and len(tickets) >= cap:
        logger.info("epic %s: tree hit the source's %d-ticket cap — not checking for missing tickets", epic_key, cap)
        return

    seen = {t.key for t in tickets}
    for row in sm.list_stories_for_epic(epic_key):
        if row["parent_key"] or row["external_source"] != source.name or row["jira_key"] in seen or row["external_closed"]:
            continue
        try:
            missing = source.is_missing(row["jira_key"])
        except Exception as exc:  # noqa: BLE001 — unknown is not gone
            logger.warning("story %s: could not confirm it was deleted (%s) — left alone", row["jira_key"], exc)
            continue
        if missing:
            _reconcile_closure(sm, "story", row, closed=True, status_text=GONE_STATUS, report=report)
