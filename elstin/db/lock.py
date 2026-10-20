"""One elstin per database.

The daemon inside the app dispatches stories, merges branches and opens pull
requests from the database it reads. Two of them on one database would each
dispatch the same story. SQLite has no server to notice that, so the process that
owns a database holds an exclusive lock on a file beside it for as long as it runs;
the lock goes with the process however it ends, so a crash never leaves a stale one.

A process takes a database's lock once, however many times it asks: the app's
startup and the launcher (elstin/launcher.py) both do, and the last to let go
releases it.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Another elstin process holds this database."""


# lock file -> [file descriptor, how many holders in this process]
_held: dict[Path, list[int]] = {}


def lock_path(database: Path) -> Path:
    return database.with_name(database.name + ".lock")


def acquire(database: Path) -> None:
    path = lock_path(database)
    if path in _held:
        _held[path][1] += 1
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = os.pread(fd, 32, 0).decode(errors="replace").strip() or "unknown"
        os.close(fd)
        raise AlreadyRunning(
            f"Another elstin (process {owner}) is already using {database}. "
            "Two of them on one database would each pick up the same work; quit the other one first."
        ) from None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _held[path] = [fd, 1]


def release(database: Path) -> None:
    path = lock_path(database)
    held = _held.get(path)
    if held is None:
        return
    held[1] -= 1
    if held[1] > 0:
        return
    del _held[path]
    os.close(held[0])  # closing the descriptor drops the flock
