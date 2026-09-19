"""GitHub Issues as a ticket source (forge/services/github_issues.py).

httpx's MockTransport plays GitHub — no token is ever sent anywhere."""
from __future__ import annotations

import re

import httpx
import pytest

import forge.services.epic_sync as epic_sync
import forge.services.jira_filter as jira_filter
from forge.config.store import set_config
from forge.services import github_issues, ticket_source
from forge.services.github_issues import GitHubApiError, GitHubIssuesSource, make_key, normalize_key, parse_key
from forge.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("forge_db")


# ---------------------------------------------------------------- fake GitHub


def _issue(
    number, title="", *, owner="acme", repo="app", state="open", state_reason=None, body="", labels=(),
    parent=None, children=None, blocked_by=None, type_name=None, pull_request=False,
):
    issue = {
        "number": number,
        "title": title or f"Issue {number}",
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "labels": [{"name": name} for name in labels],
        "repository_url": f"https://api.github.com/repos/{owner}/{repo}",
        "parent_issue_url": f"https://api.github.com/repos/{owner}/{repo}/issues/{parent}" if parent else None,
        "type": {"name": type_name} if type_name else None,
    }
    if children is not None:
        issue["sub_issues_summary"] = {"total": len(children), "completed": 0}
    if blocked_by is not None:
        issue["issue_dependencies_summary"] = {"total_blocked_by": len(blocked_by), "blocked_by": len(blocked_by)}
    if pull_request:
        issue["pull_request"] = {"url": "x"}
    return issue


class FakeGitHub:
    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.children: dict[int, list[int]] = {}
        self.blocked: dict[int, list[int]] = {}
        self.search_items: list[dict] = []
        self.status_for: dict[int, int] = {}
        self.requests: list[httpx.Request] = []

    def add(self, issue, *, children=None, blocked_by=None):
        self.issues[issue["number"]] = issue
        if children is not None:
            self.children[issue["number"]] = children
        if blocked_by is not None:
            self.blocked[issue["number"]] = blocked_by

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/search/issues":
            return httpx.Response(200, json={"items": self.search_items})
        match = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/(\d+)(/[a-z_/]+)?", path)
        if not match:
            return httpx.Response(404, json={"message": "Not Found"})
        number, tail = int(match[1]), match[2] or ""
        if number in self.status_for:
            return httpx.Response(self.status_for[number], json={"message": "gone"})
        if number not in self.issues:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "PATCH" and not tail:
            return httpx.Response(200, json=self.issues[number])
        if request.method == "POST" and tail == "/comments":
            return httpx.Response(201, json={"id": 1})
        if tail == "":
            return httpx.Response(200, json=self.issues[number])
        if tail == "/sub_issues":
            return httpx.Response(200, json=[self.issues[c] for c in self.children.get(number, [])])
        if tail == "/dependencies/blocked_by":
            return httpx.Response(200, json=[self.issues[b] for b in self.blocked.get(number, [])])
        return httpx.Response(404, json={"message": "Not Found"})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture()
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    set_config("github_token", "test-token")
    fake = FakeGitHub()
    real_init = GitHubIssuesSource.__init__

    def init(self, project_id=None, *, _transport=None):
        real_init(self, project_id, _transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(GitHubIssuesSource, "__init__", init)
    return fake


def _source(gh: FakeGitHub) -> GitHubIssuesSource:
    return GitHubIssuesSource()


# ---------------------------------------------------------------- keys


@pytest.mark.parametrize("raw", ["acme/app#12", "https://github.com/acme/app/issues/12", "https://github.com/Acme/App/issues/12#issuecomment-1", "gh.acme.app.12"])
def test_every_way_of_naming_an_issue_normalises_to_one_local_key(raw):
    assert normalize_key(raw) == "gh.acme.app.12"


def test_a_key_round_trips_including_a_repo_with_dots():
    key = make_key("acme", "my.app.js", 7)

    assert key == "gh.acme.my.app.js.7"
    assert parse_key(key) == ("acme", "my.app.js", 7)


@pytest.mark.parametrize("raw", ["ENG-123", "acme/app", "https://github.com/acme/app/pull/3", "", "#12"])
def test_something_that_is_not_an_issue_is_refused_with_a_hint(raw):
    with pytest.raises(ValueError, match="owner/repo#123"):
        normalize_key(raw)


def test_a_key_too_long_for_the_board_is_refused_clearly():
    with pytest.raises(ValueError, match="board key"):
        make_key("an-organisation-with-a-long-name", "a-repository-with-an-even-longer-name-than-that", 1234)


def test_the_local_key_is_safe_in_a_url_path_and_a_branch_name():
    key = make_key("Acme", "App", 12)

    assert "/" not in key and "#" not in key and key == key.lower()


# ---------------------------------------------------------------- reads


def test_a_ticket_carries_what_the_board_uses(gh):
    gh.add(
        _issue(
            12, "Add login", body="Intro\n\n## Acceptance criteria\n- can sign in\n- can sign out", labels=("qa", "p1"),
            state="closed", state_reason="not_planned", parent=3, type_name="Feature", children=[], blocked_by=[],
        ),
    )

    ticket = _source(gh).get_ticket("gh.acme.app.12")

    assert (ticket.key, ticket.external_id) == ("gh.acme.app.12", "acme/app#12")
    assert (ticket.summary, ticket.issue_type, ticket.parent_key) == ("Add login", "Feature", "gh.acme.app.3")
    assert ticket.status == "closed: not planned" and ticket.closed is True
    assert ticket.labels == ["qa", "p1"]
    assert ticket.acceptance_criteria == ["can sign in", "can sign out"]


def test_blocked_by_becomes_a_dependency_link_the_level_builder_understands(gh):
    gh.add(_issue(2, children=[], blocked_by=[1]), blocked_by=[1])
    gh.add(_issue(1, children=[], blocked_by=[]))

    ticket = _source(gh).get_ticket("gh.acme.app.2")

    assert ticket.linked_issues == [
        {"key": "gh.acme.app.1", "summary": "Issue 1", "status": "open", "link_type": "is blocked by"}
    ]


def test_dependencies_are_not_requested_when_the_payload_says_there_are_none(gh):
    gh.add(_issue(1, children=[], blocked_by=[]))

    _source(gh).get_ticket("gh.acme.app.1")

    assert not any(p.endswith("/dependencies/blocked_by") for p in gh.paths())


def test_the_tree_is_epic_stories_and_subtasks(gh):
    gh.add(_issue(1, "Epic", children=[2, 3]), children=[2, 3])
    gh.add(_issue(2, "Story A", parent=1, children=[4]), children=[4])
    gh.add(_issue(3, "Story B", parent=1, children=[], blocked_by=[2]), blocked_by=[2])
    gh.add(_issue(4, "Do the thing", parent=2, children=[]))

    tree = {t.key: t for t in _source(gh).get_ticket_tree("gh.acme.app.1")}

    assert list(tree)[0] == "gh.acme.app.1"  # the root first; the rest is depth-first and nothing relies on it
    assert set(tree) == {"gh.acme.app.1", "gh.acme.app.2", "gh.acme.app.3", "gh.acme.app.4"}
    assert tree["gh.acme.app.1"].issue_type == "Epic"
    assert (tree["gh.acme.app.2"].parent_key, tree["gh.acme.app.2"].issue_type) == ("gh.acme.app.1", "Issue")
    assert (tree["gh.acme.app.4"].parent_key, tree["gh.acme.app.4"].issue_type) == ("gh.acme.app.2", "Sub-task")
    assert tree["gh.acme.app.3"].linked_issues[0]["key"] == "gh.acme.app.2"


def test_an_issue_with_no_sub_issues_is_a_lone_story_and_no_children_are_requested(gh):
    gh.add(_issue(1, "Just one", children=[]))

    tree = _source(gh).get_ticket_tree("gh.acme.app.1")

    assert [(t.key, t.issue_type) for t in tree] == [("gh.acme.app.1", "Issue")]
    assert not any(p.endswith("/sub_issues") for p in gh.paths())


def test_sub_issues_in_another_repo_keep_their_own_repo_in_the_key(gh):
    gh.add(_issue(1, children=[9]), children=[9])
    other = _issue(9, owner="acme", repo="api", parent=1, children=[])
    gh.add(other)

    tree = _source(gh).get_ticket_tree("gh.acme.app.1")

    assert [t.key for t in tree] == ["gh.acme.app.1", "gh.acme.api.9"]


def test_the_tree_stops_at_the_size_cap(gh):
    ids = list(range(2, 12))
    gh.add(_issue(1, children=ids), children=ids)
    for n in ids:
        gh.add(_issue(n, parent=1, children=[]))

    tree = _source(gh).get_ticket_tree("gh.acme.app.1", max_tickets=4)

    assert len(tree) == 4


def test_search_skips_pull_requests_and_reads_parents_without_extra_calls(gh):
    gh.search_items = [_issue(5, parent=1), _issue(6, pull_request=True), _issue(7)]

    found = _source(gh).search_tickets("is:issue repo:acme/app", max_results=10)

    assert [(t.key, t.parent_key) for t in found] == [("gh.acme.app.5", "gh.acme.app.1"), ("gh.acme.app.7", None)]
    assert gh.paths() == ["/search/issues"]
    assert gh.requests[0].url.params["q"] == "is:issue repo:acme/app"


def test_an_api_error_carries_status_and_message(gh):
    with pytest.raises(GitHubApiError, match="404: Not Found"):
        _source(gh).get_ticket("gh.acme.app.999")


# ---------------------------------------------------------------- filter


def test_the_filter_compiles_to_a_github_search_query(gh):
    query = _source(gh).compile_filter(
        {"jira_project_key": "acme/app", "assignee": "currentUser()", "statuses": ["Open"], "labels": ["forge", "help wanted"],
         "issue_types": ["Bug"], "extra_jql": "milestone:v2"}
    )

    assert query == 'is:issue repo:acme/app assignee:@me is:open label:"forge","help wanted" type:"Bug" milestone:v2'


def test_a_bare_owner_scopes_to_an_organisation_and_unknown_statuses_are_ignored(gh):
    query = _source(gh).compile_filter({"jira_project_key": "acme", "statuses": ["To Do", "open", "closed"]})

    assert query == "is:issue org:acme"


def test_an_empty_filter_is_refused_rather_than_searching_all_of_github(gh):
    with pytest.raises(ValueError, match="empty"):
        _source(gh).compile_filter({"statuses": ["To Do"], "labels": []})


# ---------------------------------------------------------------- writes


def test_writes_are_blocked_until_enabled(gh):
    gh.add(_issue(1))

    assert _source(gh).add_comment("gh.acme.app.1", "report") is False
    assert _source(gh).transition_ticket("gh.acme.app.1", "close") is False
    assert not any(r.method != "GET" for r in gh.requests)


def test_an_enabled_source_comments_and_closes_or_reopens(gh):
    gh.add(_issue(1))
    set_config("github_issues_write_enabled", "true")
    source = _source(gh)

    assert source.add_comment("gh.acme.app.1", "QA report") is True
    assert source.transition_ticket("gh.acme.app.1", "Done") is True
    assert source.transition_ticket("gh.acme.app.1", "reopen") is True
    assert source.transition_ticket("gh.acme.app.1", "In Review") is False  # GitHub has two states

    writes = [(r.method, r.url.path, r.content) for r in gh.requests if r.method != "GET"]
    assert writes[0][:2] == ("POST", "/repos/acme/app/issues/1/comments") and b"QA report" in writes[0][2]
    assert b'"closed"' in writes[1][2] and b'"completed"' in writes[1][2]
    assert b'"open"' in writes[2][2]


@pytest.mark.parametrize("status,expected", [(404, True), (410, True), (301, False)])
def test_a_missing_issue_is_only_reported_missing_when_github_says_so(gh, status, expected):
    gh.add(_issue(1))
    gh.status_for[1] = status

    assert _source(gh).is_missing("gh.acme.app.1") is expected


def test_an_existing_issue_is_not_missing_and_a_server_error_is_not_an_answer(gh):
    gh.add(_issue(1))
    assert _source(gh).is_missing("gh.acme.app.1") is False

    gh.status_for[1] = 502
    with pytest.raises(GitHubApiError):
        _source(gh).is_missing("gh.acme.app.1")


# ---------------------------------------------------------------- config


def test_without_a_token_the_source_is_unconfigured(monkeypatch: pytest.MonkeyPatch):
    assert GitHubIssuesSource.is_configured() is False
    with pytest.raises(ValueError, match="not configured"):
        GitHubIssuesSource()


# ---------------------------------------------------------------- end to end


@pytest.fixture()
def github_project(gh, monkeypatch: pytest.MonkeyPatch) -> int:
    # These tests ride the same code path the daemon does, so the registry —
    # not a patched module attribute — is what hands out the source.
    sm = StateManager()
    project_id = sm.create_project("Widgets")
    sm.update_project(project_id, ticket_source="github")
    return project_id


def _tree(gh: FakeGitHub) -> None:
    gh.add(_issue(1, "Ship login", children=[2, 3, 5]), children=[2, 3, 5])
    gh.add(_issue(2, "Backend", parent=1, children=[4]), children=[4])
    gh.add(_issue(3, "Frontend", parent=1, children=[], blocked_by=[2]), blocked_by=[2])
    gh.add(_issue(4, "Add the table", parent=2, children=[]))
    gh.add(_issue(5, "Validate the login flow", parent=1, children=[], labels=("qa",)))


def test_a_github_epic_syncs_onto_the_board_with_levels_and_provenance(gh, github_project):
    _tree(gh)

    epic_sync.sync_epic_from_jira("acme/app#1", project_id=github_project)

    sm = StateManager()
    epic = sm.get_epic("gh.acme.app.1")
    assert (epic["external_source"], epic["external_id"], epic["summary"]) == ("github", "acme/app#1", "Ship login")
    # Frontend waits on Backend; the QA-labelled issue is the final level.
    assert epic["levels"] == [["gh.acme.app.2"], ["gh.acme.app.3"], ["gh.acme.app.5"]]
    backend = sm.get_story("gh.acme.app.2")
    assert (backend["external_id"], backend["kind"]) == ("acme/app#2", "dev")
    assert sm.get_story("gh.acme.app.5")["kind"] == "qa"
    sub = sm.get_story("gh.acme.app.4")
    assert (sub["parent_key"], sub["status"], sub["external_id"]) == ("gh.acme.app.2", "skipped", "acme/app#4")


def test_a_lone_github_issue_is_synced_as_the_sole_work_unit(gh, github_project):
    gh.add(_issue(8, "Fix typo", children=[]))

    epic_sync.sync_epic_from_jira("https://github.com/acme/app/issues/8", project_id=github_project)

    assert [s["jira_key"] for s in StateManager().list_stories_for_epic("gh.acme.app.8")] == ["gh.acme.app.8"]


def test_closing_an_issue_on_github_skips_the_unstarted_story_on_the_next_walk(gh, github_project):
    _tree(gh)
    epic_sync.sync_epic_from_jira("acme/app#1", project_id=github_project)

    gh.issues[3]["state"] = "closed"
    report = epic_sync.sync_epic_from_jira("gh.acme.app.1", project_id=github_project)

    story = StateManager().get_story("gh.acme.app.3")
    assert (story["status"], story["external_closed"]) == ("skipped", True)
    assert report.closed == ["gh.acme.app.3"]


def test_an_edited_github_issue_is_held_once_its_story_has_been_planned(gh, github_project):
    _tree(gh)
    epic_sync.sync_epic_from_jira("acme/app#1", project_id=github_project)
    sm = StateManager()
    sm.set_story_status("gh.acme.app.2", "awaiting_plan_approval")
    sm.set_story_thread_id("gh.acme.app.2", "gh.acme.app.2")

    gh.issues[2]["title"] = "Backend (now with rate limiting)"
    epic_sync.sync_epic_from_jira("gh.acme.app.1", project_id=github_project)

    story = sm.get_story("gh.acme.app.2")
    assert story["summary"] == "Backend"
    assert story["external_pending"]["summary"] == "Backend (now with rate limiting)"


def test_the_filter_sweep_ingests_matching_issues_for_a_github_project(gh, github_project):
    _tree(gh)
    gh.search_items = [gh.issues[1]]
    StateManager().set_project_ticket_filter(github_project, enabled=True, jira_project_key="acme/app", labels=["epic"])

    result = jira_filter.sync_project_filter(github_project)

    assert result["synced"] == ["gh.acme.app.1"]
    search = next(r for r in gh.requests if r.url.path == "/search/issues")
    assert search.url.params["q"] == 'is:issue repo:acme/app label:"epic"'
    assert StateManager().get_epic("gh.acme.app.1") is not None


def test_an_issue_that_is_not_a_github_reference_is_rejected_by_the_sync(gh, github_project):
    with pytest.raises(ValueError, match="owner/repo#123"):
        epic_sync.sync_epic_from_jira("ENG-123", project_id=github_project)


def test_the_qa_report_goes_back_to_the_issue_through_the_project_source(gh, github_project):
    set_config("github_issues_write_enabled", "true")
    gh.add(_issue(1))

    assert ticket_source.get_ticket_source(github_project).add_comment("gh.acme.app.1", "report") is True
    assert ticket_source.ticket_source_configured(github_project) is True


def test_a_github_story_key_works_as_a_url_path_segment(gh, github_project):
    """The reason the board key is `gh.owner.repo.12` and not `owner/repo#12`."""
    from fastapi.testclient import TestClient

    from forge.app import create_app

    _tree(gh)
    epic_sync.sync_epic_from_jira("acme/app#1", project_id=github_project)

    with TestClient(create_app()) as client:
        response = client.get("/api/stories/gh.acme.app.2")

    assert response.status_code == 200
    assert response.json()["external_id"] == "acme/app#2"
