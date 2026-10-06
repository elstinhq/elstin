"""Confluence reads (elstin/services/confluence_service.py). The wire is faked
with httpx's MockTransport — no real Confluence, and no DB: the settings are
read through `resolve_config`, which these tests patch.
"""
from __future__ import annotations

import httpx
import pytest

import elstin.services.confluence_service as confluence_service
from elstin.services.confluence_service import (
    ConfluenceError, ConfluenceOutOfScope, ConfluenceService, page_ids_in, parse_spaces, scoped_cql, storage_to_text,
)


@pytest.fixture()
def settings(monkeypatch):
    values = {
        "confluence_enabled": "true",
        "confluence_spaces": "ENG, PROD",
        "confluence_base_url": None,
        "jira_base_url": "https://acme.atlassian.net/",
        "jira_email": "me@acme.test",
        "jira_api_token": "token",
    }
    monkeypatch.setattr(confluence_service, "resolve_config", lambda key, project_id=None: values.get(key))
    monkeypatch.setattr(
        confluence_service, "resolve_bool_config",
        lambda key, project_id=None: (values.get(key) or "").lower() == "true",
    )
    return values


def _service(handler) -> ConfluenceService:
    return ConfluenceService(client=httpx.Client(transport=httpx.MockTransport(handler)))


# -- pure helpers --------------------------------------------------------------


def test_page_ids_are_found_in_order_whichever_link_form():
    text = (
        "Design: https://acme.atlassian.net/wiki/spaces/ENG/pages/222/Retry+design and the older "
        "https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=111 plus the first again "
        "https://acme.atlassian.net/wiki/spaces/ENG/pages/222"
    )
    assert page_ids_in(text) == ["222", "111"]
    assert page_ids_in(None) == []
    assert page_ids_in("no links, only the number 12345") == []


def test_spaces_are_split_and_deduplicated():
    assert parse_spaces(" ENG, PROD ,ENG\nOPS ") == ["ENG", "PROD", "OPS"]
    assert parse_spaces(None) == []
    assert parse_spaces('"ENG",') == ["ENG"]


def test_cql_is_held_to_pages_and_the_projects_spaces():
    assert scoped_cql('text ~ "retry"', ["ENG", "PROD"]) == (
        'type = page AND space in ("ENG", "PROD") AND (text ~ "retry")'
    )
    # An ORDER BY cannot live inside the parentheses the condition is wrapped in.
    assert scoped_cql('text ~ "x" ORDER BY lastmodified DESC', ["ENG"]) == (
        'type = page AND space in ("ENG") AND (text ~ "x") ORDER BY lastmodified DESC'
    )
    assert scoped_cql("", []) == "type = page"


def test_agent_cql_cannot_widen_the_spaces():
    # The agent's own `space = SECRET` is ANDed with the project's list, never OR'd in.
    cql = scoped_cql('space = "SECRET" OR text ~ "x"', ["ENG"])
    assert cql.startswith('type = page AND space in ("ENG") AND (')
    assert cql.count("space in") == 1


def test_storage_format_becomes_readable_text():
    storage = (
        "<h1>Retry design</h1><p>Failed payments are <strong>retried</strong> three times.</p>"
        "<ul><li>Backoff: 1s, 5s, 30s</li><li>Give up after 3</li></ul>"
        '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>Owned by payments</p></ac:rich-text-body>'
        "</ac:structured-macro><table><tr><th>Code</th><th>Meaning</th></tr><tr><td>402</td><td>Declined</td></tr></table>"
    )
    text, truncated = storage_to_text(storage)
    assert not truncated
    assert "# Retry design" in text
    assert "Failed payments are retried three times." in text
    assert "- Backoff: 1s, 5s, 30s" in text
    assert "Owned by payments" in text
    assert "402 | Declined" in text
    assert "<" not in text


def test_a_long_page_is_cut_and_says_so():
    text, truncated = storage_to_text("<p>" + "word " * 5_000 + "</p>", max_chars=1_000)
    assert truncated
    assert text.endswith("…[page truncated]")
    assert len(text) < 1_100


# -- the service ---------------------------------------------------------------


def test_not_configured_without_the_switch_or_the_jira_connection(settings):
    assert ConfluenceService.is_configured(1) is True
    settings["confluence_enabled"] = "false"
    assert ConfluenceService.is_configured(1) is False
    settings["confluence_enabled"] = "true"
    settings["jira_api_token"] = None
    assert ConfluenceService.is_configured(1) is False
    with pytest.raises(ValueError, match="not enabled"):
        ConfluenceService(1)


def test_the_wiki_lives_under_the_jira_url_unless_overridden(settings):
    assert _service(lambda request: httpx.Response(200, json={})).base_url == "https://acme.atlassian.net/wiki"
    settings["confluence_base_url"] = "https://wiki.acme.test/confluence/"
    assert _service(lambda request: httpx.Response(200, json={})).base_url == "https://wiki.acme.test/confluence"


def test_search_asks_for_the_scoped_query_and_reads_the_hits(settings):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [
            {
                "content": {"id": "222", "title": "Retry design", "space": {"key": "ENG"}, "_links": {"webui": "/spaces/ENG/pages/222"}},
                "excerpt": "Failed payments are   retried\nthree times",
            },
            {"content": {}, "title": "not a page"},
        ]})

    hits = _service(handler).search('text ~ "retry"', limit=500)
    request = seen[0]
    assert request.url.path == "/wiki/rest/api/search"
    assert request.url.params["cql"] == 'type = page AND space in ("ENG", "PROD") AND (text ~ "retry")'
    assert request.url.params["limit"] == "20"  # capped
    assert len(hits) == 1
    assert (hits[0].id, hits[0].title, hits[0].space) == ("222", "Retry design", "ENG")
    assert hits[0].url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/222"
    assert hits[0].excerpt == "Failed payments are retried three times"


def test_get_page_returns_text(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/rest/api/content/222"
        assert request.url.params["expand"] == "body.storage,space"
        return httpx.Response(200, json={
            "id": "222", "title": "Retry design", "space": {"key": "ENG"},
            "body": {"storage": {"value": "<p>Retry three times.</p>"}}, "_links": {"webui": "/spaces/ENG/pages/222"},
        })

    page = _service(handler).get_page("222")
    assert page.text == "Retry three times."
    assert (page.title, page.space, page.truncated) == ("Retry design", "ENG", False)


def _page_in(space: str):
    return lambda request: httpx.Response(200, json={
        "id": "9", "title": "Elsewhere", "space": {"key": space}, "body": {"storage": {"value": "<p>x</p>"}},
    })


def test_a_page_outside_the_projects_spaces_is_refused_unless_the_epic_linked_it(settings):
    service = _service(_page_in("HR"))
    with pytest.raises(ConfluenceOutOfScope, match="HR"):
        service.get_page("9")
    assert service.get_page("9", allow_ids={"9"}).space == "HR"


def test_no_spaces_listed_means_every_space(settings):
    settings["confluence_spaces"] = ""
    assert _service(_page_in("HR")).get_page("9").space == "HR"


def test_a_page_id_must_be_digits(settings):
    with pytest.raises(ConfluenceError, match="not a Confluence page id"):
        _service(lambda request: httpx.Response(200, json={})).get_page("../../admin")


@pytest.mark.parametrize("status,words", [(401, "rejected"), (403, "may not read"), (404, "Not found"), (500, "500")])
def test_failures_say_what_went_wrong(settings, status, words):
    with pytest.raises(ConfluenceError, match=words):
        _service(lambda request: httpx.Response(status, text="nope")).search("x")


def test_an_unreachable_confluence_is_a_confluence_error(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(ConfluenceError, match="could not be reached"):
        _service(handler).search("x")
