"""A thin wrapper around the `jira` SDK. Every credential/URL/gate read goes
through `get_config("x")` (Postgres), per elstin's no-`.env` rule.
Tuning constants that aren't credentials/URLs/gates (timeouts, result caps)
are plain module constants instead of settings-UI fields — not worth
exposing in the UI.

Consumed directly by elstin/mcp/jira_server.py's MCP tools, and by the
daemon's own framework-owned JIRA reads/writes (status sync, comment
watermarks) — the framework, not agents, owns JIRA writes.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from jira import JIRA, JIRAError

from elstin.config.store import get_config, resolve_bool_config, resolve_config
from elstin.services.adf import markdown_to_adf
from elstin.services.ticket_source import Ticket, TicketWriteDisabled, TicketWriteError

logger = logging.getLogger("elstin.jira_service")

_JIRA_TIMEOUT_SECONDS = 15
_MAX_SYNC_TICKETS = 200
_MAX_REFERENCED_TICKETS = 5


# The ticket shape moved to elstin/services/ticket_source.py so it is not
# JIRA's; the old name stays because every existing caller and test uses it.
JiraTicket = Ticket


class JiraService:
    """Thin wrapper around the ``jira`` SDK. Requires the JIRA base
    URL/email/API token to be set — either globally via the settings UI, or
    as a project-specific override (see elstin.config.store.resolve_config).
    `project_id=None` means global-only, the same behavior as before
    projects existed."""

    name = "jira"
    max_tree_tickets = _MAX_SYNC_TICKETS

    def __init__(self, project_id: int | None = None) -> None:
        self.project_id = project_id
        if not self.is_configured(project_id):
            raise ValueError(
                "JIRA integration is not configured. Set the JIRA base URL, "
                "account email, and API token at /settings."
                + (" or in this project's settings" if project_id is not None else "")
            )
        self._client = JIRA(
            server=resolve_config("jira_base_url", project_id),
            basic_auth=(resolve_config("jira_email", project_id), resolve_config("jira_api_token", project_id)),
            timeout=_JIRA_TIMEOUT_SECONDS,
            max_retries=1,
        )
        self._verify_auth()
        self._epic_link_field_id_cache: str | None = None
        logger.info("JIRA client initialised for %s", resolve_config("jira_base_url", project_id))

    def _verify_auth(self) -> None:
        """Jira Cloud's search endpoints fail *open* on a bad token (200 with
        an empty result), so check /myself up front for a clear error at
        construction time instead of a silent 'no matching tickets' later."""
        try:
            self._client.myself()
        except JIRAError as exc:
            if exc.status_code == 401:
                raise ValueError(
                    f"JIRA authentication failed for {resolve_config('jira_email', self.project_id)} "
                    f"at {resolve_config('jira_base_url', self.project_id)}: the API token was rejected (401). "
                    "Generate a new token and update it at /settings."
                ) from exc
            raise

    @staticmethod
    def is_configured(project_id: int | None = None) -> bool:
        return bool(
            resolve_config("jira_base_url", project_id)
            and resolve_config("jira_email", project_id)
            and resolve_config("jira_api_token", project_id)
        )

    # -- search / fetch -------------------------------------------------------

    def search_tickets(self, jql: str, max_results: int = 50) -> list[JiraTicket]:
        logger.info("Searching JIRA: %s (max %d)", jql, max_results)
        # `labels` is asked for because story_kind's label rules (a `qa`
        # label wins over any prose heuristic) only fire on a ticket that
        # carries them — and every child of an epic reaches elstin through
        # this search, not through get_ticket. The PO publisher also marks
        # the QA stories it creates with that label, so they must read back.
        issues = self._client.search_issues(
            jql,
            maxResults=max_results,
            fields="summary,description,status,issuetype,priority,issuelinks,attachment,parent,labels",
        )
        return [self._parse_issue(issue) for issue in issues]

    def normalize_key(self, raw: str) -> str:
        return (raw or "").strip()

    def compile_filter(self, ticket_filter: dict) -> str:
        """The project's saved filter parts as JQL. Imported here because
        elstin/services/jira_filter.py imports this module."""
        from elstin.services.jira_filter import build_jql

        return build_jql(ticket_filter)

    def is_missing(self, key: str) -> bool:
        try:
            self._client.issue(key, fields="summary")
        except JIRAError as exc:
            if exc.status_code == 404:
                return True
            raise
        return False

    def get_ticket(self, issue_key: str) -> JiraTicket:
        logger.info("Fetching JIRA issue %s", issue_key)
        return self._parse_issue(self._client.issue(issue_key))

    # -- hierarchy --------------------------------------------------------------

    def get_children(self, issue_key: str, *, issue_type: str | None = None) -> list[JiraTicket]:
        try:
            children = self.search_tickets(f'parent = "{issue_key}"', max_results=200)
        except JIRAError as exc:
            logger.warning("get_children(%s) parent-field query failed: %s", issue_key, exc)
            children = []

        if children or (issue_type or "").strip().lower() != "epic":
            return children

        try:
            field_id = self._epic_link_field_id()
            if not field_id:
                return []
            children = self.search_tickets(f'"{field_id}" = "{issue_key}"', max_results=200)
        except JIRAError as exc:
            logger.info("get_children(%s) Epic Link fallback failed: %s", issue_key, exc)
            return []
        return children

    def _epic_link_field_id(self) -> str | None:
        if self._epic_link_field_id_cache is not None:
            return self._epic_link_field_id_cache or None
        try:
            fields = self._client.fields()
        except JIRAError as exc:
            logger.info("Could not list JIRA fields to resolve Epic Link: %s", exc)
            self._epic_link_field_id_cache = ""
            return None
        match = next((f for f in fields if (f.get("name") or "").strip().lower() == "epic link"), None)
        field_id = match["id"] if match else ""
        self._epic_link_field_id_cache = field_id
        return field_id or None

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[JiraTicket]:
        if max_tickets is None:
            max_tickets = _MAX_SYNC_TICKETS

        root = self.get_ticket(root_key)
        tree: list[JiraTicket] = [root]
        visited: set[str] = {root.key}
        queue: list[JiraTicket] = [root]

        while queue and len(tree) < max_tickets:
            parent = queue.pop(0)
            for child in self.get_children(parent.key, issue_type=parent.issue_type):
                if child.key in visited:
                    continue
                visited.add(child.key)
                tree.append(child)
                queue.append(child)
                if len(tree) >= max_tickets:
                    break
        return tree

    # -- transitions ------------------------------------------------------------

    def get_transitions(self, issue_key: str) -> list[dict]:
        raw = self._client.transitions(issue_key)
        return [{"id": t["id"], "name": t["name"], "to_status": t.get("to", {}).get("name", "")} for t in raw]

    def transition_ticket(self, issue_key: str, transition_name: str) -> bool:
        transitions = self.get_transitions(issue_key)
        match = next((t for t in transitions if t["name"].lower() == transition_name.lower()), None)
        if match is None:
            logger.warning(
                "Transition '%s' not found for %s. Available: %s",
                transition_name, issue_key, [t["name"] for t in transitions],
            )
            return False

        if not resolve_bool_config("jira_write_enabled", self.project_id):
            logger.warning("transition_ticket called but jira_write_enabled=false — blocked for %s", issue_key)
            return False

        self._client.transition_issue(issue_key, match["id"])
        logger.info("Transitioned %s via '%s' -> '%s'", issue_key, transition_name, match["to_status"])
        return True

    def add_comment(self, issue_key: str, body: str, *, guard: str | None = None) -> bool:
        """Posts a comment, gated on the same `jira_write_enabled` flag as
        transitions — the QA report is the one agent output that is meant
        to leave elstin and reach the humans on the ticket, so it goes
        through the same write gate as every other outbound JIRA change
        rather than a private path of its own.

        A caller with a switch of its own (the PO agent's suggestions:
        `po_jira_comment_enabled`) names it as `guard` and is gated on that
        alone — it neither needs `jira_write_enabled` nor is allowed by it."""
        guard = guard or "jira_write_enabled"
        if not resolve_bool_config(guard, self.project_id):
            logger.warning("add_comment called but %s=false — blocked for %s", guard, issue_key)
            return False
        self._client.add_comment(issue_key, body)
        logger.info("Commented on %s (%d chars)", issue_key, len(body))
        return True

    # -- creating tickets (the PO publisher) -----------------------------------------

    def create_ticket(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        parent_key: str | None = None,
        labels: Sequence[str] = (),
        *,
        guard: str,
    ) -> str:
        """Create an issue and return its key. `guard` is the setting that allows it
        (the PO publisher's is `po_jira_create_enabled`: per project, off by default,
        and independent of `jira_write_enabled`, which covers transitions and QA
        comments). The caller names it and it is checked here as well, so a caller
        that forgot to check cannot write.

        `description` is markdown, written as ADF — which needs REST API v3, so the
        create goes through a second client (`_write_client`); reads stay on v2. The
        issue type is looked up by name in what `project_key` actually offers
        ("Sub-task" is "Subtask" in some projects) and a type it does not have is an
        error naming the ones it does, never a silent substitute. `parent` is how
        both an epic's story and a story's sub-task are attached; it works for the
        team- and company-managed projects Cloud has."""
        self._require(guard)
        type_id, type_name = self._issue_type_for(project_key, issue_type)
        fields: dict = {
            "project": {"key": project_key},
            "issuetype": {"id": type_id},
            "summary": (summary or "").strip()[:255],
            "description": markdown_to_adf(description),
        }
        clean_labels = [re.sub(r"\s+", "-", str(label).strip()) for label in labels if str(label).strip()]
        if clean_labels:
            fields["labels"] = clean_labels
        if parent_key:
            fields["parent"] = {"key": parent_key}
        try:
            try:
                issue = self._writer().create_issue(fields=fields, prefetch=False)
            except JIRAError as exc:
                # A company-managed project's Epic screen can require the legacy "Epic
                # Name" field, which a team-managed one does not have at all — so it is
                # sent only when JIRA asks for it, under the id it asked for.
                field_id = _required_epic_name_field(exc) if type_name.strip().lower() == "epic" else None
                if field_id is None:
                    raise
                fields[field_id] = fields["summary"]
                issue = self._writer().create_issue(fields=fields, prefetch=False)
        except JIRAError as exc:
            raise TicketWriteError(
                f"JIRA refused to create the {type_name} {summary[:60]!r} in {project_key}: {_jira_error_text(exc)}"
            ) from exc
        key = getattr(issue, "key", None)
        if not key:
            raise TicketWriteError(f"JIRA created the {type_name} {summary[:60]!r} in {project_key} but returned no key.")
        logger.info("Created %s %s in %s (%d chars)", type_name, key, project_key, len(description or ""))
        return str(key)

    def _require(self, guard: str) -> None:
        if not resolve_bool_config(guard, self.project_id):
            raise TicketWriteDisabled(
                f"Creating tickets in JIRA is switched off for this project ({guard}). "
                "Turn it on at /settings or in the project's settings."
            )

    def link_tickets(self, blocker: str, blocked: str, *, guard: str) -> None:
        """`blocker` blocks `blocked`. JIRA's "Blocks" link: the issue on the inward
        side ("is blocked by") is the one that waits — which is the direction
        elstin/services/epic_sync.py's `compute_dependencies` reads back."""
        self._require(guard)
        try:
            self._client.create_issue_link("Blocks", inwardIssue=blocked, outwardIssue=blocker)
        except JIRAError as exc:
            raise TicketWriteError(f"JIRA refused to link {blocker} as blocking {blocked}: {_jira_error_text(exc)}") from exc
        logger.info("Linked %s blocks %s", blocker, blocked)

    def _writer(self):
        """The REST v3 client creates go through, made once. Separate from the
        reading client (v2, which the rest of elstin parses) on purpose."""
        if getattr(self, "_write_client", None) is None:
            self._write_client = JIRA(
                server=resolve_config("jira_base_url", self.project_id),
                basic_auth=(resolve_config("jira_email", self.project_id), resolve_config("jira_api_token", self.project_id)),
                options={"rest_api_version": "3"},
                timeout=_JIRA_TIMEOUT_SECONDS,
                max_retries=1,
                get_server_info=False,
            )
        return self._write_client

    def _issue_type_for(self, project_key: str, wanted: str) -> tuple[str, str]:
        """(id, name) of the issue type `project_key` offers that matches `wanted`
        ignoring case, spaces and hyphens."""
        cache = self.__dict__.setdefault("_issue_types_cache", {})
        if project_key not in cache:
            try:
                # `project/{key}/statuses` lists the project's issue types (with their
                # statuses) on Cloud and Data Center alike. The SDK's
                # createmeta_issuetypes / project_issue_types are the obvious calls and
                # both raise on Cloud ("use createmeta instead").
                cache[project_key] = [(str(t.id), str(t.name)) for t in self._client.issue_types_for_project(project_key)]
            except JIRAError as exc:
                raise TicketWriteError(
                    f"Could not read which issue types {project_key} offers: {_jira_error_text(exc)}"
                ) from exc

        def norm(name: str) -> str:
            return re.sub(r"[\s-]+", "", name or "").lower()

        for type_id, name in cache[project_key]:
            if norm(name) == norm(wanted):
                return type_id, name
        offered = ", ".join(name for _id, name in cache[project_key]) or "none"
        raise TicketWriteError(f"{project_key} has no issue type {wanted!r} (it offers: {offered}).")

    # -- attachments --------------------------------------------------------------

    def download_attachment(self, attachment_id: str, filename: str, ticket_key: str) -> Path:
        dest_dir = Path(get_config("jira_attachments_dir") or "/data/jira-attachments") / ticket_key
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename

        attachment = self._client.attachment(attachment_id)
        dest_path.write_bytes(attachment.get())
        logger.info("Downloaded attachment %s -> %s", attachment_id, dest_path)
        return dest_path

    # -- ADF / text helpers ---------------------------------------------------------

    @staticmethod
    def parse_adf_to_text(adf: dict) -> str:
        def _walk(node: dict) -> str:  # noqa: C901
            node_type = node.get("type", "")
            children = node.get("content", [])

            if node_type == "text":
                return node.get("text", "")
            if node_type == "hardBreak":
                return "\n"
            if node_type == "heading":
                # Keep the `#`s: a ticket's own "## Acceptance Criteria" is how
                # parse_acceptance_criteria finds the criteria, and the text a
                # heading becomes would otherwise be indistinguishable from prose.
                level = max(1, min(int((node.get("attrs") or {}).get("level") or 1), 6))
                return "#" * level + " " + "".join(_walk(c) for c in children) + "\n"
            if node_type == "paragraph":
                return "".join(_walk(c) for c in children) + "\n"
            if node_type in ("bulletList", "orderedList"):
                lines: list[str] = []
                for idx, child in enumerate(children, start=1):
                    item_text = "".join(_walk(c) for c in child.get("content", [])).strip()
                    lines.append(f"{idx}. {item_text}" if node_type == "orderedList" else f"- {item_text}")
                return "\n".join(lines) + "\n"
            if node_type == "listItem":
                return "".join(_walk(c) for c in children)
            if node_type == "codeBlock":
                return f"```\n{''.join(_walk(c) for c in children)}\n```\n"
            return "".join(_walk(c) for c in children)

        if not adf or not isinstance(adf, dict):
            return ""
        return _walk(adf).strip()

    @staticmethod
    def parse_acceptance_criteria(description_text: str) -> list[str]:
        if not description_text:
            return []

        # `## Acceptance Criteria` (markdown, and what parse_adf_to_text writes for an
        # ADF heading) or `h2. Acceptance Criteria` (what JIRA's REST v2 returns for
        # the same heading, as wiki markup — the client here reads over v2).
        heading_re = re.compile(r"^(?:#{1,3}|h[1-3]\.)\s*(acceptance\s+criteria|ac)\b", re.IGNORECASE)
        next_heading_re = re.compile(r"^(?:#{1,3}\s+|h[1-6]\.\s+)")
        bullet_re = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)")

        in_section = False
        criteria: list[str] = []
        for line in description_text.splitlines():
            if not in_section:
                if heading_re.match(line):
                    in_section = True
                continue
            if next_heading_re.match(line):
                break
            m = bullet_re.match(line)
            if m:
                criteria.append(m.group(1).strip())
        return criteria

    # -- internal helpers -----------------------------------------------------------

    def _parse_issue(self, issue) -> JiraTicket:
        fields = issue.fields

        raw_desc = fields.description
        description = self.parse_adf_to_text(raw_desc) if isinstance(raw_desc, dict) else (raw_desc or "")

        linked: list[dict] = []
        for link in getattr(fields, "issuelinks", None) or []:
            if hasattr(link, "inwardIssue") and link.inwardIssue:
                linked.append({
                    "key": link.inwardIssue.key,
                    "summary": link.inwardIssue.fields.summary,
                    "status": str(link.inwardIssue.fields.status),
                    "link_type": link.type.inward,
                })
            if hasattr(link, "outwardIssue") and link.outwardIssue:
                linked.append({
                    "key": link.outwardIssue.key,
                    "summary": link.outwardIssue.fields.summary,
                    "status": str(link.outwardIssue.fields.status),
                    "link_type": link.type.outward,
                })

        image_attachments: list[dict] = []
        for att in getattr(fields, "attachment", None) or []:
            mime = getattr(att, "mimeType", "") or ""
            if mime.startswith("image/"):
                image_attachments.append({
                    "id": att.id,
                    "filename": att.filename,
                    "mime_type": mime,
                    "size": getattr(att, "size", 0),
                    "url": getattr(att, "content", ""),
                })

        acceptance = self.parse_acceptance_criteria(description)
        parent = getattr(fields, "parent", None)
        parent_key = getattr(parent, "key", None) if parent else None

        return JiraTicket(
            key=issue.key,
            summary=fields.summary or "",
            description=description,
            status=str(fields.status),
            issue_type=str(fields.issuetype),
            priority=str(fields.priority) if fields.priority else None,
            parent_key=parent_key,
            acceptance_criteria=acceptance,
            linked_issues=linked,
            attachments=image_attachments,
            labels=list(getattr(fields, "labels", None) or []),
            closed=_status_is_done(fields.status),
        )


def _required_epic_name_field(exc: JIRAError) -> str | None:
    """The custom field id JIRA says is required for an epic's name, from its
    error ({"errors": {"customfield_10011": "Epic Name is required."}}), else None."""
    response = getattr(exc, "response", None)
    try:
        errors = (response.json() if response is not None else {}).get("errors") or {}
    except Exception:  # noqa: BLE001 — not JSON
        return None
    for field_id, message in errors.items():
        if str(field_id).startswith("customfield_") and "epic name" in str(message).lower():
            return str(field_id)
    return None


def _jira_error_text(exc: JIRAError) -> str:
    """What JIRA said, in one line: its own field messages when it gave any."""
    response = getattr(exc, "response", None)
    try:
        body = response.json() if response is not None else {}
    except Exception:  # noqa: BLE001 — not JSON
        body = {}
    parts = [*(body.get("errorMessages") or []), *(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())]
    return "; ".join(parts) or (exc.text or str(exc))[:300]


def _status_is_done(status) -> bool:
    """JIRA groups every workflow's statuses into three categories; "done"
    is the one that means finished regardless of what the team calls it."""
    category = getattr(status, "statusCategory", None)
    return (getattr(category, "key", "") or "").lower() == "done"


# ---------------------------------------------------------------------------
# Referenced-ticket resolution.
# ---------------------------------------------------------------------------

_KEY_SORT_RE = re.compile(r"^(.*?)(\d+)$")


def issue_key_sort_key(key: str) -> tuple[str, int, str]:
    """Natural sort for JIRA keys — PROJ-999 before PROJ-1000, which a plain
    string sort gets backwards.

    Lives here rather than in epic_sync because elstin.state also needs it
    and importing epic_sync from there would be circular (epic_sync imports
    StateManager). JIRA's own search result order is arbitrary, so anything
    showing a list of tickets to a human sorts through this."""
    match = _KEY_SORT_RE.match(key or "")
    return (match.group(1), int(match.group(2)), key) if match else (key or "", 0, key or "")


_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
_NON_JIRA_PREFIXES = frozenset({"UTF", "SHA", "ISO", "RFC", "UTC", "MD5", "IPV4", "IPV6", "BASE64", "OAUTH2", "SHA256"})
_REFERENCED_BODY_CAP = 2000

_ref_service: "JiraService | None" = None


def extract_jira_keys(text: str, *, exclude: frozenset[str] | set[str] = frozenset()) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _JIRA_KEY_RE.finditer(text or ""):
        key = match.group(1)
        if key in exclude or key in seen:
            continue
        if key.split("-", 1)[0] in _NON_JIRA_PREFIXES:
            continue
        seen.add(key)
        out.append(key)
    return out


def _get_ref_service() -> "JiraService":
    global _ref_service
    if _ref_service is None:
        _ref_service = JiraService()
    return _ref_service


def build_referenced_tickets_context(
    text: str,
    *,
    exclude: frozenset[str] | set[str] = frozenset(),
    max_tickets: int | None = None,
    body_cap: int = _REFERENCED_BODY_CAP,
) -> str:
    if not JiraService.is_configured():
        return ""
    keys = extract_jira_keys(text, exclude=exclude)
    if max_tickets is None:
        max_tickets = _MAX_REFERENCED_TICKETS
    keys = keys[: max(max_tickets, 0)]
    if not keys:
        return ""
    try:
        svc = _get_ref_service()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Referenced-ticket resolver: JIRA client unavailable: %s", exc)
        return ""

    blocks: list[str] = []
    for key in keys:
        try:
            ticket = svc.get_ticket(key)
        except Exception as exc:  # noqa: BLE001 — 404 / no access / transport
            logger.info("Referenced ticket %s not fetched (%s); skipping", key, exc)
            continue
        body = (ticket.description or "").strip()
        if len(body) > body_cap:
            body = body[:body_cap] + "\n…[truncated]"
        blocks.append(f"### {ticket.key} ({ticket.status}) — {ticket.summary}\n{body}".strip())

    if not blocks:
        return ""
    return "Referenced JIRA tickets (auto-fetched — use these details in your plan):\n\n" + "\n\n".join(blocks)
