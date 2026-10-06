"""JiraService creating tickets through the *real* `jira` SDK, against a stdlib HTTP
server that plays JIRA Cloud for the few endpoints involved.

tests/test_jira_create.py fakes the SDK's client, which is how a call the SDK refuses
on Cloud (`createmeta_issuetypes` raises "use createmeta instead") passed every
test. This one lets the SDK make its own requests, so what is checked is what would
actually go over the wire: the issue-type lookup, the v3 create with an ADF
description, and the direction of the "Blocks" link. Local only — no network, no
credentials."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import elstin.services.jira_service as jira_service
from elstin.services.jira_service import JiraService
from elstin.services.ticket_source import TicketWriteError

GUARD = "po_jira_create_enabled"

_TYPES = [
    {"id": "10001", "name": "Epic", "subtask": False, "statuses": []},
    {"id": "10002", "name": "Story", "subtask": False, "statuses": []},
    {"id": "10003", "name": "Task", "subtask": False, "statuses": []},
    {"id": "10004", "name": "Subtask", "subtask": True, "statuses": []},
]


class _FakeJira:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.next_key = 100
        self.create_status = 201
        self.create_body: dict | None = None
        # When set, a create that does not carry this field is refused with JIRA's
        # "Epic Name is required" — what a company-managed project's Epic screen says.
        self.require_epic_name: str | None = None


def _handler(state: _FakeJira):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, status: int, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else None

        def do_GET(self):
            path = self.path.split("?")[0]
            state.requests.append(("GET", path, None))
            if path == "/rest/api/2/serverInfo":
                return self._send(200, {"version": "1001.0.0", "versionNumbers": [1001, 0, 0], "deploymentType": "Cloud", "buildNumber": 1})
            if path == "/rest/api/2/myself":
                return self._send(200, {"accountId": "1", "emailAddress": "me@acme.test"})
            if path == "/rest/api/2/project/PAY/statuses":
                return self._send(200, _TYPES)
            if path == "/rest/api/2/issueLinkType":
                return self._send(200, {"issueLinkTypes": [{"id": "1", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]})
            return self._send(404, {"errorMessages": [f"no such endpoint {path}"]})

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._body()
            state.requests.append(("POST", path, body))
            if path == "/rest/api/3/issue":
                if state.require_epic_name and state.require_epic_name not in (body or {}).get("fields", {}):
                    return self._send(400, {"errorMessages": [], "errors": {state.require_epic_name: "Epic Name is required."}})
                if state.create_status != 201:
                    return self._send(state.create_status, state.create_body or {})
                state.next_key += 1
                return self._send(201, {"id": str(state.next_key), "key": f"PAY-{state.next_key}", "self": f"{path}/{state.next_key}"})
            if path == "/rest/api/2/issueLink":
                return self._send(201, {})
            return self._send(404, {"errorMessages": [f"no such endpoint {path}"]})

    return Handler


@pytest.fixture()
def jira(monkeypatch):
    state = _FakeJira()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    values = {
        "jira_base_url": f"http://127.0.0.1:{server.server_address[1]}", "jira_email": "me@acme.test", "jira_api_token": "token",
        "po_jira_create_enabled": "true",
    }
    monkeypatch.setattr(jira_service, "resolve_config", lambda key, project_id=None: values.get(key))
    monkeypatch.setattr(jira_service, "resolve_bool_config", lambda key, project_id=None: values.get(key) == "true")
    try:
        yield state, JiraService(project_id=1)
    finally:
        server.shutdown()
        server.server_close()


def _posts(state: _FakeJira, path: str) -> list[dict]:
    return [body for method, p, body in state.requests if method == "POST" and p == path and body is not None]


def test_a_story_is_created_over_the_v3_api_with_the_projects_own_issue_type_id_and_an_adf_description(jira):
    state, service = jira
    key = service.create_ticket(
        "PAY", "Story", "Notify the customer", "Why.\n\n## Acceptance Criteria\n- It works", parent_key="PAY-1", labels=["elstin-po-7-s1"],
        guard=GUARD,
    )
    assert key == "PAY-101"
    (sent,) = _posts(state, "/rest/api/3/issue")
    fields = sent["fields"]
    assert fields["project"] == {"key": "PAY"} and fields["issuetype"] == {"id": "10002"}
    assert fields["parent"] == {"key": "PAY-1"} and fields["labels"] == ["elstin-po-7-s1"]
    assert fields["description"]["type"] == "doc" and [b["type"] for b in fields["description"]["content"]] == ["paragraph", "heading", "bulletList"]
    # Reading stays on v2; only the create is on v3, and no v2 create was made.
    assert not _posts(state, "/rest/api/2/issue")


def test_a_sub_task_finds_the_projects_own_spelling_of_the_type(jira):
    state, service = jira
    service.create_ticket("PAY", "Sub-task", "Write it", "d", parent_key="PAY-2", guard=GUARD)
    assert _posts(state, "/rest/api/3/issue")[0]["fields"]["issuetype"] == {"id": "10004"}  # this project says "Subtask"


def test_the_projects_issue_types_are_asked_for_once(jira):
    state, service = jira
    for _ in range(3):
        service.create_ticket("PAY", "Story", "s", "d", guard=GUARD)
    assert [p for m, p, _ in state.requests if p == "/rest/api/2/project/PAY/statuses"] == ["/rest/api/2/project/PAY/statuses"]


def test_a_type_the_project_lacks_is_named_before_anything_is_created(jira):
    state, service = jira
    with pytest.raises(TicketWriteError, match=r"has no issue type 'Bug' \(it offers: Epic, Story, Task, Subtask\)"):
        service.create_ticket("PAY", "Bug", "s", "d", guard=GUARD)
    assert _posts(state, "/rest/api/3/issue") == []


def test_a_link_is_posted_with_the_blocked_ticket_on_the_inward_side(jira):
    state, service = jira
    service.link_tickets("PAY-10", "PAY-11", guard=GUARD)
    (sent,) = _posts(state, "/rest/api/2/issueLink")
    assert sent["type"] == {"name": "Blocks"}
    assert sent["inwardIssue"] == {"key": "PAY-11"} and sent["outwardIssue"] == {"key": "PAY-10"}


def test_jiras_refusal_reaches_the_person_in_jiras_words(jira):
    state, service = jira
    state.create_status = 400
    state.create_body = {"errorMessages": [], "errors": {"customfield_10011": "Epic Name is required"}}
    with pytest.raises(TicketWriteError) as raised:
        service.create_ticket("PAY", "Epic", "Retry payments", "d", guard=GUARD)
    assert "refused to create the Epic 'Retry payments' in PAY" in str(raised.value)
    assert "customfield_10011: Epic Name is required" in str(raised.value)


def test_an_epic_whose_screen_wants_the_legacy_epic_name_is_retried_with_it(jira):
    state, service = jira
    state.require_epic_name = "customfield_10011"
    key = service.create_ticket("PAY", "Epic", "Retry payments", "d", guard=GUARD)
    first, second = _posts(state, "/rest/api/3/issue")
    assert "customfield_10011" not in first["fields"]
    assert second["fields"]["customfield_10011"] == "Retry payments" and key == "PAY-101"


def test_the_epic_name_is_not_sent_unless_jira_asks_for_it(jira):
    state, service = jira
    service.create_ticket("PAY", "Epic", "Retry payments", "d", guard=GUARD)
    (sent,) = _posts(state, "/rest/api/3/issue")
    assert not [name for name in sent["fields"] if name.startswith("customfield_")]


def test_a_story_is_never_retried_with_an_epic_name(jira):
    state, service = jira
    state.require_epic_name = "customfield_10011"
    with pytest.raises(TicketWriteError, match="Epic Name is required"):
        service.create_ticket("PAY", "Story", "s", "d", guard=GUARD)
    assert len(_posts(state, "/rest/api/3/issue")) == 1
