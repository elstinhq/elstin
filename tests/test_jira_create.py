"""Creating tickets through JiraService (elstin/services/jira_service.py): its own
switch, the issue type looked up by name, ADF descriptions over a v3 client,
`parent` and labels, the link's direction. The JIRA SDK is faked at the client —
no network, no DB (the settings are patched)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from jira import JIRAError

import elstin.services.jira_service as jira_service
from elstin.services.jira_service import JiraService
from elstin.services.ticket_source import TicketWriteDisabled, TicketWriteError


class FakeReadClient:
    """The v2 client: reads, issue types, links."""

    def __init__(self, types=(("10001", "Epic"), ("10002", "Story"), ("10003", "Task"), ("10004", "Subtask"))):
        self.types = [SimpleNamespace(id=i, name=n) for i, n in types]
        self.createmeta_calls: list[str] = []
        self.links: list[dict] = []
        self.link_error: Exception | None = None
        self.meta_error: Exception | None = None

    def issue_types_for_project(self, project_key):
        self.createmeta_calls.append(project_key)
        if self.meta_error:
            raise self.meta_error
        return self.types

    def create_issue_link(self, type, inwardIssue, outwardIssue):
        if self.link_error:
            raise self.link_error
        self.links.append({"type": type, "inward": inwardIssue, "outward": outwardIssue})


class FakeWriteClient:
    """The v3 client: where creates go."""

    def __init__(self):
        self.created: list[dict] = []
        self.error: Exception | None = None
        self.no_key = False

    def create_issue(self, fields, prefetch):
        assert prefetch is False
        if self.error:
            raise self.error
        self.created.append(fields)
        return SimpleNamespace(key=None if self.no_key else f"PAY-{100 + len(self.created)}")


GUARD = "po_jira_create_enabled"


def _create(service, *args, **kwargs):
    return service.create_ticket(*args, guard=kwargs.pop("guard", GUARD), **kwargs)


def _link(service, *args, **kwargs):
    return service.link_tickets(*args, guard=kwargs.pop("guard", GUARD), **kwargs)


@pytest.fixture()
def settings(monkeypatch):
    values = {GUARD: "true"}
    monkeypatch.setattr(jira_service, "resolve_bool_config", lambda key, project_id=None: values.get(key, "false") == "true")
    return values


def _service(read=None, write=None) -> JiraService:
    service = JiraService.__new__(JiraService)
    service.project_id = 1
    service._client = read or FakeReadClient()
    service._write_client = write or FakeWriteClient()
    return service


def _jira_error(status=400, body=None):
    response = SimpleNamespace(json=lambda: body or {}, status_code=status, headers={}, text="")
    return JIRAError(status_code=status, text="boom", response=response)


def test_creating_is_off_until_the_callers_switch_is_on(settings):
    settings[GUARD] = "false"
    service = _service()
    with pytest.raises(TicketWriteDisabled, match=GUARD):
        _create(service, "PAY", "Story", "S", "d")
    with pytest.raises(TicketWriteDisabled, match=GUARD):
        _link(service, "PAY-1", "PAY-2")
    assert service._client.links == [] and service._write_client.created == []


def test_the_guard_is_whichever_setting_the_caller_names(settings):
    """The check inside the service is on the switch the caller passes, not on a
    setting the service picks: another caller's switch allows nothing here."""
    settings["some_other_switch"] = "true"
    settings[GUARD] = "false"
    service = _service()
    with pytest.raises(TicketWriteDisabled, match="not_a_switch") as raised:
        _create(service, "PAY", "Story", "S", "d", guard="not_a_switch")
    assert "not_a_switch" in str(raised.value)
    assert _create(service, "PAY", "Story", "S", "d", guard="some_other_switch") == "PAY-101"


def test_the_framework_write_switch_neither_allows_nor_is_needed_for_the_po_writes(monkeypatch):
    """`jira_write_enabled` is the framework's (transitions, QA comments). With it on
    and the PO's own switches off the PO writes are refused; with it off and the PO's
    on they go through."""
    values = {"jira_write_enabled": "true"}
    monkeypatch.setattr(jira_service, "resolve_bool_config", lambda key, project_id=None: values.get(key, "false") == "true")
    service = _service()
    with pytest.raises(TicketWriteDisabled):
        _create(service, "PAY", "Story", "S", "d")
    with pytest.raises(TicketWriteDisabled):
        _link(service, "PAY-1", "PAY-2")
    assert service.add_comment("PAY-1", "hi", guard="po_jira_comment_enabled") is False

    values.clear()
    values.update({"po_jira_create_enabled": "true", "po_jira_comment_enabled": "true"})  # jira_write_enabled now off
    assert _create(service, "PAY", "Story", "S", "d") == "PAY-101"
    _link(service, "PAY-1", "PAY-2")
    service._client.add_comment = lambda key, body: None
    assert service.add_comment("PAY-1", "hi", guard="po_jira_comment_enabled") is True
    # ...and the framework's own comment, which names no guard, still needs the framework's switch
    assert service.add_comment("PAY-1", "QA report") is False


def test_a_ticket_is_created_with_adf_a_parent_labels_and_the_projects_own_issue_type(settings):
    write = FakeWriteClient()
    key = _create(_service(write=write), 
        "PAY", "story", "  Notify the customer  ", "Why.\n\n## Acceptance Criteria\n- It works", parent_key="PAY-1",
        labels=["payments", "needs review", " ", "elstin-po-7-s1"],
    )
    assert key == "PAY-101"
    fields = write.created[0]
    assert fields["project"] == {"key": "PAY"} and fields["issuetype"] == {"id": "10002"}
    assert fields["summary"] == "Notify the customer" and fields["parent"] == {"key": "PAY-1"}
    assert fields["labels"] == ["payments", "needs-review", "elstin-po-7-s1"]  # JIRA labels cannot hold spaces
    description = fields["description"]
    assert description["type"] == "doc" and [b["type"] for b in description["content"]] == ["paragraph", "heading", "bulletList"]


def test_an_epic_has_no_parent_and_no_labels_key_when_there_are_none(settings):
    write = FakeWriteClient()
    _create(_service(write=write), "PAY", "Epic", "E", "d")
    assert "parent" not in write.created[0] and "labels" not in write.created[0]


def test_issue_types_are_matched_ignoring_case_spaces_and_hyphens(settings):
    write = FakeWriteClient()
    service = _service(write=write)
    _create(service, "PAY", "Sub-task", "s", "d", parent_key="PAY-2")
    assert write.created[0]["issuetype"] == {"id": "10004"}  # this project calls it "Subtask"

    spaced = _service(read=FakeReadClient(types=(("1", "Sub Task"),)), write=write)
    _create(spaced, "PAY", "Sub-task", "s", "d")
    assert write.created[1]["issuetype"] == {"id": "1"}


def test_a_type_the_project_does_not_have_is_an_error_naming_the_ones_it_does(settings):
    service = _service(read=FakeReadClient(types=(("1", "Epic"), ("2", "Bug"))))
    with pytest.raises(TicketWriteError, match=r"PAY has no issue type 'Story' \(it offers: Epic, Bug\)"):
        _create(service, "PAY", "Story", "s", "d")
    assert service._write_client.created == []


def test_the_projects_issue_types_are_read_once_per_project(settings):
    read = FakeReadClient()
    service = _service(read=read)
    for _ in range(3):
        _create(service, "PAY", "Story", "s", "d")
    _create(service, "OPS", "Story", "s", "d")
    assert read.createmeta_calls == ["PAY", "OPS"]


def test_jiras_refusal_is_reported_with_what_it_said(settings):
    write = FakeWriteClient()
    write.error = _jira_error(400, {"errorMessages": ["Epic Name is required"], "errors": {"customfield_10011": "Required"}})
    with pytest.raises(TicketWriteError) as raised:
        _create(_service(write=write), "PAY", "Epic", "Retry payments", "d")
    message = str(raised.value)
    assert "refused to create the Epic 'Retry payments' in PAY" in message
    assert "Epic Name is required" in message and "customfield_10011: Required" in message


def test_a_failure_reading_the_issue_types_is_reported(settings):
    read = FakeReadClient()
    read.meta_error = _jira_error(403)
    with pytest.raises(TicketWriteError, match="Could not read which issue types PAY offers"):
        _create(_service(read=read), "PAY", "Story", "s", "d")


def test_a_response_with_no_key_is_an_error(settings):
    write = FakeWriteClient()
    write.no_key = True
    with pytest.raises(TicketWriteError, match="returned no key"):
        _create(_service(write=write), "PAY", "Story", "s", "d")


def test_a_link_makes_the_blocked_ticket_the_one_that_waits(settings):
    """JIRA's "Blocks" link: the issue on the inward side ("is blocked by") waits.
    That is the direction epic_sync.compute_dependencies reads back as a dependency."""
    read = FakeReadClient()
    _link(_service(read=read), "PAY-10", "PAY-11")
    assert read.links == [{"type": "Blocks", "inward": "PAY-11", "outward": "PAY-10"}]


def test_a_refused_link_is_reported(settings):
    read = FakeReadClient()
    read.link_error = _jira_error(404, {"errorMessages": ["No issue with key PAY-99"]})
    with pytest.raises(TicketWriteError, match="refused to link PAY-99 as blocking PAY-11: No issue with key PAY-99"):
        _link(_service(read=read), "PAY-99", "PAY-11")


def test_a_link_reads_back_as_a_dependency():
    """The write above and the read in epic_sync agree on who waits for whom."""
    from elstin.services.epic_sync import compute_dependencies
    from elstin.services.jira_service import JiraTicket

    blocker = JiraTicket(key="PAY-10", summary="", description="", status="To Do", issue_type="Story", priority=None,
                         linked_issues=[{"key": "PAY-11", "link_type": "blocks"}])
    blocked = JiraTicket(key="PAY-11", summary="", description="", status="To Do", issue_type="Story", priority=None,
                         linked_issues=[{"key": "PAY-10", "link_type": "is blocked by"}])
    assert compute_dependencies([blocker, blocked], {"PAY-10": "dev", "PAY-11": "dev"}) == {"PAY-10": [], "PAY-11": ["PAY-10"]}


def test_the_github_source_does_not_create_tickets():
    from elstin.services.github_issues import GitHubIssuesSource

    source = GitHubIssuesSource.__new__(GitHubIssuesSource)
    with pytest.raises(NotImplementedError, match="GitHub Issues"):
        _create(source, "acme/app", "Story", "s", "d")
    with pytest.raises(NotImplementedError):
        _link(source, "a", "b")
