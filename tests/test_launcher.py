"""elstin/launcher.py — the backend the Mac app's window starts, run for real, as
a process: what it prints, who can reach it, what happens when a second one starts
and when it is told to stop or loses its parent.

The same tests check the packaged build: ELSTIN_LAUNCHER=build/dist/elstin-backend/elstin-backend
pytest tests/test_launcher.py runs them against the frozen program instead of
`python -m elstin.launcher` (packaging/build-backend.sh does it after building)."""
from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
# The program under test: the packaged backend if ELSTIN_LAUNCHER names it, else the source.
LAUNCHER = [os.environ["ELSTIN_LAUNCHER"]] if os.environ.get("ELSTIN_LAUNCHER") else [sys.executable, "-m", "elstin.launcher"]


class Launched:
    def __init__(self, proc: subprocess.Popen, database: Path, log: Path) -> None:
        self.proc, self.database, self.log = proc, database, log
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.ready = self.error = None

    def _pump(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            self.lines.put(line.rstrip("\n"))

    def next_line(self, timeout: float = 40) -> str:
        return self.lines.get(timeout=timeout)

    def wait_ready(self) -> dict:
        line = self.next_line()
        assert line.startswith("ELSTIN_READY "), line
        self.ready = json.loads(line.removeprefix("ELSTIN_READY "))
        return self.ready

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.ready['port']}"


def _env(database: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["DATABASE_URL"] = f"sqlite:///{database}"
    return env


@contextmanager
def launch(tmp_path: Path, *extra: str, database: Path | None = None):
    database = database or tmp_path / "data" / "elstin.db"
    log = tmp_path / "logs" / "elstin.log"
    proc = subprocess.Popen(
        [*LAUNCHER, "--log-file", str(log), *extra],
        cwd=ROOT, env=_env(database), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        yield Launched(proc, database, log)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_it_announces_a_port_and_a_token_and_serves_only_to_whoever_holds_the_token(tmp_path: Path):
    with launch(tmp_path) as app:
        ready = app.wait_ready()
        assert ready["port"] > 1024 and len(ready["token"]) >= 43 and ready["pid"] == app.proc.pid

        assert httpx.get(f"{app.base}/healthz").status_code == 200
        assert httpx.get(f"{app.base}/api/config").status_code == 401
        assert httpx.get(f"{app.base}/", headers={"host": "evil.example.com"}).status_code == 421

        with httpx.Client(base_url=app.base, follow_redirects=True) as window:
            page = window.get(f"/?token={ready['token']}")  # what the window does once
            assert page.status_code == 200 and str(page.url).rstrip("/") == app.base  # the token left the address
            assert window.get("/api/config").status_code == 200  # and the cookie does the rest
        assert httpx.get(f"{app.base}/api/config", headers={"authorization": f"Bearer {ready['token']}"}).status_code == 200

        # the web UI's own files are served too (from inside the bundle, when packaged)
        bundle = httpx.get(f"{app.base}/settings-ui/vendor/app.js", headers={"authorization": f"Bearer {ready['token']}"})
        assert bundle.status_code == 200 and len(bundle.content) > 100_000
        assert httpx.get(f"{app.base}/favicon.ico", headers={"authorization": f"Bearer {ready['token']}"}).status_code == 200


def test_it_creates_the_database_and_nothing_is_written_to_stdout_but_the_ready_line(tmp_path: Path):
    with launch(tmp_path) as app:
        app.wait_ready()
        assert app.database.exists()
        assert app.lines.empty()


def test_every_launch_gets_its_own_port_and_token(tmp_path: Path):
    with launch(tmp_path / "a") as first, launch(tmp_path / "b") as second:
        one, two = first.wait_ready(), second.wait_ready()

        assert one["port"] != two["port"] and one["token"] != two["token"]


def test_a_second_copy_on_the_same_database_is_refused_with_a_reason(tmp_path: Path):
    database = tmp_path / "shared" / "elstin.db"
    with launch(tmp_path / "a", database=database) as first:
        first.wait_ready()
        with launch(tmp_path / "b", database=database) as second:
            line = second.next_line()
            assert line.startswith("ELSTIN_ERROR ")
            error = json.loads(line.removeprefix("ELSTIN_ERROR "))
            assert error["error"] == "already_running"
            assert f"process {first.proc.pid}" in error["message"]
            assert second.proc.wait(timeout=20) == 2
        assert httpx.get(f"{first.base}/healthz").status_code == 200  # the first one is untouched


def test_a_clean_quit_leaves_the_one_file_complete(tmp_path: Path):
    """Copy only elstin.db — a backup, or moving it to another Mac — and nothing is
    missing: the write-ahead log was folded into it. (A worker thread still busy at the
    instant of quit can keep the -wal file from being deleted, so that is not asserted;
    what is, is that it holds nothing the main file lacks.)"""
    import shutil
    import sqlite3
    from contextlib import closing

    with launch(tmp_path) as app:
        app.wait_ready()
        app.proc.send_signal(signal.SIGTERM)
        app.proc.wait(timeout=30)

        wal = Path(f"{app.database}-wal")
        assert not wal.exists() or wal.stat().st_size == 0
        lonely = tmp_path / "copied" / "elstin.db"
        lonely.parent.mkdir()
        shutil.copyfile(app.database, lonely)  # the main file, and nothing beside it
        with closing(sqlite3.connect(lonely)) as conn:
            tables = {name for (name,) in conn.execute("select name from sqlite_master where type = 'table'")}
            assert {"alembic_version", "stories", "app_config"} <= tables
            assert conn.execute("select version_num from alembic_version").fetchall() == [("0001",)]


def test_the_files_it_keeps_are_private_to_the_user(tmp_path: Path):
    with launch(tmp_path) as app:
        app.wait_ready()
        modes = {path.name: path.stat().st_mode & 0o777 for path in (app.database, app.log, Path(f"{app.database}.lock"))}
        assert modes == {"elstin.db": 0o600, "elstin.log": 0o600, "elstin.db.lock": 0o600}
        assert app.database.parent.stat().st_mode & 0o777 == 0o700


def test_the_log_is_written_and_never_holds_the_token(tmp_path: Path):
    with launch(tmp_path) as app:
        ready = app.wait_ready()
        httpx.get(f"{app.base}/?token={ready['token']}", follow_redirects=False)
        app.proc.send_signal(signal.SIGTERM)
        app.proc.wait(timeout=30)

        log = app.log.read_text()
        assert "elstin starting" in log and "Application startup complete" in log
        assert ready["token"] not in log


def test_it_stops_when_the_process_that_started_it_is_gone(tmp_path: Path):
    """A window that crashes must not leave a daemon running agents behind it."""
    database = tmp_path / "data" / "elstin.db"
    log = tmp_path / "logs" / "elstin.log"
    command = f"{' '.join(shlex.quote(part) for part in LAUNCHER)} --log-file {shlex.quote(str(log))} & echo $! ; wait"
    parent = subprocess.Popen(["sh", "-c", command], cwd=ROOT, env=_env(database), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in parent.stdout], daemon=True).start()  # type: ignore[union-attr]
        child = int(lines.get(timeout=20))
        assert lines.get(timeout=40).startswith("ELSTIN_READY ")
        os.kill(child, 0)  # alive

        parent.kill()  # the "window" dies without a word

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.25)
        else:
            pytest.fail("the backend kept running after its parent died")
        assert "the process that started elstin is gone" in log.read_text()
    finally:
        parent.kill()
        parent.wait()


def test_it_can_be_pinned_to_a_port(tmp_path: Path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with launch(tmp_path, "--port", str(port)) as app:
        assert app.wait_ready()["port"] == port


def _occupied_port():
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    return holder


def test_a_preferred_port_that_is_free_is_the_one_it_gets(tmp_path: Path):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with launch(tmp_path, "--prefer-port", str(port)) as app:
        assert app.wait_ready()["port"] == port


def test_a_preferred_port_that_is_taken_falls_back_to_a_free_one(tmp_path: Path):
    holder = _occupied_port()
    try:
        taken = holder.getsockname()[1]
        with launch(tmp_path, "--prefer-port", str(taken)) as app:
            port = app.wait_ready()["port"]
            assert port != taken and port > 1024
            assert httpx.get(f"http://127.0.0.1:{port}/healthz").status_code == 200
    finally:
        holder.close()


def test_a_port_that_was_asked_for_outright_and_is_taken_is_an_error(tmp_path: Path):
    holder = _occupied_port()
    try:
        with launch(tmp_path, "--port", str(holder.getsockname()[1])) as app:
            assert app.proc.wait(timeout=30) != 0
    finally:
        holder.close()
