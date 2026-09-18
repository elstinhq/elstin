"""A thin wrapper around the `jira` SDK. Every credential/URL/gate read goes
through `get_config("x")` (Postgres), per code_forge's no-`.env` rule.
Tuning constants that aren't credentials/URLs/gates (timeouts, result caps)
are plain module constants instead of settings-UI fields — not worth
exposing in the UI.

Consumed directly by forge/mcp/jira_server.py's MCP tools, and by the
daemon's own framework-owned JIRA reads/writes (status sync, comment
watermarks) — the framework, not agents, owns JIRA writes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from jira import JIRA, JIRAError

from forge.config.store import get_config, resolve_bool_config, resolve_config

logger = logging.getLogger("forge.jira_service")

_JIRA_TIMEOUT_SECONDS = 15
_MAX_SYNC_TICKETS = 200
_MAX_REFERENCED_TICKETS = 5


@dataclass
class JiraTicket:
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


class JiraService:
    """Thin wrapper around the ``jira`` SDK. Requires the JIRA base
    URL/email/API token to be set — either globally via the settings UI, or
    as a project-specific override (see forge.config.store.resolve_config).
    `project_id=None` means global-only, the same behavior as before
    projects existed."""

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
        issues = self._client.search_issues(
            jql,
            maxResults=max_results,
            fields="summary,description,status,issuetype,priority,issuelinks,attachment,parent",
        )
        return [self._parse_issue(issue) for issue in issues]

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

    def add_comment(self, issue_key: str, body: str) -> bool:
        """Posts a comment, gated on the same `jira_write_enabled` flag as
        transitions — the QA report is the one agent output that is meant
        to leave forge and reach the humans on the ticket, so it goes
        through the same write gate as every other outbound JIRA change
        rather than a private path of its own."""
        if not resolve_bool_config("jira_write_enabled", self.project_id):
            logger.warning("add_comment called but jira_write_enabled=false — blocked for %s", issue_key)
            return False
        self._client.add_comment(issue_key, body)
        logger.info("Commented on %s (%d chars)", issue_key, len(body))
        return True

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
            if node_type in ("paragraph", "heading"):
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

        heading_re = re.compile(r"^#{1,3}\s*(acceptance\s+criteria|ac)\b", re.IGNORECASE)
        next_heading_re = re.compile(r"^#{1,3}\s+")
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
        )


# ---------------------------------------------------------------------------
# Referenced-ticket resolution.
# ---------------------------------------------------------------------------

_KEY_SORT_RE = re.compile(r"^(.*?)(\d+)$")


def issue_key_sort_key(key: str) -> tuple[str, int, str]:
    """Natural sort for JIRA keys — PROJ-999 before PROJ-1000, which a plain
    string sort gets backwards.

    Lives here rather than in epic_sync because forge.state also needs it
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
