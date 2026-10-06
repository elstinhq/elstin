"""Read-only research tools for the PO agent (elstin/agents/roles/product_owner.py)
beyond the codebase tools it shares with the planner: the project's JIRA
tickets, its Confluence pages, and elstin's own view of an epic's stories.

Read-only by construction, like elstin/tools/planner_tools.py: nothing here
writes to JIRA, Confluence or the board. The agent writes nothing at all —
what it proposes comes back as a draft, and elstin's own publisher
(elstin/services/backlog_publisher.py) is the only code that creates a ticket.

**Nothing is imported from `services/`, `state/` or `config/`.** The clients
arrive as arguments (a `TicketSource`, a `ConfluenceService`, a board reader),
so this module is testable with plain fakes and the daemon, which owns the
database and the credentials, decides what the agent may reach. A tool whose
source is not handed in is simply not offered — the model never sees its
schema (elstin/agents/loop.py's note on tool gating).

**`PoResearch` is the other half of the contract.** Every ticket and page the
agent reads is recorded with the text it was shown. The draft must cite its
evidence (`po_gate.py`), and a citation of a JIRA key or a page the agent
never opened is a citation it made up; knowing what was actually seen is what
lets the gate say so. The recorded text is also what an evidence quote is
checked against, verbatim.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# Every tool here only reads, so elstin/agents/loop.py may run a turn's calls
# for them concurrently, alongside the planner's own read-only tools.
JIRA_TOOLS = frozenset({"jira_search", "jira_get_issue"})
CONFLUENCE_TOOLS = frozenset({"confluence_search", "confluence_get_page"})
BOARD_TOOLS = frozenset({"board_status"})
PO_READ_ONLY_TOOLS: frozenset[str] = JIRA_TOOLS | CONFLUENCE_TOOLS | BOARD_TOOLS

_MAX_SEARCH_RESULTS = 25
_DEFAULT_SEARCH_RESULTS = 15
_MAX_DESCRIPTION_CHARS = 6_000
_MAX_CHILDREN = 40
_MAX_BOARD_ROWS = 60

_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)
# What marks a string as CQL rather than the words someone would type into the
# search box: a comparison, or an upper-case boolean / ORDER BY / IN (…). Case
# matters here on purpose — "retry and backoff" is a phrase, not a query.
_CQL_SYNTAX_RE = re.compile(r"(\s!?~\s|[=<>]|\s(?:AND|OR)\s|\bNOT\s|\bORDER\s+BY\b|\bIN\s*\()")


class _TicketLike(Protocol):
    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    parent_key: str | None
    acceptance_criteria: list[str]
    linked_issues: list[dict]
    labels: list[str]


@dataclass
class PoResearch:
    """What the agent has actually read in a session: JIRA keys and Confluence
    page ids, each with the text it was shown.

    Seeded with whatever the brief already put in front of it (the epic's
    tickets, the pages the epic links to) — it read those without calling a
    tool, and refusing a citation of them would punish it for using what it
    was given."""

    jira: dict[str, str] = field(default_factory=dict)
    pages: dict[str, str] = field(default_factory=dict)

    def saw_jira(self, key: str, text: str) -> None:
        # A later, fuller reading replaces a thinner one (a search hit, then the issue).
        if len(text) >= len(self.jira.get(key, "")):
            self.jira[key] = text

    def saw_page(self, page_id: str, text: str) -> None:
        self.pages[str(page_id)] = text

    def knows_jira(self, key: str) -> bool:
        return key in self.jira

    def knows_page(self, page_id: str) -> bool:
        return str(page_id) in self.pages


def scope_jql(jql: str, project_key: str | None) -> str:
    """`jql` held to one JIRA project. The ORDER BY moves outside the parentheses
    the conditions are wrapped in, since `(x = 1 ORDER BY y)` is not valid JQL.
    The same move elstin/services/jira_filter.py makes for a saved filter."""
    body, order = (jql or "").strip(), ""
    match = _ORDER_BY_RE.search(body)
    if match:
        body, order = body[: match.start()].strip(), " " + body[match.start():].strip()
    if not project_key:
        return (body + order).strip()
    scope = f'project = "{project_key}"'
    return (f"{scope} AND ({body})" if body else scope) + order


def looks_like_cql(query: str) -> bool:
    return bool(_CQL_SYNTAX_RE.search(query or ""))


def cql_for(query: str) -> str:
    """What the agent typed as CQL. Plain words become a text search: a model
    that was told "CQL" still often sends `payment retry design`."""
    query = (query or "").strip()
    if not query or looks_like_cql(query):
        return query
    return 'text ~ "' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…[truncated]"


def render_ticket(ticket: _TicketLike, children: list[_TicketLike] | None) -> str:
    lines = [
        f"{ticket.key} — {ticket.summary}",
        f"Type: {ticket.issue_type}   Status: {ticket.status}"
        + (f"   Parent: {ticket.parent_key}" if ticket.parent_key else "")
        + (f"   Labels: {', '.join(ticket.labels)}" if ticket.labels else ""),
    ]
    if ticket.description:
        lines += ["", "Description:", _clip(ticket.description.strip(), _MAX_DESCRIPTION_CHARS)]
    if ticket.acceptance_criteria:
        lines += ["", "Acceptance criteria (parsed from the description):"] + [f"- {ac}" for ac in ticket.acceptance_criteria]
    if ticket.linked_issues:
        lines += ["", "Links:"] + [
            f"- {link.get('link_type', 'relates to')} {link.get('key')} [{link.get('status', '?')}] {link.get('summary', '')}".rstrip()
            for link in ticket.linked_issues
        ]
    if children:
        lines += ["", f"Children ({len(children)}):"] + [
            f"- {child.key} [{child.status}] ({child.issue_type}) {child.summary}" for child in children[:_MAX_CHILDREN]
        ]
        if len(children) > _MAX_CHILDREN:
            lines.append(f"- … {len(children) - _MAX_CHILDREN} more")
    return "\n".join(lines)


def render_board(epic_key: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"elstin's board has no stories for {epic_key}."
    lines = [f"elstin's board for {epic_key} ({len(rows)} stories):"]
    for row in rows[:_MAX_BOARD_ROWS]:
        bits = [f"{row.get('key')} [{row.get('status')}]"]
        if row.get("jira_status"):
            bits.append(f"(JIRA: {row['jira_status']})")
        if row.get("kind"):
            bits.append(f"kind={row['kind']}")
        if row.get("parked_reason"):
            bits.append(f"reason={row['parked_reason']}")
        line = " ".join(bits) + f" — {row.get('summary') or ''}"
        extras: list[str] = []
        if row.get("depends_on"):
            extras.append("waits for " + ", ".join(row["depends_on"]))
        for pr in row.get("pull_requests") or []:
            extras.append(f"PR {pr.get('url') or pr.get('repo') or '?'} ({pr.get('state') or '?'})")
        for action in row.get("person_actions") or []:
            text = action.get("action") if isinstance(action, dict) else str(action)
            extras.append(f"left to a person: {text}")
        lines.append(line.rstrip() + ("".join(f"\n    {extra}" for extra in extras)))
    if len(rows) > _MAX_BOARD_ROWS:
        lines.append(f"… {len(rows) - _MAX_BOARD_ROWS} more")
    return "\n".join(lines)


def make_po_tools(
    *,
    research: PoResearch,
    ticket_source: Any | None = None,
    jira_project_key: str | None = None,
    confluence: Any | None = None,
    linked_page_ids: frozenset[str] | set[str] = frozenset(),
    board_reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Callable[..., str]], list[dict[str, Any]]]:
    """(functions, schemas), the same pair shape make_planner_tools returns, for
    whichever of JIRA, Confluence and the board were handed in.

    `ticket_source` is only used when it is JIRA — the tools speak JQL — and
    `jira_project_key` is ANDed into every search so the agent reads the
    project's own backlog, not the whole instance's. `linked_page_ids` are the
    pages a person linked from the epic, readable whatever space they are in."""
    functions: dict[str, Callable[..., str]] = {}
    schemas: list[dict[str, Any]] = []

    def _add(name: str, fn: Callable[..., str], description: str, properties: dict[str, Any], required: list[str]) -> None:
        functions[name] = fn
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object", "properties": properties, "required": required, "additionalProperties": False,
                },
            },
        })

    if ticket_source is not None and getattr(ticket_source, "name", "") == "jira":
        def jira_search(jql: str, max_results: int = _DEFAULT_SEARCH_RESULTS, any_project: bool = False) -> str:
            cap = max(1, min(int(max_results or _DEFAULT_SEARCH_RESULTS), _MAX_SEARCH_RESULTS))
            query = scope_jql(jql, None if any_project else jira_project_key)
            try:
                tickets = ticket_source.search_tickets(query, max_results=cap)
            except Exception as exc:  # noqa: BLE001 — a bad JQL is data the model can fix, not a crash
                return f"JIRA search failed: {exc}"
            if not tickets:
                return f"No tickets match: {query}"
            lines = []
            for ticket in tickets[:cap]:
                research.saw_jira(ticket.key, f"{ticket.key} {ticket.summary}")
                lines.append(
                    f"{ticket.key} [{ticket.status}] ({ticket.issue_type}) {ticket.summary}"
                    + (f"  labels: {', '.join(ticket.labels)}" if ticket.labels else "")
                )
            return f"{len(lines)} ticket(s) for: {query}\n" + "\n".join(lines)

        def jira_get_issue(key: str) -> str:
            key = (key or "").strip()
            try:
                tree = ticket_source.get_ticket_tree(key, max_tickets=_MAX_CHILDREN + 1)
            except Exception as exc:  # noqa: BLE001
                return f"Could not read {key}: {exc}"
            if not tree:
                return f"No such ticket: {key}"
            ticket, descendants = tree[0], tree[1:]
            # The same rule as elstin/services/epic_sync.py's _split_by_depth: a
            # child is one whose parent is this ticket, or that reports none (a
            # project that links stories to an epic through the legacy Epic Link).
            children = [t for t in descendants if t.parent_key in (None, ticket.key)]
            rendered = render_ticket(ticket, children)
            research.saw_jira(ticket.key, rendered)
            for child in children:
                research.saw_jira(child.key, f"{child.key} {child.summary}")
            return rendered

        _add(
            "jira_search", jira_search,
            "Search the project's JIRA tickets with JQL. Returns one line per ticket: key, status, type, summary. "
            + (f"Searches are limited to project {jira_project_key} unless any_project is true. " if jira_project_key else "")
            + f"At most {_MAX_SEARCH_RESULTS} results. Use it to find tickets that already cover something before "
            "proposing it, or to see what a feature's history looks like. Read a ticket in full with jira_get_issue.",
            {
                "jql": {"type": "string", "description": 'JQL, e.g. text ~ "retry" AND statusCategory != Done'},
                "max_results": {"type": "integer", "description": f"Most tickets to return (capped at {_MAX_SEARCH_RESULTS})"},
                "any_project": {"type": "boolean", "description": "Search every JIRA project, not just this one. Default false."},
            },
            ["jql"],
        )
        _add(
            "jira_get_issue", jira_get_issue,
            "Read one JIRA ticket in full: summary, type, status, labels, parent, description, acceptance criteria, "
            "links and children. Cite it as evidence only after reading it here.",
            {"key": {"type": "string", "description": "The ticket key, e.g. PROJ-123"}},
            ["key"],
        )

    if confluence is not None:
        def confluence_search(query: str, limit: int = 10) -> str:
            cql = cql_for(query)
            try:
                hits = confluence.search(cql, limit)
            except Exception as exc:  # noqa: BLE001
                return f"Confluence search failed: {exc}"
            if not hits:
                return f"No pages match: {cql}"
            return f"{len(hits)} page(s) for: {cql}\n" + "\n".join(
                f"{hit.id} [{hit.space}] {hit.title}" + (f"\n    {hit.excerpt}" if hit.excerpt else "") for hit in hits
            )

        def confluence_get_page(page_id: str) -> str:
            try:
                page = confluence.get_page(page_id, allow_ids=linked_page_ids)
            except Exception as exc:  # noqa: BLE001
                return f"Could not read page {page_id}: {exc}"
            rendered = f"{page.title} [{page.space}] (page {page.id})\n{page.url}\n\n{page.text}"
            research.saw_page(page.id, rendered)
            return rendered

        _add(
            "confluence_search", confluence_search,
            "Search the project's Confluence pages. Takes CQL (e.g. text ~ \"payment\" AND label = \"adr\") or plain "
            "words, which become a text search. Returns page ids, titles and a short excerpt; read one with "
            "confluence_get_page. Searches are held to the spaces this project lists.",
            {
                "query": {"type": "string", "description": "CQL, or plain words to search for"},
                "limit": {"type": "integer", "description": "Most pages to return (default 10)"},
            },
            ["query"],
        )
        _add(
            "confluence_get_page", confluence_get_page,
            "Read one Confluence page as text. Cite a page as evidence only after reading it here.",
            {"page_id": {"type": "string", "description": "The page id from a search result or a /pages/<id> link"}},
            ["page_id"],
        )

    if board_reader is not None:
        def board_status(epic_key: str) -> str:
            try:
                rows = board_reader((epic_key or "").strip())
            except Exception as exc:  # noqa: BLE001
                return f"Could not read the board for {epic_key}: {exc}"
            return render_board(epic_key, rows)

        _add(
            "board_status", board_status,
            "elstin's own view of an epic's stories: each one's status on elstin's board (pending, merged, failed…), its "
            "kind, what it waits for, the pull request it landed in and what was left to a person. It can differ from "
            "JIRA — merged in elstin but still To Do there — and that difference is a finding.",
            {"epic_key": {"type": "string", "description": "The epic's key"}},
            ["epic_key"],
        )

    return functions, schemas
