"""elstin/db/engine.py: where the database lives, what it refuses, and the two
guarantees the rest of the code leans on — a stored time comes back in UTC, and
a read-modify-write inside one `session_scope()` is atomic."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import StatementError

import elstin.db.engine as engine_mod
from elstin.db.engine import ConfigError, checkpoints_path, default_database_url, get_database_url, session_scope
from elstin.db.models import AppConfig

IST = timezone(timedelta(hours=5, minutes=30))


# --- where it lives --------------------------------------------------------


def test_with_no_env_var_the_database_is_in_the_users_data_directory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert get_database_url() == default_database_url()
    assert default_database_url().startswith("sqlite:///")
    assert default_database_url().endswith("/elstin.db")


def test_the_env_var_moves_the_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/mine.db")

    assert get_database_url() == f"sqlite:///{tmp_path}/mine.db"


@pytest.mark.parametrize("url", ["postgresql+psycopg://elstin:elstin@localhost:5433/elstin", "mysql://u@h/db"])
def test_only_sqlite_is_accepted(monkeypatch: pytest.MonkeyPatch, url: str):
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(ConfigError, match="sqlite"):
        get_database_url()


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_an_in_memory_database_is_refused(monkeypatch: pytest.MonkeyPatch, url: str):
    """Every connection would get its own empty one."""
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(ConfigError, match="in-memory"):
        get_database_url()


def test_the_checkpoints_sit_beside_the_database_in_their_own_file():
    assert checkpoints_path("sqlite:////data/elstin.db") == Path("/data/elstin.checkpoints.db")


# --- stored times ----------------------------------------------------------


def _stamp(key: str, when: datetime) -> None:
    with session_scope() as s:
        s.add(AppConfig(key=key, value="x", secret=False, updated_at=when))


def _read(key: str) -> datetime:
    with session_scope() as s:
        return s.get(AppConfig, key).updated_at


def test_a_stored_time_comes_back_aware_and_in_utc(elstin_db):
    when = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
    _stamp("a", when)

    got = _read("a")

    assert got == when
    assert got.tzinfo is not None and got.utcoffset() == timedelta(0)
    assert got > datetime.now(timezone.utc) - timedelta(days=365 * 5)  # comparable with an aware "now"


def test_a_time_in_another_zone_is_stored_as_the_same_instant(elstin_db):
    """Not as its wall-clock reading: 15:00 IST is 09:30 UTC."""
    _stamp("ist", datetime(2026, 10, 12, 15, 0, tzinfo=IST))

    assert _read("ist") == datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)


def test_times_in_different_zones_order_by_the_instant_in_a_query(elstin_db):
    from sqlalchemy import select

    _stamp("later", datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc))        # 11:00 UTC
    _stamp("earlier", datetime(2026, 10, 12, 16, 0, tzinfo=IST))               # 10:30 UTC, but "16:00" as written
    cutoff = datetime(2026, 10, 12, 10, 45, tzinfo=timezone.utc)

    with session_scope() as s:
        keys = set(s.scalars(select(AppConfig.key).where(AppConfig.updated_at > cutoff)))

    assert keys == {"later"}


def test_a_naive_time_is_refused_not_guessed_at(elstin_db):
    with pytest.raises(StatementError, match="naive"):
        _stamp("naive", datetime(2026, 10, 12, 9, 30))


# --- one transaction is atomic --------------------------------------------


def test_concurrent_read_modify_writes_lose_no_update(elstin_db):
    """What the row locks used to guarantee: read a value, write it back plus
    one, from several threads at once — every increment survives and none fails
    with "database is locked"."""
    _stamp("counter", datetime(2026, 1, 1, tzinfo=timezone.utc))
    with session_scope() as s:
        s.get(AppConfig, "counter").value = "0"
    errors: list[BaseException] = []

    def bump(times: int) -> None:
        try:
            for _ in range(times):
                with session_scope() as s:
                    row = s.get(AppConfig, "counter")
                    row.value = str(int(row.value) + 1)
        except BaseException as exc:  # noqa: BLE001 — reported below
            errors.append(exc)

    threads = [threading.Thread(target=bump, args=(40,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert errors == []
    with session_scope() as s:
        assert s.get(AppConfig, "counter").value == str(6 * 40)


def test_the_engine_enforces_foreign_keys_and_runs_in_wal_mode(elstin_db):
    with engine_mod.get_engine().connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_a_nested_scope_fails_at_once_instead_of_waiting_for_the_lock(elstin_db):
    with session_scope():
        with pytest.raises(RuntimeError, match="inside another session_scope"):
            with session_scope():
                pass

    with session_scope():  # the outer one's end released the guard
        pass


def test_a_database_that_cannot_be_opened_does_not_leave_later_scopes_refused(unreadable_database, database_url, monkeypatch):
    with pytest.raises(Exception):
        with session_scope():
            pass

    monkeypatch.setenv("DATABASE_URL", database_url)
    engine_mod._engine = None
    engine_mod._SessionLocal = None
    try:
        with session_scope() as s:
            s.connection()
    finally:
        engine_mod.get_engine().dispose()
        engine_mod._engine = None
        engine_mod._SessionLocal = None


# --- files elstin keeps ---------------------------------------------------------


def test_the_database_is_private_to_the_user(elstin_db, database_url):
    """It holds every JIRA, GitHub and model key the settings page stores."""
    _stamp("k", datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert Path(database_url.removeprefix("sqlite:///")).stat().st_mode & 0o777 == 0o600


def test_a_directory_elstin_creates_is_private_and_one_that_exists_is_left_alone(tmp_path: Path):
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)
    existing.chmod(0o755)

    engine_mod.make_engine(f"sqlite:///{tmp_path}/created/elstin.db").dispose()
    engine_mod.make_engine(f"sqlite:///{existing}/elstin.db").dispose()

    assert (tmp_path / "created").stat().st_mode & 0o777 == 0o700
    assert existing.stat().st_mode & 0o777 == 0o755  # a Docker volume's root is not ours to change


def test_closing_folds_the_log_into_the_one_file_and_a_later_use_reopens(elstin_db, database_url):
    import shutil
    import sqlite3
    from contextlib import closing

    _stamp("k", datetime(2026, 1, 1, tzinfo=timezone.utc))

    engine_mod.close_engine()

    assert engine_mod._engine is None
    alone = Path(database_url.removeprefix("sqlite:///")).with_name("copy-of-just-the-file.db")
    shutil.copyfile(database_url.removeprefix("sqlite:///"), alone)
    with closing(sqlite3.connect(alone)) as conn:
        assert conn.execute("select count(*) from app_config where key = 'k'").fetchone()[0] == 1
    _stamp("after", datetime(2026, 1, 2, tzinfo=timezone.utc))  # the next use opens a fresh engine
    assert _read("after").year == 2026


def test_closing_with_nothing_open_is_harmless():
    engine_mod._engine = None

    engine_mod.close_engine()
