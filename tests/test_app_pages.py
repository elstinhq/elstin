"""The UI's pages and bundles (elstin/app.py). They keep fixed names, so they
must be revalidated on every load — otherwise a browser runs a cached bundle
after a rebuild and a new button exists on the server but not on screen."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.usefixtures("elstin_db")
# A file under /settings-ui that git tracks: the bundles in vendor/ are build
# output, absent from a fresh checkout, but they are served by the same mount.
@pytest.mark.parametrize("url", ["/", "/projects", "/projects/1", "/projects/1/po/new", "/projects/1/po/7", "/settings", "/settings-ui/dashboard.html"])
def test_ui_files_are_revalidated_on_every_load(url):
    from elstin.app import create_app

    response = TestClient(create_app()).get(url)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag") or response.headers.get("last-modified")  # so a revalidation can be a 304
