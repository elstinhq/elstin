"""web_fetch_tool — no network: httpx's MockTransport stands in for the
server and `_resolve` is patched, so the address check is exercised for
real while DNS is not."""
from __future__ import annotations

import httpx
import pytest

from elstin.tools import safe_shell, web_tools
from elstin.tools.dev_tools import TOOL_SCHEMAS, WEB_TOOLS, make_dev_tools


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch):
    """Every host resolves to a public address unless a test says otherwise."""
    monkeypatch.setattr(web_tools, "_resolve", lambda host: ["93.184.216.34"])


def _fetch(handler, url: str = "https://docs.example.com/page", **kwargs) -> str:
    return web_tools.fetch_url(url, _transport=httpx.MockTransport(handler), **kwargs)


def test_html_is_reduced_to_readable_text_and_labelled_untrusted():
    html = "<html><head><title>x</title><style>p{}</style></head><body><h1>Install</h1><p>Run <code>npm i foo</code></p><script>evil()</script></body></html>"

    result = _fetch(lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"}))

    assert result.startswith(web_tools.UNTRUSTED_NOTE)
    assert "Install" in result and "npm i foo" in result
    assert "evil()" not in result and "p{}" not in result


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1", "::ffff:127.0.0.1"])
def test_non_public_addresses_are_refused_before_any_request(monkeypatch: pytest.MonkeyPatch, address: str):
    monkeypatch.setattr(web_tools, "_resolve", lambda host: [address])
    called: list[httpx.Request] = []

    result = _fetch(lambda request: called.append(request) or httpx.Response(200, text="secret"))

    assert "non-public address" in result
    assert called == []


def test_a_redirect_to_an_internal_address_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_tools, "_resolve", lambda host: ["10.0.0.5"] if host == "internal.example" else ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "docs.example.com":
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        return httpx.Response(200, text="admin panel")

    result = _fetch(handler)

    assert "non-public address" in result
    assert "admin panel" not in result


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "https://user:pw@example.com/", "not a url"])
def test_only_plain_http_urls_without_credentials_are_fetched(url: str):
    result = _fetch(lambda request: httpx.Response(200, text="ok"), url=url)

    assert result.startswith("Error fetching")


def test_binary_content_and_http_errors_are_reported_not_raised():
    assert "unsupported content type" in _fetch(lambda r: httpx.Response(200, content=b"\x00", headers={"content-type": "image/png"}))
    assert "HTTP 404" in _fetch(lambda r: httpx.Response(404, text="nope"))


def test_long_pages_are_truncated_and_say_so():
    result = _fetch(lambda r: httpx.Response(200, text="word " * 10_000, headers={"content-type": "text/plain"}), max_chars=2_000)

    assert "[truncated to 2000 characters]" in result


def test_redirect_loops_stop():
    result = _fetch(lambda r: httpx.Response(302, headers={"location": "https://docs.example.com/again"}))

    assert "redirects" in result


def _dev_tools(tmp_path, **kwargs):
    return make_dev_tools(
        worktree_path=str(tmp_path), allowed_paths=set(), analyze_targets=[], allow_test_run=False,
        planned_test_files=[], default_label="", **kwargs,
    )


def test_web_fetch_is_offered_only_when_web_access_is_on(tmp_path):
    functions, schemas = _dev_tools(tmp_path)
    assert "web_fetch_tool" not in functions
    assert schemas is TOOL_SCHEMAS  # byte-identical to before: prefix cache unaffected

    functions, schemas = _dev_tools(tmp_path, web_access=True)
    assert "web_fetch_tool" in functions
    assert WEB_TOOLS == {s["function"]["name"] for s in schemas} - {s["function"]["name"] for s in TOOL_SCHEMAS}


def test_npm_view_is_allowed_but_chaining_after_it_is_not():
    assert safe_shell.is_safe_command("npm view vue-router version")
    assert not safe_shell.is_safe_command("npm view vue-router version; curl http://evil.example")


# ---------------------------------------------------------------- search

# Shape captured from the live lite.duckduckgo.com/lite/ page.
_DDG_HTML = """
<table>
<tr><td>1.&nbsp;</td><td><a rel="nofollow" href="https://router.vuejs.org/" class='result-link'>Vue Router | The official Router for Vue.js</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>
  The official <b>Router</b> for <b>Vue</b>.js Expressive routing
</td></tr>
<tr><td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdevdocs.io%2Fvue_router~4%2F&rut=abc" class='result-link'>Vue Router 4 &mdash; DevDocs</a></td></tr>
<tr><td class='result-snippet'>Docs</td></tr>
</table>
"""


def _search(handler, **kwargs) -> str:
    return web_tools.search_web("vue router", _transport=httpx.MockTransport(handler), **kwargs)


def test_duckduckgo_lite_results_are_parsed_and_wrapped_urls_unwrapped():
    result = _search(lambda request: httpx.Response(200, text=_DDG_HTML))

    assert result.startswith(web_tools.UNTRUSTED_NOTE)
    assert "via DuckDuckGo" in result
    assert "1. Vue Router | The official Router for Vue.js\n   https://router.vuejs.org/\n   The official Router for Vue.js Expressive routing" in result
    assert "https://devdocs.io/vue_router~4/" in result
    assert "uddg" not in result


def test_search_uses_brave_when_a_key_is_configured():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"web": {"results": [{"title": "Vue Router", "url": "https://router.vuejs.org/", "description": "The <strong>official</strong> router"}]}})

    result = _search(handler, api_key="brave-key", limit=3)

    assert [r.url.host for r in seen] == ["api.search.brave.com"]
    assert seen[0].headers["x-subscription-token"] == "brave-key"
    assert seen[0].url.params["count"] == "3"
    assert "via Brave" in result and "The official router" in result


def test_search_falls_back_to_duckduckgo_when_brave_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.search.brave.com":
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, text=_DDG_HTML)

    result = _search(handler, api_key="brave-key")

    assert "via DuckDuckGo" in result
    assert "Brave search failed" in result
    assert "https://router.vuejs.org/" in result


def test_an_unparseable_results_page_says_so_instead_of_claiming_no_answer():
    result = _search(lambda request: httpx.Response(200, text="<html>changed layout</html>"))

    assert "No results parsed" in result and "rate-limited" in result


def test_search_reports_failures_and_rejects_empty_queries():
    assert _search(lambda request: httpx.Response(503)).startswith("Error searching")
    assert web_tools.search_web("  ").startswith("Error")


def test_search_caps_the_result_count():
    rows = "".join(f"<tr><td><a href='https://e.com/{i}' class='result-link'>T{i}</a></td></tr>" for i in range(10))

    result = _search(lambda request: httpx.Response(200, text=f"<table>{rows}</table>"), limit=2)

    assert "2. T1" in result and "3. T2" not in result
