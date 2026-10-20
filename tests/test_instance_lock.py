"""elstin/db/lock.py — one elstin per database."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from elstin.app import create_app
from elstin.db import lock


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    return tmp_path / "data" / "elstin.db"


def _held_by_someone_else(database: Path, pid: int = 4242) -> int:
    """Take the lock the way another process would: a descriptor of its own."""
    path = lock.lock_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, f"{pid}\n".encode())
    return fd


def test_a_second_holder_is_refused_and_told_who_has_it(database: Path):
    other = _held_by_someone_else(database, pid=4242)
    try:
        with pytest.raises(lock.AlreadyRunning, match=r"process 4242\) is already using .*elstin\.db"):
            lock.acquire(database)
    finally:
        os.close(other)


def test_it_can_be_taken_once_the_other_has_gone(database: Path):
    os.close(_held_by_someone_else(database))  # the other process ended: closing drops its lock

    lock.acquire(database)
    try:
        assert lock.lock_path(database).read_text().strip() == str(os.getpid())
        assert oct(lock.lock_path(database).stat().st_mode & 0o777) == "0o600"
    finally:
        lock.release(database)


def test_a_process_may_ask_twice_and_the_last_to_let_go_releases(database: Path):
    """The launcher and the app's own startup both take it."""
    lock.acquire(database)
    lock.acquire(database)
    lock.release(database)
    fd = os.open(lock.lock_path(database), os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):  # still held after the first release
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.release(database)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # and free after the last
    finally:
        os.close(fd)
    lock.release(database)  # releasing what is not held is harmless


def test_the_app_refuses_to_start_beside_another_and_says_so(elstin_db, database_url: str):
    from sqlalchemy.engine import make_url

    db = Path(make_url(database_url).database)
    other = _held_by_someone_else(db)
    try:
        with pytest.raises(lock.AlreadyRunning, match="already using"):
            with TestClient(create_app()):
                pass
    finally:
        os.close(other)


def test_the_app_holds_the_lock_while_it_runs_and_frees_it_when_it_stops(elstin_db, database_url: str):
    from sqlalchemy.engine import make_url

    db = Path(make_url(database_url).database)
    with TestClient(create_app()):
        fd = os.open(lock.lock_path(db), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    fd = os.open(lock.lock_path(db), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again
    finally:
        os.close(fd)
