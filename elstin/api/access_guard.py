"""Keeps everything but elstin's own window away from the Mac app's server.

The app listens on 127.0.0.1, but that is not private: any web page open in the
user's browser can send a request to a local port, and elstin's API can read the
settings, start runs and — through the agents — run commands. So the server the app
starts answers only to its window:

* **A per-launch token.** The launcher (elstin/launcher.py) invents one at every
  start and tells only the window, which opens `/?token=...` once; the server
  answers with a cookie (HttpOnly, SameSite=Strict) and a redirect that drops the
  token from the address, and every later request carries the cookie. A page
  anywhere else has no cookie for this host, and cannot guess a 256-bit token. The
  token is also accepted as `Authorization: Bearer` for scripts.
* **The Host header.** A page can point its own domain at 127.0.0.1 and have the
  browser treat the server as its own origin (DNS rebinding), cookies and all — but
  the request would still say it is for that domain. Only the server's own
  addresses are answered.

Off unless the launcher turns it on: the Docker image and `scripts/start-local.sh`
are reached by people and tools that have no window to hand a token to.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlencode, parse_qsl

COOKIE = "elstin_access"
# Answered without the token (the Host check still applies): it says nothing but "up".
OPEN_PATHS = frozenset({"/healthz"})


@dataclass(frozen=True)
class Access:
    token: str
    hosts: frozenset[str]  # the Host header values this server answers to, e.g. {"127.0.0.1:51234", "localhost:51234"}

    @classmethod
    def for_port(cls, port: int, token: str | None = None) -> "Access":
        return cls(token or secrets.token_urlsafe(32), frozenset({f"127.0.0.1:{port}", f"localhost:{port}"}))


def _same(supplied: str | None, token: str) -> bool:
    return supplied is not None and secrets.compare_digest(supplied.encode(), token.encode())


class AccessGuard:
    """Pure ASGI, not Starlette's BaseHTTPMiddleware: the activity feed streams, and
    that class buffers around streaming responses."""

    def __init__(self, app, access: Access) -> None:
        self.app = app
        self.access = access

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope["headers"]}

        if headers.get("host", "").lower() not in self.access.hosts:
            await self._refuse(scope, send, 421, "Misdirected request: this server only answers to its own address.")
            return
        if scope["type"] == "http" and scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        if self._authorized(headers):
            await self.app(scope, receive, send)
            return

        # The window's first request: the token in the address, once, for a cookie.
        if scope["type"] == "http" and scope["method"] == "GET":
            query = parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)
            if any(name == "token" and _same(value, self.access.token) for name, value in query):
                rest = urlencode([(name, value) for name, value in query if name != "token"])
                await send({
                    "type": "http.response.start", "status": 303,
                    "headers": [
                        (b"location", (scope["path"] + (f"?{rest}" if rest else "")).encode("latin-1")),
                        (b"set-cookie", f"{COOKIE}={self.access.token}; HttpOnly; SameSite=Strict; Path=/".encode("latin-1")),
                        (b"cache-control", b"no-store"),
                        (b"content-length", b"0"),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})
                return
        await self._refuse(scope, send, 401, "elstin: this is only reachable from the elstin app window.")

    def _authorized(self, headers: dict[str, str]) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001 — a malformed Cookie header is just no cookie
            pass
        if COOKIE in cookie and _same(cookie[COOKIE].value, self.access.token):
            return True
        scheme, _, credential = headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and _same(credential.strip(), self.access.token)

    @staticmethod
    async def _refuse(scope, send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = message.encode()
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")],
        })
        await send({"type": "http.response.body", "body": body})
