"""elstin/api/access_guard.py — the Mac app's server answers only to its own window."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from elstin.api.access_guard import COOKIE, Access, AccessGuard
from elstin.app import create_app

PORT = 51234
TOKEN = "t" * 43
HOST = f"127.0.0.1:{PORT}"


@pytest.fixture()
def client(elstin_db) -> TestClient:
    app = create_app(Access.for_port(PORT, TOKEN))
    return TestClient(app, base_url=f"http://{HOST}", follow_redirects=False)


def test_nothing_is_served_without_the_token(client: TestClient):
    assert client.get("/api/config").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/settings").status_code == 401
    assert client.post("/api/projects", json={"name": "x"}).status_code == 401


def test_health_is_open_so_a_supervisor_can_ask_whether_it_is_up(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_the_window_hands_over_the_token_once_and_gets_a_cookie(client: TestClient):
    first = client.get(f"/settings?token={TOKEN}&tab=models")

    assert first.status_code == 303
    assert first.headers["location"] == "/settings?tab=models"  # the token is not left in the address
    cookie = first.headers["set-cookie"]
    assert f"{COOKIE}={TOKEN}" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie

    # the cookie the window now holds is all it needs
    assert client.get("/api/config", headers={"cookie": f"{COOKIE}={TOKEN}"}).status_code == 200
    assert client.get("/", headers={"cookie": f"{COOKIE}={TOKEN}"}).status_code == 200


def test_a_wrong_or_missing_token_gets_nothing(client: TestClient):
    assert client.get("/?token=nope").status_code == 401
    assert client.get("/?token=").status_code == 401
    assert client.get("/api/config", headers={"cookie": f"{COOKIE}=nope"}).status_code == 401
    assert client.get("/api/config", headers={"authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/config", headers={"authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.get("/api/config", headers={"cookie": "garbage;;;="}).status_code == 401


def test_the_token_in_the_address_only_counts_on_a_get(client: TestClient):
    assert client.post(f"/api/projects?token={TOKEN}", json={"name": "x"}).status_code == 401


def test_a_script_may_present_the_token_as_a_bearer(client: TestClient):
    assert client.get("/api/config", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_another_host_name_is_refused_even_with_the_right_token(client: TestClient):
    """DNS rebinding: a page on evil.example whose name now resolves to 127.0.0.1."""
    for host in ("evil.example.com", f"evil.example.com:{PORT}", "127.0.0.1:1", "127.0.0.1", ""):
        refused = client.get("/api/config", headers={"host": host, "authorization": f"Bearer {TOKEN}"})
        assert refused.status_code == 421, host
    assert client.get("/healthz", headers={"host": "evil.example.com"}).status_code == 421


def test_localhost_is_the_same_server(elstin_db):
    client = TestClient(create_app(Access.for_port(PORT, TOKEN)), base_url=f"http://localhost:{PORT}")

    assert client.get("/api/config", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_a_websocket_is_closed_without_the_token(client: TestClient):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/anything"):
            pass


def test_without_access_the_server_is_open_as_the_docker_image_and_start_local_need(elstin_db):
    plain = TestClient(create_app(), base_url="http://anything.example:8000")

    assert plain.get("/api/config").status_code == 200


def test_a_streaming_response_passes_through_unbuffered():
    async def streaming(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b"data: two\n\n"})

    guarded = TestClient(AccessGuard(streaming, Access.for_port(PORT, TOKEN)), base_url=f"http://{HOST}")

    with guarded.stream("GET", "/stream", headers={"cookie": f"{COOKIE}={TOKEN}"}) as response:
        assert response.status_code == 200
        assert "".join(response.iter_text()) == "data: one\n\ndata: two\n\n"
    assert guarded.get("/stream").status_code == 401


def test_each_launch_invents_its_own_token():
    assert Access.for_port(1).token != Access.for_port(1).token
    assert len(Access.for_port(1).token) >= 43  # 256 bits, url-safe
