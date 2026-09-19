"""GitHub Issues as a ticket source.

The board's story key ends up in URL paths (`/api/stories/{key}`), branch
names and worktree directories, so it cannot be GitHub's own reference
`owner/repo#12` — the `/` alone would break the route. The local key is
instead `gh.owner.repo.12`, which is reversible: an owner never contains a
dot, a repo may, and the number is the last segment, so `parse_key` splits
on the first and last dot. The GitHub reference is kept as the ticket's
`external_id`. Everything this source accepts or returns as a "key" is the
local form; `normalize_key` is where what a person types (`owner/repo#12`,
an issue URL) becomes one.

Mapping to forge's model:

- **Hierarchy** is GitHub's sub-issues. A root issue with sub-issues is an
  epic whose children are the stories; a sub-issue of a story is a
  "Sub-task" — context for its parent's brief, never dispatched, the same
  as JIRA's. An issue with no sub-issues is a lone story. A root that is
  itself a sub-issue is still a root when it is added directly.
- **Dependencies** are "blocked by" relationships between issues.
- **Closed** is `state == "closed"`.
- **Kind** comes from labels, exactly as for JIRA (`story_kind.py`).

Reads use the search and issues REST endpoints with `github_token`; the
same token forge already uses to open pull requests. Writing back (a comment
on the issue, closing it) is off until `github_issues_write_enabled`.

Never raises for a missing optional field: GitHub's payload has grown fields
over time (`sub_issues_summary`, `parent_issue_url`, dependency summaries)
and a source that also runs against older servers treats absence as
"unknown, ask" rather than "none".
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from forge.config.store import get_config, get_bool_config
from forge.services.ticket_source import Ticket

logger = logging.getLogger("forge.github_issues")

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 20.0
PAGE_SIZE = 100
_MAX_TREE_TICKETS = 200
_MAX_PAGES = 5
KEY_PREFIX = "gh"
# The board's key columns are String(64); a key that would not fit is refused
# with a clear message instead of failing inside the database.
MAX_KEY_LENGTH = 64

_REF = re.compile(r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<repo>[A-Za-z0-9._-]+)#(?P<number>\d+)$")
_URL = re.compile(r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)(?:[/?#].*)?$")
_API_PATH = re.compile(r"/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)(?:/issues/(?P<number>\d+))?$")

_CLOSE_WORDS = frozenset({"close", "closed", "done", "complete", "completed"})
_REOPEN_WORDS = frozenset({"reopen", "open", "reopened"})


class GitHubApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


# ---------------------------------------------------------------- keys


def make_key(owner: str, repo: str, number: int) -> str:
    key = f"{KEY_PREFIX}.{owner}.{repo}.{number}".lower()
    if len(key) > MAX_KEY_LENGTH:
        raise ValueError(
            f"{owner}/{repo}#{number} would need a {len(key)}-character board key; the limit is {MAX_KEY_LENGTH}. "
            "Use a repository with a shorter owner/name."
        )
    return key


def parse_key(key: str) -> tuple[str, str, int]:
    """(owner, repo, number) from a local key. Lower-cased, which GitHub
    treats as the same repository."""
    parts = key.strip().split(".")
    if len(parts) < 4 or parts[0] != KEY_PREFIX or not parts[-1].isdigit():
        raise ValueError(f"{key!r} is not a GitHub issue key (expected gh.<owner>.<repo>.<number>)")
    return parts[1], ".".join(parts[2:-1]), int(parts[-1])


def external_ref(owner: str, repo: str, number: int) -> str:
    return f"{owner}/{repo}#{number}"


def normalize_key(raw: str) -> str:
    """What a person typed → the local key. Accepts `owner/repo#12`, an issue
    URL, or a local key already."""
    text = (raw or "").strip()
    for pattern in (_REF, _URL):
        match = pattern.match(text)
        if match:
            return make_key(match["owner"], match["repo"].removesuffix(".git"), int(match["number"]))
    if text.lower().startswith(f"{KEY_PREFIX}."):
        owner, repo, number = parse_key(text)
        return make_key(owner, repo, number)
    raise ValueError(f"{text!r} is not a GitHub issue — use owner/repo#123 or the issue's URL")


def _from_api_url(url: str) -> tuple[str, str, int | None] | None:
    match = _API_PATH.search(urlparse(url or "").path)
    if not match:
        return None
    return match["owner"], match["repo"], int(match["number"]) if match["number"] else None


# ---------------------------------------------------------------- source


class GitHubIssuesSource:
    name = "github"
    max_tree_tickets = _MAX_TREE_TICKETS

    def __init__(self, project_id: int | None = None, *, _transport: httpx.BaseTransport | None = None) -> None:
        token = get_config("github_token")
        if not token:
            raise ValueError("GitHub integration is not configured. Set the GitHub token at /settings.")
        self.project_id = project_id
        self._client = httpx.Client(
            base_url=API_URL,
            timeout=TIMEOUT_SECONDS,
            transport=_transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "code_forge",
            },
        )

    @staticmethod
    def is_configured(project_id: int | None = None) -> bool:
        return bool(get_config("github_token"))

    def normalize_key(self, raw: str) -> str:
        return normalize_key(raw)

    # -- http --------------------------------------------------------------

    def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (), **kwargs) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400 and response.status_code not in ok:
            message = ""
            try:
                message = str(response.json().get("message") or "")
            except Exception:  # noqa: BLE001 — an error body that is not JSON is still an error
                message = response.text[:200]
            raise GitHubApiError(response.status_code, message or response.reason_phrase)
        return response

    def _list(self, path: str, *, limit: int = _MAX_TREE_TICKETS) -> list[dict]:
        items: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._request("GET", path, params={"per_page": PAGE_SIZE, "page": page}).json()
            items.extend(batch)
            if len(batch) < PAGE_SIZE or len(items) >= limit:
                break
        return items[:limit]

    # -- mapping -----------------------------------------------------------

    def _to_ticket(self, issue: dict, *, issue_type: str | None = None, blocked_by: list[dict] | None = None) -> Ticket:
        location = _from_api_url(issue.get("repository_url") or "")
        if location is None:
            raise GitHubApiError(502, f"issue {issue.get('number')} has no repository_url")
        owner, repo, _ = location
        number = int(issue["number"])

        parent = _from_api_url(issue.get("parent_issue_url") or "")
        parent_key = make_key(parent[0], parent[1], parent[2]) if parent and parent[2] else None

        state = issue.get("state") or "open"
        reason = issue.get("state_reason")
        status = "closed: not planned" if state == "closed" and reason == "not_planned" else state

        linked = []
        for other in blocked_by or []:
            other_location = _from_api_url(other.get("repository_url") or "")
            if other_location is None:
                continue
            linked.append({
                "key": make_key(other_location[0], other_location[1], int(other["number"])),
                "summary": other.get("title") or "",
                "status": other.get("state") or "",
                "link_type": "is blocked by",
            })

        from forge.services.jira_service import JiraService  # acceptance-criteria parsing is source-neutral

        body = issue.get("body") or ""
        type_name = (issue.get("type") or {}).get("name")
        return Ticket(
            key=make_key(owner, repo, number),
            external_id=external_ref(owner, repo, number),
            summary=issue.get("title") or "",
            description=body,
            status=status,
            issue_type=issue_type or type_name or "Issue",
            priority=None,
            parent_key=parent_key,
            acceptance_criteria=JiraService.parse_acceptance_criteria(body),
            linked_issues=linked,
            labels=[str(label["name"]) for label in issue.get("labels") or [] if isinstance(label, dict) and label.get("name")],
            closed=state == "closed",
        )

    def _blocked_by(self, owner: str, repo: str, issue: dict) -> list[dict]:
        summary = issue.get("issue_dependencies_summary")
        if summary is not None and not summary.get("total_blocked_by") and not summary.get("blocked_by"):
            return []  # the payload says there are none — no call needed
        try:
            return self._list(f"/repos/{owner}/{repo}/issues/{issue['number']}/dependencies/blocked_by", limit=50)
        except GitHubApiError as exc:
            if exc.status == 404:  # a server without the dependencies API
                return []
            raise

    # -- reads -------------------------------------------------------------

    def get_ticket(self, key: str) -> Ticket:
        owner, repo, number = parse_key(key)
        issue = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}").json()
        return self._to_ticket(issue, blocked_by=self._blocked_by(owner, repo, issue))

    def get_ticket_tree(self, root_key: str, max_tickets: int | None = None) -> list[Ticket]:
        cap = max_tickets or _MAX_TREE_TICKETS
        owner, repo, number = parse_key(root_key)
        root_issue = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}").json()

        has_children = (root_issue.get("sub_issues_summary") or {}).get("total") != 0
        children = self._sub_issues(owner, repo, number) if has_children else []
        root = self._to_ticket(
            root_issue,
            issue_type="Epic" if children else None,
            blocked_by=self._blocked_by(owner, repo, root_issue),
        )
        tree = [root]
        seen = {root.key}

        for child in children:
            if len(tree) >= cap:
                break
            child_location = _from_api_url(child.get("repository_url") or "")
            if child_location is None or make_key(child_location[0], child_location[1], int(child["number"])) in seen:
                continue
            ticket = self._to_ticket(
                child, blocked_by=self._blocked_by(child_location[0], child_location[1], child),
            )
            ticket.parent_key = root.key
            tree.append(ticket)
            seen.add(ticket.key)

            # A sub-issue of a story is a sub-task: context, never dispatched.
            if (child.get("sub_issues_summary") or {}).get("total") == 0:
                continue
            for grandchild in self._sub_issues(child_location[0], child_location[1], int(child["number"])):
                if len(tree) >= cap:
                    break
                sub = self._to_ticket(grandchild, issue_type="Sub-task")
                if sub.key in seen:
                    continue
                sub.parent_key = ticket.key
                tree.append(sub)
                seen.add(sub.key)
        return tree

    def _sub_issues(self, owner: str, repo: str, number: int) -> list[dict]:
        try:
            return [i for i in self._list(f"/repos/{owner}/{repo}/issues/{number}/sub_issues") if "pull_request" not in i]
        except GitHubApiError as exc:
            if exc.status == 404:  # no sub-issues API — a plain, childless issue
                return []
            raise

    def search_tickets(self, query: str, max_results: int = 50) -> list[Ticket]:
        logger.info("Searching GitHub issues: %s (max %d)", query, max_results)
        payload = self._request(
            "GET", "/search/issues",
            params={"q": query, "per_page": min(max_results, PAGE_SIZE), "sort": "created", "order": "desc"},
        ).json()
        tickets = []
        for issue in payload.get("items", []):
            if "pull_request" in issue:
                continue
            tickets.append(self._to_ticket(issue))
        return tickets[:max_results]

    def compile_filter(self, ticket_filter: dict) -> str:
        """The saved filter's parts as a GitHub issue-search query. The
        "project" field is a repository (`owner/repo`) or an organisation
        (`owner`); "statuses" only understands open and closed."""
        terms = ["is:issue"]
        scoped = False

        project = (ticket_filter.get("jira_project_key") or "").strip()
        if project:
            terms.append(f"repo:{project}" if "/" in project else f"org:{project}")
            scoped = True

        assignee = (ticket_filter.get("assignee") or "").strip()
        if assignee:
            terms.append(f"assignee:{'@me' if assignee.lower() == 'currentuser()' else assignee}")
            scoped = True

        states = {str(s).strip().lower() for s in ticket_filter.get("statuses") or []} & {"open", "closed"}
        if len(states) == 1:
            terms.append(f"is:{next(iter(states))}")
            scoped = True

        labels = [str(label).strip() for label in ticket_filter.get("labels") or [] if str(label).strip()]
        if labels:
            terms.append("label:" + ",".join(f'"{label}"' for label in labels))
            scoped = True

        types = [str(t).strip() for t in ticket_filter.get("issue_types") or [] if str(t).strip()]
        if types:
            terms.append("type:" + ",".join(f'"{t}"' for t in types))
            scoped = True

        extra = (ticket_filter.get("extra_jql") or "").strip()
        if extra:
            terms.append(extra)
            scoped = True

        if not scoped:
            # An unscoped issue search matches every public issue on GitHub.
            raise ValueError("The ticket filter is empty — set at least one of repository, assignee, state, labels or types")
        return " ".join(terms)

    # -- writes ------------------------------------------------------------

    def _writes_allowed(self, key: str, what: str) -> bool:
        if not get_bool_config("github_issues_write_enabled"):
            logger.warning("%s called but github_issues_write_enabled=false — blocked for %s", what, key)
            return False
        return True

    def add_comment(self, key: str, body: str) -> bool:
        if not self._writes_allowed(key, "add_comment"):
            return False
        owner, repo, number = parse_key(key)
        self._request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body})
        logger.info("Commented on %s (%d chars)", key, len(body))
        return True

    def transition_ticket(self, key: str, transition_name: str) -> bool:
        """GitHub has two states, so a "transition" is closing or reopening."""
        word = transition_name.strip().lower()
        if word in _CLOSE_WORDS:
            payload = {"state": "closed", "state_reason": "completed"}
        elif word in _REOPEN_WORDS:
            payload = {"state": "open"}
        else:
            logger.warning("GitHub issues only close or reopen; %r is not a transition", transition_name)
            return False
        if not self._writes_allowed(key, "transition_ticket"):
            return False
        owner, repo, number = parse_key(key)
        self._request("PATCH", f"/repos/{owner}/{repo}/issues/{number}", json=payload)
        return True

    def is_missing(self, key: str) -> bool:
        owner, repo, number = parse_key(key)
        response = self._request("GET", f"/repos/{owner}/{repo}/issues/{number}", ok=(404, 410, 301))
        if response.status_code in (404, 410):
            return True
        return False  # 301: transferred elsewhere — it exists, just not here
