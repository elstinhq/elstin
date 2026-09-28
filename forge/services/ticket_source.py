"""Where tickets come from: one interface, one neutral ticket shape, and a
per-project choice of source.

forge's own Postgres board (epics and stories) is what the agents read.
JIRA — and next GitHub Issues — is where a ticket's *content* is authored.
This module is the seam between the two: sync code asks `get_ticket_source`
for the project's source and speaks only `Ticket` and `TicketSource`, so
supporting another system means writing one class that satisfies the
protocol and registering it here, not touching the sync code.

Deliberately small: only what the callers use today. A method added here
without a caller is a promise nothing checks.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("forge.ticket_source")

DEFAULT_SOURCE = "jira"


@dataclass
class Ticket:
    """A ticket in whichever system it lives in, reduced to what forge uses."""

    key: str
    summary: str
    description: str
    status: str
    issue_type: str
    priority: str | None
    parent_key: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    linked_issues: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    # Carried through purely so story-kind classification has an explicit,
    # human-set signal to prefer over prose heuristics — see
    # forge/services/story_kind.py.
    labels: list[str] = field(default_factory=list)
    # The source considers the ticket finished (JIRA: status category
    # "done"; GitHub: closed). Sync stops dispatching a closed ticket.
    closed: bool = False
    # The ticket's id in its source when that is not `key` — a GitHub issue's
    # key on the board is path-safe (`gh.owner.repo.12`) while its id is
    # `owner/repo#12`. None means the key is the id (JIRA).
    external_id: str | None = None


def content_hash(summary: str, description: str | None) -> str:
    """Digest of what a ticket *says* — the two fields forge copies onto the
    board. Whitespace at the ends is ignored so a trailing newline the source
    added does not read as an edit."""
    payload = json.dumps([(summary or "").strip(), (description or "").strip()])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TicketSource(Protocol):
    """A system tickets are read from and (optionally) reported back to."""

    name: str

    def search_tickets(self, query: str, max_results: int = 50) -> list[Ticket]:
        """`query` is in the source's own query language — see `compile_filter`."""

    def normalize_key(self, raw: str) -> str:
        """What a person typed to add a ticket → the key the board uses.
        Raises ValueError for something that is not a ticket of this source."""

    def get_ticket(self, key: str) -> Ticket: ...

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[Ticket]:
        """The root and every descendant, root first."""

    def compile_filter(self, ticket_filter: dict) -> str:
        """Turns a project's saved filter (its parts, not a query) into the
        source's own query language."""

    def add_comment(self, key: str, body: str) -> bool:
        """Post a comment; False when the source is read-only or the write failed."""

    def transition_ticket(self, key: str, transition_name: str) -> bool: ...

    def is_missing(self, key: str) -> bool:
        """True only when the source positively says the ticket does not
        exist (or is no longer visible to us). A transient failure must
        raise, not return True — a wrong "missing" skips real work."""

    # Optional: the most tickets get_ticket_tree returns. A tree that hits it
    # may be truncated, so sync must not read absent tickets as gone.
    max_tree_tickets: int | None


@dataclass(frozen=True)
class SourceRegistration:
    factory: Callable[[int | None], TicketSource]
    is_configured: Callable[[int | None], bool]


_REGISTRY: dict[str, SourceRegistration] = {}
_DEFAULTS_REGISTERED = False


def register_source(name: str, factory: Callable[[int | None], TicketSource], is_configured: Callable[[int | None], bool]) -> None:
    _REGISTRY[name] = SourceRegistration(factory=factory, is_configured=is_configured)


def _ensure_default_sources_registered() -> None:
    """Lazy for the same reason as language_hooks: the JIRA client imports
    `Ticket` from this module, so importing it back at module load would be
    circular."""
    global _DEFAULTS_REGISTERED
    if _DEFAULTS_REGISTERED:
        return
    _DEFAULTS_REGISTERED = True

    from forge.services.github_issues import GitHubIssuesSource
    from forge.services.jira_service import JiraService

    register_source("jira", lambda project_id: JiraService(project_id=project_id), JiraService.is_configured)
    register_source("github", lambda project_id: GitHubIssuesSource(project_id), GitHubIssuesSource.is_configured)


def known_sources() -> list[str]:
    _ensure_default_sources_registered()
    return sorted(_REGISTRY)


def project_source_name(project_id: int | None) -> str:
    """The source a project pulls from; JIRA when there is no project or the
    project predates the setting."""
    if project_id is None:
        return DEFAULT_SOURCE
    from forge.state.manager import StateManager

    return StateManager().get_project_ticket_source(project_id) or DEFAULT_SOURCE


def _registration(project_id: int | None) -> tuple[str, SourceRegistration]:
    _ensure_default_sources_registered()
    name = project_source_name(project_id)
    registration = _REGISTRY.get(name)
    if registration is None:
        raise ValueError(
            f"Ticket source {name!r} is not available (known: {', '.join(sorted(_REGISTRY))}). "
            f"Project {project_id} is set to use it."
        )
    return name, registration


def get_ticket_source(project_id: int | None) -> TicketSource:
    return _registration(project_id)[1].factory(project_id)


def ticket_source_configured(project_id: int | None) -> bool:
    """False for an unknown source as well as for missing credentials — the
    callers that use this (best-effort write-back) treat both as "skip"."""
    try:
        return _registration(project_id)[1].is_configured(project_id)
    except ValueError:
        return False
