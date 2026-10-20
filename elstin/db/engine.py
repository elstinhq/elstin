"""The one place elstin opens its database: a single SQLite file.

The database is a file under the user's data directory (see `data_home`) —
nothing to install, start or connect to. `DATABASE_URL` is the one process env
var and is optional: it overrides where that file lives (the Docker image sets
it to a path on its data volume), and it must be a `sqlite:///` URL. Every
other setting is read from the `app_config` table (elstin/config/store.py).

Concurrency. The app is one process: FastAPI handlers, the daemon's loops and
the agents' worker threads all write through this engine. SQLite allows one
writer at a time, and a transaction that reads and then writes can lose that
race with another writer and fail at once, regardless of any timeout. So every
transaction takes the write lock at BEGIN (`BEGIN IMMEDIATE`): a second one
waits (up to `BUSY_TIMEOUT_SECONDS`) for the first to finish, and a
read-modify-write inside one `session_scope()` is atomic. That is what the row
locks (`SELECT ... FOR UPDATE`) did on Postgres. The cost: keep a
`session_scope()` short — it must not span a network call, a subprocess or a
model call, or every other writer waits behind it. Readers are not blocked by a
writer (WAL), but they do queue behind one when they open a scope the same way.
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# How long a writer waits for the write lock before giving up with "database is
# locked". Generous: the holders are short transactions, so a wait this long
# means something is wrong, not just busy.
BUSY_TIMEOUT_SECONDS = 30


class ConfigError(RuntimeError):
    pass


def data_home() -> Path:
    """Where elstin keeps its own files when nothing says otherwise: the
    database, and (until the settings say elsewhere) nothing else."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Elstin"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "elstin"


def default_database_url() -> str:
    return f"sqlite:///{data_home() / 'elstin.db'}"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL") or default_database_url()
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ConfigError(
            f"DATABASE_URL must be a sqlite:/// URL (got {parsed.get_backend_name()!r}) — elstin keeps "
            "its state in a single SQLite file. Unset it to use the default location, "
            f"{default_database_url()}."
        )
    if not parsed.database or parsed.database == ":memory:":
        raise ConfigError(
            "DATABASE_URL names an in-memory database, which every connection would get its own empty copy of — "
            "give it a file path, e.g. sqlite:////path/to/elstin.db."
        )
    return url


def get_database_url() -> str:
    """Public accessor for callers outside the engine itself — e.g. the
    LangGraph checkpointer, which needs the file rather than a SQLAlchemy
    engine."""
    return _database_url()


def database_path(url: str | None = None) -> Path:
    """The database file `url` (default: the configured one) points at."""
    return Path(make_url(url or _database_url()).database)  # type: ignore[arg-type] — checked non-empty in _database_url


def checkpoints_path(url: str | None = None) -> Path:
    """The LangGraph checkpointer's file, beside the database. Its own file so
    its writes (one per graph step, with sizeable blobs) never queue behind, or
    hold up, the app's own transactions — and so a run's durable state can be
    thrown away without touching the board."""
    db = database_path(url)
    return db.with_name(f"{db.stem}.checkpoints{db.suffix}")


def make_private(path: Path) -> None:
    """Owner-only (0600) on a file elstin keeps: the database holds every JIRA,
    GitHub and model key the settings page stores, in the clear, and on a shared Mac
    a default 0644 would let any other account read them. Best effort: a volume
    mounted from elsewhere may not let this process change it."""
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError:
        pass


def make_engine(url: str, *, foreign_keys: bool = True) -> Engine:
    """An engine on `url` with the settings the concurrency model above needs.
    `foreign_keys=False` is for Alembic: a batch migration rebuilds a table,
    and SQLite cannot do that with enforcement on."""
    path = Path(make_url(url).database)  # type: ignore[arg-type] — a file path, checked in _database_url
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private when elstin makes it; a directory that exists is left alone
    engine = create_engine(
        url, future=True,
        # Sessions are used from the daemon's worker threads (asyncio.to_thread)
        # as well as the one that made them.
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _record) -> None:
        make_private(path)  # before WAL mode: the -wal and -shm files are created with the database's permissions
        # None: the driver stops opening transactions behind our back, so the
        # BEGIN below is the only one.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
        # WAL + NORMAL can lose the last commit on a power cut, never corrupt the file.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine(_database_url())
    return _engine


def close_engine() -> None:
    """Fold the write-ahead log into the database file and close every connection.
    What a clean quit does, so that the one file is the whole database — copy it
    as a backup and nothing is missing — and the -wal and -shm files are gone.
    A later `get_engine()` simply opens a new one."""
    global _engine, _SessionLocal
    engine, _engine, _SessionLocal = _engine, None, None
    if engine is None:
        return
    try:
        raw = engine.raw_connection()  # not a transaction: PRAGMA wal_checkpoint cannot run inside one
        try:
            raw.cursor().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            raw.close()
    except Exception:  # noqa: BLE001 — a checkpoint that cannot run now runs at the next open
        pass
    engine.dispose()


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


_scope = threading.local()


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction. It holds SQLite's write lock from its first statement to
    the commit (module docstring), so keep the body short and never do slow work
    — network, subprocess, a model call — inside it.

    Scopes do not nest. An inner one is another connection asking for the write
    lock its own caller holds; it would wait out the whole busy timeout and then
    fail, so it fails at once instead, naming the mistake."""
    if getattr(_scope, "open", False):
        raise RuntimeError(
            "session_scope() opened inside another session_scope(): the inner transaction would wait for the "
            "write lock the outer one holds. Finish the outer scope first, or pass its session down."
        )
    session = get_sessionmaker()()  # may raise (a database that cannot be opened): nothing is marked open yet
    _scope.open = True
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        _scope.open = False
