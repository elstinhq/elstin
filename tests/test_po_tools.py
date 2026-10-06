"""The PO agent's read tools (elstin/tools/po_tools.py) against fake clients —
no JIRA, no Confluence, no DB. Also pins JiraService.search_tickets asking for
`labels`, which every child of an epic depends on to be classified."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from elstin.services.jira_service import JiraService, JiraTicket
from elstin.tools.po_tools import PO_READ_ONLY_TOOLS, PoResearch, cql_for, make_po_tools, scope_jql


def _ticket(key: str, summary: str = "", *, status="To Do", issue_type="Story", parent_key=None, **extra) -> JiraTicket:
    return JiraTicket(
        key=key, summary=summary or f"{key} summary", description=extra.pop("description", ""), status=status,
        issue_type=issue_type, priority=None, parent_key=parent_key, **extra,
    )


@dataclass
class FakeJira:
    name: str = "jira"
    tickets: dict[str, list[JiraTicket]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    error: Exception | None = None

    def search_tickets(self, query, max_results=50):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.tickets.get("search", [])[:max_results]

    def get_ticket_tree(self, root_key, max_tickets=None):
        return self.tickets.get(root_key, [])


class FakeConfluence:
    def __init__(self):
        self.searches: list[tuple[str, int]] = []
        self.allowed: list[set[str]] = []

    def search(self, cql, limit):
        self.searches.append((cql, limit))
        return [SimpleNamespace(id="222", space="ENG", title="Retry design", excerpt="three times")]

    def get_page(self, page_id, *, allow_ids=()):
        self.allowed.append(set(allow_ids))
        if page_id == "404":
            raise RuntimeError("Not found in Confluence (404).")
        return SimpleNamespace(id=page_id, title="Retry design", space="ENG", url="https://wiki/222", text="Retry three times.")


# -- the labels fix ------------------------------------------------------------


def test_jira_search_asks_for_labels_so_a_qa_label_reaches_the_classifier():
    asked: dict = {}

    class Client:
        def search_issues(self, jql, maxResults, fields):
            asked["fields"] = fields
            return []

    service = JiraService.__new__(JiraService)
    service._client = Client()
    service.search_tickets('parent = "EPIC-1"')
    assert "labels" in asked["fields"].split(",")


# -- helpers -------------------------------------------------------------------


def test_jql_is_held_to_the_project_with_order_by_moved_out():
    assert scope_jql('text ~ "retry" ORDER BY created DESC', "PAY") == (
        'project = "PAY" AND (text ~ "retry") ORDER BY created DESC'
    )
    assert scope_jql("", "PAY") == 'project = "PAY"'
    assert scope_jql("statusCategory != Done", None) == "statusCategory != Done"


def test_plain_words_become_a_text_search_and_cql_is_left_alone():
    assert cql_for("payment retry design") == 'text ~ "payment retry design"'
    assert cql_for("retry and backoff") == 'text ~ "retry and backoff"'
    assert cql_for('say "hi"') == 'text ~ "say \\"hi\\""'
    assert cql_for('label = "adr" AND text ~ "payment"') == 'label = "adr" AND text ~ "payment"'
    assert cql_for("space = ENG") == "space = ENG"


# -- what is offered -----------------------------------------------------------


def test_a_source_that_is_not_handed_in_is_not_offered():
    fns, schemas = make_po_tools(research=PoResearch())
    assert fns == {} and schemas == []

    fns, _ = make_po_tools(research=PoResearch(), confluence=FakeConfluence())
    assert set(fns) == {"confluence_search", "confluence_get_page"}

    fns, schemas = make_po_tools(
        research=PoResearch(), ticket_source=FakeJira(), confluence=FakeConfluence(), board_reader=lambda key: [],
    )
    assert set(fns) == PO_READ_ONLY_TOOLS
    assert {s["function"]["name"] for s in schemas} == PO_READ_ONLY_TOOLS
    assert all(s["function"]["parameters"]["additionalProperties"] is False for s in schemas)


def test_the_jira_tools_need_a_jira_source():
    fns, _ = make_po_tools(research=PoResearch(), ticket_source=FakeJira(name="github"))
    assert fns == {}


# -- JIRA ----------------------------------------------------------------------


def test_search_is_scoped_records_what_it_showed_and_reports_failures():
    research = PoResearch()
    jira = FakeJira(tickets={"search": [_ticket("PAY-1", "Retry failed payments", labels=["qa"]), _ticket("PAY-2")]})
    fns, _ = make_po_tools(research=research, ticket_source=jira, jira_project_key="PAY")

    out = fns["jira_search"]('text ~ "retry"')
    assert jira.queries == ['project = "PAY" AND (text ~ "retry")']
    assert "PAY-1 [To Do] (Story) Retry failed payments  labels: qa" in out
    assert research.knows_jira("PAY-1") and research.knows_jira("PAY-2")

    fns["jira_search"]("anything", any_project=True)
    assert jira.queries[-1] == "anything"

    jira.error = RuntimeError("Error in the JQL Query")
    assert fns["jira_search"]("nonsense") == "JIRA search failed: Error in the JQL Query"


def test_search_results_are_capped():
    jira = FakeJira(tickets={"search": [_ticket(f"PAY-{n}") for n in range(60)]})
    fns, _ = make_po_tools(research=PoResearch(), ticket_source=jira)
    assert fns["jira_search"]("x", max_results=500).startswith("25 ticket(s)")


def test_get_issue_shows_the_ticket_and_records_it_for_the_gate():
    research = PoResearch()
    epic = _ticket(
        "PAY-1", "Payment retries", issue_type="Epic", description="Retry failed payments three times.",
        acceptance_criteria=["A declined card is retried"], labels=["payments"],
        linked_issues=[{"key": "PAY-9", "summary": "Ledger", "status": "Done", "link_type": "is blocked by"}],
    )
    child = _ticket("PAY-2", "Backoff", parent_key="PAY-1")
    grandchild = _ticket("PAY-3", "Sub-task", parent_key="PAY-2", issue_type="Sub-task")
    fns, _ = make_po_tools(research=research, ticket_source=FakeJira(tickets={"PAY-1": [epic, child, grandchild]}))

    out = fns["jira_get_issue"]("PAY-1")
    assert "PAY-1 — Payment retries" in out
    assert "Retry failed payments three times." in out
    assert "- A declined card is retried" in out
    assert "is blocked by PAY-9 [Done] Ledger" in out
    assert "Children (1):" in out and "PAY-2 [To Do] (Story) Backoff" in out
    assert "PAY-3" not in out  # a grandchild is the child's, not the epic's
    # What the agent was shown is what an evidence quote is checked against.
    assert "Retry failed payments three times." in research.jira["PAY-1"]
    assert research.knows_jira("PAY-2")


def test_get_issue_reports_a_missing_ticket():
    fns, _ = make_po_tools(research=PoResearch(), ticket_source=FakeJira())
    assert fns["jira_get_issue"]("PAY-404") == "No such ticket: PAY-404"


# -- Confluence ----------------------------------------------------------------


def test_confluence_search_takes_words_or_cql():
    confluence = FakeConfluence()
    fns, _ = make_po_tools(research=PoResearch(), confluence=confluence)
    out = fns["confluence_search"]("payment retry")
    assert confluence.searches == [('text ~ "payment retry"', 10)]
    assert "222 [ENG] Retry design" in out and "three times" in out


def test_get_page_records_the_page_and_lets_linked_pages_through():
    research = PoResearch()
    confluence = FakeConfluence()
    fns, _ = make_po_tools(research=research, confluence=confluence, linked_page_ids={"222"})
    out = fns["confluence_get_page"]("222")
    assert "Retry three times." in out
    assert research.knows_page("222") and "Retry three times." in research.pages["222"]
    assert confluence.allowed == [{"222"}]
    assert fns["confluence_get_page"]("404") == "Could not read page 404: Not found in Confluence (404)."
    assert not research.knows_page("404")


# -- the board -----------------------------------------------------------------


def test_board_status_shows_elstins_view_including_what_jira_does_not():
    rows = [
        {
            "key": "PAY-2", "summary": "Backoff", "status": "merged", "kind": "dev", "depends_on": ["PAY-1"],
            "pull_requests": [{"repo": "api", "url": "https://github.com/acme/api/pull/7", "state": "open"}],
            "person_actions": [{"action": "Run the backfill", "reason": None}],
        },
        {"key": "PAY-3", "summary": "Validate", "status": "skipped", "kind": "qa", "parked_reason": "closed_externally"},
    ]
    fns, _ = make_po_tools(research=PoResearch(), board_reader=lambda key: rows if key == "PAY-1" else [])
    out = fns["board_status"]("PAY-1")
    assert "PAY-2 [merged] kind=dev — Backoff" in out
    assert "waits for PAY-1" in out
    assert "PR https://github.com/acme/api/pull/7 (open)" in out
    assert "left to a person: Run the backfill" in out
    assert "PAY-3 [skipped] kind=qa reason=closed_externally" in out
    assert fns["board_status"]("OTHER-1") == "elstin's board has no stories for OTHER-1."


def test_a_broken_board_reader_is_a_message_not_a_crash():
    def boom(key):
        raise RuntimeError("db down")

    fns, _ = make_po_tools(research=PoResearch(), board_reader=boom)
    assert fns["board_status"]("PAY-1") == "Could not read the board for PAY-1: db down"


def test_the_po_tools_import_nothing_from_services_state_or_config():
    """They are handed their clients (a ticket source, Confluence, a board reader),
    so they test with fakes and the daemon alone decides what the agent may reach."""
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "elstin" / "tools" / "po_tools.py"
    imported = {
        node.module for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom) and node.module
    } | {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Import) for alias in node.names}
    assert not [m for m in imported if m.startswith(("elstin.services", "elstin.state", "elstin.config", "elstin.db", "elstin.daemon"))]
