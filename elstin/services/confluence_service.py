"""Read-only access to Confluence, for the PO agent (elstin/agents/roles/
product_owner.py).

A backlog is only as good as what its author knew: the product spec, the
decision record and the design page live in Confluence, not in the repo or in
the epic. This is the one place elstin reads them from. It never writes — there
is no method here that posts, edits or deletes — so unlike the JIRA gates there
is nothing to switch on for safety; `confluence_enabled` exists because what is
read is sent to the model provider.

**No second credential.** Atlassian Cloud's one account and API token cover
JIRA and Confluence, so the connection is the project's JIRA one
(`jira_base_url` + `/wiki`, email, token — each project-overridable through
`resolve_config`). `confluence_base_url` only overrides where the wiki lives
for a deployment that has it elsewhere.

**Scoped by construction.** A project lists its space keys (`confluence_spaces`)
and every search is wrapped in `space in (...)` here, not trusted to the model
to write — whatever CQL the agent composes cannot reach outside them. A page
read by id is held to the same rule, with one exception the caller names: a
page a person linked from the epic. Someone pointed the agent at it on
purpose, and it may live in another space.

HTTP goes through `httpx`, and the client can be handed in, which is how the
tests fake the wire without ever reaching a real Confluence.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from elstin.config.store import resolve_bool_config, resolve_config

logger = logging.getLogger("elstin.confluence_service")

_TIMEOUT_SECONDS = 15
# A page is context for a draft, not something to reproduce: past this the
# tail is cut and the cut is said, so the agent knows the page goes on.
MAX_PAGE_CHARS = 12_000
MAX_SEARCH_RESULTS = 20
_EXCERPT_CHARS = 300

# `/wiki/spaces/ENG/pages/12345/Some+Title`, and the older
# `/wiki/pages/viewpage.action?pageId=12345` / `/wiki/x/AbCd` is not resolvable
# without a request, so is not matched.
_PAGE_URL_PATTERNS = (
    re.compile(r"/spaces/[^/\s]+/pages/(\d+)"),
    re.compile(r"[?&]pageId=(\d+)"),
)


class ConfluenceError(RuntimeError):
    """Confluence refused, or could not be reached. The message is for the
    agent to read: a failed lookup is data it can route around."""


class ConfluenceOutOfScope(ConfluenceError):
    """The page is in a space this project did not list."""


@dataclass(frozen=True)
class PageHit:
    id: str
    title: str
    space: str
    url: str
    excerpt: str


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    space: str
    url: str
    text: str
    truncated: bool


def page_ids_in(text: str | None) -> list[str]:
    """Confluence page ids named by a URL in `text`, first seen first, once each."""
    # Both patterns are scanned, then put in the order the links appear in the
    # text, so "the first link" means the first one whichever form it took.
    found = sorted(
        (match.start(), match.group(1)) for pattern in _PAGE_URL_PATTERNS for match in pattern.finditer(text or "")
    )
    ids: list[str] = []
    for _position, page_id in found:
        if page_id not in ids:
            ids.append(page_id)
    return ids


def parse_spaces(raw: str | None) -> list[str]:
    """`confluence_spaces` as a list of space keys: comma- or whitespace-separated,
    de-duplicated, blanks dropped."""
    keys: list[str] = []
    for token in re.split(r"[,\s]+", raw or ""):
        token = token.strip().strip('"\'')
        if token and token not in keys:
            keys.append(token)
    return keys


def _cql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def scoped_cql(cql: str, spaces: list[str]) -> str:
    """`cql` held to the project's spaces and to pages. An `ORDER BY` moves to
    the end, outside the parentheses the conditions are wrapped in."""
    body, order = cql.strip(), ""
    match = re.search(r"\border\s+by\b", body, re.IGNORECASE)
    if match:
        body, order = body[: match.start()].strip(), " " + body[match.start():].strip()
    clauses = ["type = page"]
    if spaces:
        clauses.append("space in (" + ", ".join(_cql_string(key) for key in spaces) + ")")
    if body:
        clauses.append(f"({body})")
    return " AND ".join(clauses) + order


class _StorageTextParser(HTMLParser):
    """Confluence's storage format (XHTML plus `ac:`/`ri:` macros) as readable
    text: headings keep a `#` so the page's outline survives, list items a `-`,
    table cells are joined with ` | `. Macro wrappers are dropped but what they
    contain is kept — an info panel's text matters, its chrome does not."""

    _BLOCKS = frozenset({"p", "div", "br", "tr", "ul", "ol", "table", "blockquote", "pre", "hr"})
    _SKIP = frozenset({"style", "script", "ri:attachment", "ri:url"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._out.append("\n- ")
        elif tag in ("td", "th"):
            self._out.append(" | ")
        elif tag in self._BLOCKS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._out.append("\n")
        elif tag in ("p", "div", "tr", "pre", "blockquote"):
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        # `| a | b` rows: the leading separator carries nothing.
        lines = [line[1:].strip() if line.startswith("|") else line for line in lines]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def storage_to_text(storage: str | None, *, max_chars: int = MAX_PAGE_CHARS) -> tuple[str, bool]:
    """(text, truncated). The cut is at a line end where there is one close by,
    so a sentence is not left in half."""
    parser = _StorageTextParser()
    try:
        parser.feed(storage or "")
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup is a page we read worse, not a failure
        logger.warning("could not fully parse a Confluence page body", exc_info=True)
    text = parser.text()
    if len(text) <= max_chars:
        return text, False
    cut = text.rfind("\n", max_chars - 400, max_chars)
    return text[: cut if cut > 0 else max_chars].rstrip() + "\n…[page truncated]", True


class ConfluenceService:
    """Search and read, in the project's spaces. Requires the JIRA connection
    and `confluence_enabled`; see `is_configured`."""

    def __init__(self, project_id: int | None = None, *, client: httpx.Client | None = None) -> None:
        self.project_id = project_id
        if not self.is_configured(project_id):
            raise ValueError(
                "Confluence is not enabled for this project: turn on confluence_enabled and set the JIRA connection "
                "(base URL, email, API token) at /settings or in the project's settings."
            )
        self.spaces = parse_spaces(resolve_config("confluence_spaces", project_id))
        self.base_url = self._base_url(project_id)
        self._client = client or httpx.Client(
            auth=(resolve_config("jira_email", project_id) or "", resolve_config("jira_api_token", project_id) or ""),
            timeout=_TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )

    @staticmethod
    def _base_url(project_id: int | None) -> str:
        override = (resolve_config("confluence_base_url", project_id) or "").strip()
        if override:
            return override.rstrip("/")
        return (resolve_config("jira_base_url", project_id) or "").strip().rstrip("/") + "/wiki"

    @staticmethod
    def is_configured(project_id: int | None = None) -> bool:
        return bool(
            resolve_bool_config("confluence_enabled", project_id)
            and resolve_config("jira_base_url", project_id)
            and resolve_config("jira_email", project_id)
            and resolve_config("jira_api_token", project_id)
        )

    def _get(self, path: str, params: dict[str, str | int]) -> dict:
        url = f"{self.base_url}/rest/api/{path}"
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ConfluenceError(f"Confluence could not be reached: {exc}") from exc
        if response.status_code == 401:
            raise ConfluenceError("Confluence rejected the JIRA credentials (401).")
        if response.status_code == 403:
            raise ConfluenceError("The JIRA account may not read that part of Confluence (403).")
        if response.status_code == 404:
            raise ConfluenceError("Not found in Confluence (404).")
        if response.status_code >= 400:
            raise ConfluenceError(f"Confluence answered {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ConfluenceError("Confluence did not answer with JSON.") from exc

    def _page_url(self, links: dict | None, fallback_id: str, space: str) -> str:
        webui = (links or {}).get("webui")
        if webui:
            return f"{self.base_url}{webui}"
        return f"{self.base_url}/spaces/{space}/pages/{fallback_id}"

    def search(self, cql: str, limit: int = 10) -> list[PageHit]:
        """Pages matching `cql`, inside the project's spaces."""
        limit = max(1, min(int(limit or 10), MAX_SEARCH_RESULTS))
        query = scoped_cql(cql, self.spaces)
        logger.info("Searching Confluence: %s (max %d)", query, limit)
        payload = self._get("search", {"cql": query, "limit": limit})
        hits: list[PageHit] = []
        for result in payload.get("results") or []:
            content = result.get("content") or {}
            page_id = str(content.get("id") or "")
            if not page_id:
                continue
            space = ((content.get("space") or {}).get("key")) or (result.get("resultGlobalContainer") or {}).get("title") or ""
            excerpt = re.sub(r"\s+", " ", result.get("excerpt") or "").strip()[:_EXCERPT_CHARS]
            hits.append(PageHit(
                id=page_id,
                title=content.get("title") or result.get("title") or "",
                space=str(space),
                url=self._page_url(content.get("_links") or result.get("_links"), page_id, str(space)),
                excerpt=excerpt,
            ))
        return hits

    def get_page(self, page_id: str, *, allow_ids: Iterable[str] = ()) -> Page:
        """The page as text, capped. A page in a space the project did not list
        is refused unless its id is in `allow_ids` (the pages a person linked
        from the epic)."""
        page_id = str(page_id).strip()
        if not page_id.isdigit():
            raise ConfluenceError(f"{page_id!r} is not a Confluence page id (digits only).")
        data = self._get(f"content/{page_id}", {"expand": "body.storage,space"})
        space = ((data.get("space") or {}).get("key")) or ""
        if self.spaces and space not in self.spaces and page_id not in set(allow_ids):
            raise ConfluenceOutOfScope(
                f"Page {page_id} is in space {space or '?'}, which this project does not list "
                f"({', '.join(self.spaces)})."
            )
        text, truncated = storage_to_text(((data.get("body") or {}).get("storage") or {}).get("value"))
        return Page(
            id=page_id, title=data.get("title") or "", space=space,
            url=self._page_url(data.get("_links"), page_id, space), text=text, truncated=truncated,
        )

    @staticmethod
    def pages_linked_from(text: str | None) -> list[str]:
        """Page ids named by a Confluence URL in `text` (an epic's description)."""
        return page_ids_in(text)
