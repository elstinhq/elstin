"""The Mac app's backend: the one process its window starts.

    elstin-app            # or: python -m elstin.launcher

What it does that `uvicorn elstin.app:create_app --factory` does not:

* takes the database's single-instance lock first, so a second copy fails at once
  and says why instead of dispatching the same stories twice;
* listens on 127.0.0.1 on a free port it picks itself, and starts the server with
  a per-launch access token (elstin/api/access_guard.py), so nothing but the
  window that is told the token can use it;
* writes its log to ~/Library/Logs/Elstin/elstin.log (rotating), not a terminal;
* stops when whatever started it is gone — a window that crashed must not leave a
  daemon running agents behind it — and on SIGTERM/SIGINT, like any uvicorn.

The window reads the answer from stdout, one line, once the server is serving (the
database is migrated by then):

    ELSTIN_READY {"port": 51234, "token": "...", "pid": 4321}

or, if it could not start, `ELSTIN_ERROR {"error": "already_running" | "startup_failed",
"message": "..."}` and a non-zero exit. It opens `http://127.0.0.1:<port>/?token=<token>`.
Nothing else is written to stdout. Like the rest of elstin it reads no environment
variable but the optional DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import socket
import sys
from pathlib import Path

import uvicorn

from elstin.api.access_guard import Access
from elstin.app import create_app
from elstin.db import lock
from elstin.db.engine import data_home, database_path, make_private

logger = logging.getLogger("elstin.launcher")

READY = "ELSTIN_READY"
FAILED = "ELSTIN_ERROR"
HOST = "127.0.0.1"
EXIT_ALREADY_RUNNING = 2
EXIT_STARTUP_FAILED = 3


def default_log_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Elstin" / "elstin.log"
    return data_home() / "logs" / "elstin.log"


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))
    make_private(log_file)  # agents' commands and their output pass through it


def _tell(prefix: str, payload: dict) -> None:
    print(f"{prefix} {json.dumps(payload)}", flush=True)


def _listening_socket(port: int, *, prefer: bool = False) -> socket.socket:
    """Bound to `port` (0: any free one). With `prefer`, a port that is taken is not
    an error: any free one is used instead."""
    for candidate in ([port, 0] if prefer and port else [port]):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, candidate))
        except OSError:
            sock.close()
            if candidate == 0 or not prefer:
                raise
            continue
        return sock
    raise AssertionError("unreachable")


async def _exit_when_orphaned(server: uvicorn.Server, parent: int) -> None:
    """A process that outlives its parent is reparented (to launchd, pid 1)."""
    while not server.should_exit:
        await asyncio.sleep(1)
        if os.getppid() != parent:
            logger.warning("the process that started elstin is gone; shutting down")
            server.should_exit = True


async def _serve(sock: socket.socket, access: Access, *, exit_with_parent: bool) -> int:
    server = uvicorn.Server(uvicorn.Config(
        create_app(access),
        log_config=None,       # configure_logging owns logging
        access_log=False,      # a request line would carry the first-load token
        ws="none",             # the app streams with server-sent events; no WebSockets
        loop="asyncio",        # the plain stack, not whichever optional speedups (uvloop,
        http="h11",            # httptools) happen to be installed: what the bundle ships is what is tested
        lifespan="on",
    ))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started and not serving.done():
        await asyncio.sleep(0.05)
    if not server.started:
        await serving
        _tell(FAILED, {"error": "startup_failed", "message": "elstin could not start; see its log."})
        return EXIT_STARTUP_FAILED
    _tell(READY, {"port": sock.getsockname()[1], "token": access.token, "pid": os.getpid()})
    parent = os.getppid()
    watchdog = asyncio.create_task(_exit_when_orphaned(server, parent)) if exit_with_parent and parent > 1 else None
    try:
        await serving
    finally:
        if watchdog is not None:
            watchdog.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start elstin's backend for its Mac app window.")
    parser.add_argument("--port", type=int, default=0, help="Port on 127.0.0.1 (default: any free one).")
    parser.add_argument("--prefer-port", type=int, default=0, metavar="PORT",
                        help="Use this port if it is free, else any free one. The window passes the one it used last, so the "
                             "page keeps its address — and with it the browser storage (theme, sidebar) the UI saves per address.")
    parser.add_argument("--log-file", type=Path, default=None, help=f"Log file (default: {default_log_file()}).")
    parser.add_argument("--no-exit-with-parent", action="store_true", help="Keep running when the process that started it ends.")
    parser.add_argument("--self-test", action="store_true", help="Check this build has everything it needs (elstin/selftest.py), then exit.")
    args = parser.parse_args(argv)

    if args.self_test:
        from elstin.selftest import run as self_test

        return self_test()

    configure_logging(args.log_file or default_log_file())
    database = database_path()
    try:
        lock.acquire(database)
    except lock.AlreadyRunning as exc:
        logger.error("%s", exc)
        _tell(FAILED, {"error": "already_running", "message": str(exc)})
        return EXIT_ALREADY_RUNNING
    try:
        sock = _listening_socket(args.prefer_port or args.port, prefer=bool(args.prefer_port))
        access = Access.for_port(sock.getsockname()[1])
        logger.info("elstin starting: database %s, port %s", database, sock.getsockname()[1])
        return asyncio.run(_serve(sock, access, exit_with_parent=not args.no_exit_with_parent))
    finally:
        lock.release(database)


if __name__ == "__main__":
    sys.exit(main())
