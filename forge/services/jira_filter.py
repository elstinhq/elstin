"""Filter-based ticket ingestion: one saved JIRA query per project,
swept periodically, instead of a human typing one issue key at a time.

`POST /api/epics/sync` (one key, one epic) still exists and is still the
right tool for a one-off. This module is for the standing case — "every
MMA ticket assigned to me that isn't done yet is forge's problem" — where
naming each ticket individually means the work only starts once somebody
remembers to come and add it.

**The stored filter is not JQL.** It is the *parts* of a query
(project / assignee / statuses / labels / issue types), which `build_jql`
compiles, plus a free-form `extra_jql` for everything those fields can't
express. Storing the parts is what lets the settings dialog round-trip an
edit; compiling on every use is what keeps one quoting/escaping
implementation instead of one per caller. A filter with no parts at all is
rejected rather than compiled into a query that matches the entire JIRA
instance.

**A sweep only ever adds.** Every matched ticket is classified
(`evaluate_matches`) into exactly one action, and only "new" and
"already synced" roots are handed to `sync_epic_from_jira`:

- a **sub-task** is dropped, for the same reason epic_sync never dispatches
  one — it is not independently deliverable, and a JQL query has no idea it
  matched half a story;
- a ticket whose **parent is also in the result set** is dropped, because
  syncing the parent pulls it in as a child anyway, and syncing both would
  register the same work twice, once as a story under its epic and once as
  an epic of its own;
- a ticket **already synced** under this project (an Epic row) is
  **re-walked**, not skipped — see below;
- a ticket **already tracked** as a story under some epic, or already
  synced under a *different* project, is left alone: the first is already
  arriving through its own epic, and the second would mean one sweep
  quietly stealing another project's work.

**Re-walking an already-synced root is the point, not an optimisation to
avoid.** Teams add stories to an epic *after* forge first sees it, and the
first version of this module skipped every existing epic — so a filter
matching an epic ingested last week never noticed the story added to it
yesterday, and the only way to pick it up was for a human to remember to
re-type the key into "Add ticket". That is precisely the manual step this
module exists to remove.

Re-walking is safe because `sync_epic_from_jira` is idempotent by
construction: `create_epic` and `create_story` both no-op on an existing
row, so a re-walk can only ever *insert* the children JIRA has and forge
doesn't. Status, kind, gates, worktrees, and PRs on stories forge already
knows about are never touched — a story mid-run stays mid-run while a
sibling appears next to it.

Nothing is ever unsynced, paused, or deleted when a ticket stops matching.
A ticket that was in the filter yesterday and isn't today has usually just
moved status mid-run, and tearing down an epic underneath a running story
to chase a query result is not a trade this platform makes. Removing an
epic stays a deliberate human action.

Newly ingested epics land exactly where "Add ticket" leaves them: paused
(`manual_gate=True`, see StateManager.create_epic) and with no folder
assigned, so the daemon ignores them until a human picks a folder and
resumes. Background ingestion decides *what forge knows about*, never
*what forge starts working on*.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from forge.services.epic_sync import is_jira_subtask, sync_epic_from_jira
from forge.services.jira_service import JiraTicket, issue_key_sort_key
from forge.services.ticket_source import get_ticket_source
from forge.state.manager import StateManager

logger = logging.getLogger("forge.jira_filter")

# Cap on tickets pulled per sweep. Not a settings-UI field, same reasoning
# as jira_service.py's own caps: it is a runaway-query guard, not a knob
# anyone should be tuning. A filter that matches more than this is too
# broad to be ingesting into a single-tenant laptop platform in the first
# place, and the preview shows the truncation.
MAX_FILTER_RESULTS = 100

_DEFAULT_ORDER_BY = "ORDER BY created DESC"
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# JQL compilation.
# ---------------------------------------------------------------------------


def _quote(value: str) -> str:
    """JQL string literal. Backslash first, then the quote — reversing that
    double-escapes every quote."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _in_clause(field: str, values: list[str]) -> str:
    return f"{field} IN ({', '.join(_quote(v) for v in values)})"


def _clean_list(values: list[str] | None) -> list[str]:
    return [v.strip() for v in (values or []) if v and v.strip()]


def _split_order_by(jql: str) -> tuple[str, str]:
    """Separates a user-supplied `extra_jql`'s trailing ORDER BY from its
    conditions. Necessary because the conditions get wrapped in parentheses
    before being AND-ed with the structured clauses, and `(x = 1 ORDER BY
    created)` is not valid JQL — the ordering has to move to the end of the
    whole query instead."""
    match = _ORDER_BY_RE.search(jql)
    if match is None:
        return jql.strip(), ""
    return jql[: match.start()].strip(), jql[match.start() :].strip()


def build_jql(ticket_filter: dict) -> str:
    """Compiles a stored filter into JQL. Raises ValueError when every
    field is empty: an all-empty filter would compile to a bare ORDER BY
    that matches every issue the account can see, which is never what the
    user meant and would ingest an entire JIRA instance."""
    clauses: list[str] = []

    project_key = (ticket_filter.get("jira_project_key") or "").strip()
    if project_key:
        clauses.append(f"project = {_quote(project_key)}")

    assignee = (ticket_filter.get("assignee") or "").strip()
    if assignee:
        # `currentUser()` (and any other JQL function) must go through
        # unquoted or JIRA compares against the literal string.
        rendered = assignee if assignee.endswith("()") else _quote(assignee)
        clauses.append(f"assignee = {rendered}")

    statuses = _clean_list(ticket_filter.get("statuses"))
    if statuses:
        clauses.append(_in_clause("status", statuses))

    labels = _clean_list(ticket_filter.get("labels"))
    if labels:
        clauses.append(_in_clause("labels", labels))

    issue_types = _clean_list(ticket_filter.get("issue_types"))
    if issue_types:
        clauses.append(_in_clause("issuetype", issue_types))

    extra, order_by = _split_order_by(ticket_filter.get("extra_jql") or "")
    if extra:
        # Parenthesised so an OR inside the escape hatch can't quietly
        # widen the whole query past the structured clauses.
        clauses.append(f"({extra})")

    if not clauses:
        raise ValueError(
            "Ticket filter is empty — set at least one of project, assignee, "
            "status, label, issue type, or extra JQL."
        )
    return " AND ".join(clauses) + " " + (order_by or _DEFAULT_ORDER_BY)


# ---------------------------------------------------------------------------
# Classification.
# ---------------------------------------------------------------------------

# What a sweep would do with one matched ticket. Preview and sync run the
# exact same classification, so "what the preview showed" and "what the
# sweep did" cannot drift apart.
ACTION_SYNC = "sync"
# An epic forge already has, re-walked to pick up children added in JIRA
# since it was first ingested. Distinct from ACTION_SYNC purely so the
# preview and the sweep summary can say "3 new, 1 checked for new stories"
# instead of reporting four identical-looking ingests.
ACTION_REFRESH = "refresh"
ACTION_SKIP = "skip"


@dataclass(frozen=True)
class FilterMatch:
    key: str
    summary: str
    issue_type: str
    status: str
    action: str  # ACTION_SYNC | ACTION_REFRESH | ACTION_SKIP
    reason: str

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "summary": self.summary,
            "issue_type": self.issue_type,
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
        }


def evaluate_matches(project_id: int, tickets: list[JiraTicket], sm: StateManager | None = None) -> list[FilterMatch]:
    """Decides, per matched ticket, whether a sweep would ingest it — see
    this module's docstring for why each skip exists. Ordered by issue key
    (JIRA's own search order is arbitrary) so the preview list and the
    ingest order are both stable."""
    sm = sm or StateManager()
    matched_keys = {t.key for t in tickets}
    results: list[FilterMatch] = []

    for ticket in sorted(tickets, key=lambda t: issue_key_sort_key(t.key)):
        action, reason = ACTION_SKIP, ""
        if is_jira_subtask(ticket):
            reason = "sub-task — ingested as context under its parent, never on its own"
        elif ticket.parent_key and ticket.parent_key in matched_keys:
            reason = f"child of {ticket.parent_key}, which this filter also matched"
        else:
            epic = sm.get_epic(ticket.key)
            story = sm.get_story(ticket.key)
            if epic is not None and epic["project_id"] != project_id:
                reason = f"already synced under a different project (project_id={epic['project_id']})"
            elif epic is not None:
                action, reason = ACTION_REFRESH, "already synced — re-checked for new child tickets"
            elif story is not None:
                reason = f"already tracked as a story under {story['epic_key']}"
            else:
                action, reason = ACTION_SYNC, "new"

        results.append(
            FilterMatch(
                key=ticket.key,
                summary=ticket.summary,
                issue_type=ticket.issue_type,
                status=ticket.status,
                action=action,
                reason=reason,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Search / preview / sync.
# ---------------------------------------------------------------------------


def _resolve_filter(project_id: int, ticket_filter: dict | None, sm: StateManager) -> dict:
    """`ticket_filter` is the unsaved one the settings dialog is editing
    (so "Test filter" works before saving); None means use the saved one."""
    if ticket_filter is not None:
        return ticket_filter
    saved = sm.get_project_ticket_filter(project_id)
    if saved is None:
        raise ValueError(f"No ticket filter is configured for project {project_id}")
    return saved


def search_matching_tickets(project_id: int, ticket_filter: dict) -> tuple[str, list[JiraTicket]]:
    source = get_ticket_source(project_id)
    jql = source.compile_filter(ticket_filter)
    tickets = source.search_tickets(jql, max_results=MAX_FILTER_RESULTS)
    logger.info("project %s filter matched %d ticket(s): %s", project_id, len(tickets), jql)
    return jql, tickets


def preview_filter(project_id: int, ticket_filter: dict | None = None) -> dict:
    """Runs the query and classifies the results without writing anything —
    what the settings dialog's "Test filter" calls, so a user can see which
    tickets a filter would pull in (and which it would skip, and why)
    before switching the background sweep on."""
    sm = StateManager()
    resolved = _resolve_filter(project_id, ticket_filter, sm)
    jql, tickets = search_matching_tickets(project_id, resolved)
    matches = evaluate_matches(project_id, tickets, sm)
    return {
        "jql": jql,
        "matched": len(matches),
        "truncated": len(tickets) >= MAX_FILTER_RESULTS,
        "matches": [m.to_dict() for m in matches],
        "would_sync": [m.key for m in matches if m.action == ACTION_SYNC],
        "would_refresh": [m.key for m in matches if m.action == ACTION_REFRESH],
    }


def sync_project_filter(project_id: int, ticket_filter: dict | None = None) -> dict:
    """One sweep: search, classify, and `sync_epic_from_jira` every new
    root. Called both by the settings dialog's "Sync now" and by the
    background loop in forge/daemon/ticket_sync.py.

    Per-ticket failures are collected, not raised — one ticket the account
    can't read (or a root whose key already belongs to another project)
    must not abort the rest of the sweep, and the next sweep retries it
    anyway. A failure of the *search itself* does raise, after being
    recorded on the filter row, because there is no partial result to
    report and the caller (route or loop) needs to surface it.
    """
    sm = StateManager()
    resolved = _resolve_filter(project_id, ticket_filter, sm)

    try:
        jql, tickets = search_matching_tickets(project_id, resolved)
    except Exception as exc:  # noqa: BLE001 — recorded for the UI, then re-raised
        sm.record_ticket_filter_sync(project_id, error=str(exc)[:1000])
        raise

    matches = evaluate_matches(project_id, tickets, sm)
    synced: list[str] = []
    refreshed: list[str] = []
    errors: list[dict] = []

    # Both actions run the same call — sync_epic_from_jira only ever inserts
    # what is missing (see this module's docstring) — and are tracked apart
    # only so the summary distinguishes "forge learned about this epic" from
    # "forge re-checked an epic it already had".
    for match in matches:
        if match.action not in (ACTION_SYNC, ACTION_REFRESH):
            continue
        try:
            before = _story_keys(sm, match.key) if match.action == ACTION_REFRESH else set()
            sync_epic_from_jira(match.key, project_id=project_id)
            if match.action == ACTION_SYNC:
                synced.append(match.key)
                continue
            added = sorted(_story_keys(sm, match.key) - before, key=issue_key_sort_key)
            if added:
                refreshed.append(match.key)
                logger.info("project %s filter: %s picked up %s", project_id, match.key, added)
        except Exception as exc:  # noqa: BLE001 — one bad ticket must not end the sweep
            logger.warning("project %s filter: %s failed to sync: %s", project_id, match.key, exc)
            errors.append({"key": match.key, "error": str(exc)})

    sm.record_ticket_filter_sync(
        project_id,
        matched=len(matches),
        synced=len(synced) + len(refreshed),
        error="; ".join(f"{e['key']}: {e['error']}" for e in errors)[:1000] or None,
    )
    logger.info(
        "project %s filter sweep: %d matched, %d synced, %d refreshed, %d error(s)",
        project_id, len(matches), len(synced), len(refreshed), len(errors),
    )
    return {
        "jql": jql,
        "matched": len(matches),
        "truncated": len(tickets) >= MAX_FILTER_RESULTS,
        "synced": synced,
        "refreshed": refreshed,
        "skipped": [m.to_dict() for m in matches if m.action == ACTION_SKIP],
        "errors": errors,
    }


def _story_keys(sm: StateManager, epic_key: str) -> set[str]:
    """The story keys forge currently holds for an epic. Diffed across a
    re-walk so the sweep can report *which* epics actually gained
    something, rather than listing every epic it re-checked as though
    something had changed."""
    return {s["jira_key"] for s in sm.list_stories_for_epic(epic_key)}
