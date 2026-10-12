"""The UI's shell and bundles (elstin/app.py). The UI is one app with its own
client-side routes, so every page URL serves the same shell. Shell and bundles
keep fixed names, so they must be revalidated on every load — otherwise a
browser runs a cached bundle after a rebuild and a new button exists on the
server but not on screen."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("elstin_db")

PAGES = [
    "/",
    "/projects",
    "/settings",
    "/projects/1",
    "/projects/1/tasks",
    "/projects/1/lessons",
    "/projects/1/backlog/new",
    "/projects/1/backlog/7",
    # The PO pages' old URLs: still served, the app redirects them.
    "/projects/1/po/new",
    "/projects/1/po/7",
]


def _client() -> TestClient:
    from elstin.app import create_app

    return TestClient(create_app())


@pytest.mark.parametrize("url", PAGES)
def test_every_page_url_serves_the_one_shell(url):
    response = _client().get(url)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in response.text
    assert "/settings-ui/vendor/app.js" in response.text


# A file under /settings-ui that git tracks: the bundles in vendor/ are build
# output, absent from a fresh checkout, but they are served by the same mount.
@pytest.mark.parametrize("url", [*PAGES, "/settings-ui/app.html"])
def test_ui_files_are_revalidated_on_every_load(url):
    response = _client().get(url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag") or response.headers.get("last-modified")  # so a revalidation can be a 304


def test_the_shell_does_not_swallow_other_routes():
    client = _client()

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/projects").headers["content-type"].startswith("application/json")
    assert client.get("/api/projects/999999").status_code == 404
    assert client.get("/settings-ui/favicon.svg").headers["content-type"].startswith("image/svg")
    assert client.get("/no-such-page").status_code == 404
