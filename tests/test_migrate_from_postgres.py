"""scripts/migrate_postgres_to_sqlite.py — the one-off copy from the old,
Postgres-backed database into the SQLite one.

The suite needs no Docker, so the *source* here is a SQLite file with the same
schema (the script reflects whatever it is given). That covers the copy itself,
its refusals and its all-or-nothing transaction. What only a real Postgres can
show — psycopg's aware datetimes, JSONB, the read-only repeatable-read snapshot,
and the conversion of each story's latest LangGraph checkpoint between the two
savers — was checked by running the script against a Postgres built with the old
code; none of it is in this file."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import make_url

from elstin.db.base import Base
from elstin.db.engine import make_engine

from tests.synthetic_rows import fill

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_postgres_to_sqlite.py"
_spec = importlib.util.spec_from_file_location("migrate_postgres_to_sqlite", _SCRIPT)
m = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = m
_spec.loader.exec_module(m)

BASELINE = "0001"  # the revision of a database built by the current Alembic chain


def quiet(*_args, **_kwargs) -> None:
    pass


@pytest.fixture()
def source_url(_migrated_database: Path, tmp_path_factory: pytest.TempPathFactory) -> str:
    """A filled database standing in for the old one."""
    import shutil

    path = tmp_path_factory.mktemp("old-database") / "old.db"
    shutil.copyfile(_migrated_database, path)
    url = f"sqlite:///{path}"
    engine = make_engine(url)
    with engine.begin() as conn:
        fill(conn, Base.metadata, rows_per_table=3)
    engine.dispose()
    return url


@pytest.fixture()
def target_path(elstin_db, database_url: str) -> Path:
    """An empty, migrated database — the one the script fills."""
    return Path(make_url(database_url).database)


def _counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as conn:
        return {t.name: conn.execute(f'select count(*) from "{t.name}"').fetchone()[0] for t in Base.metadata.sorted_tables}


def _rows(url: str | Path, *, models: bool) -> dict[str, list[dict]]:
    """Every row of every table, each value reduced to something comparable."""
    engine = create_engine(url if isinstance(url, str) else f"sqlite:///{url}")
    meta = Base.metadata
    if not models:
        meta = MetaData()
        meta.reflect(bind=engine)

    def norm(value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (Decimal, float)):
            return round(float(value), 6)
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return value

    out = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            t = meta.tables[table.name]
            rows = conn.execute(select(t).order_by(*t.primary_key.columns)).mappings().all()
            out[table.name] = [{k: norm(v) for k, v in row.items()} for row in rows]
    engine.dispose()
    return out


# --- the copy -----------------------------------------------------------------


def test_every_row_of_every_table_arrives_unchanged(source_url: str, target_path: Path):
    report = m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    assert sum(report.copied.values()) == 51 and len(report.copied) == len(Base.metadata.tables) == 17
    assert _rows(target_path, models=True) == _rows(source_url, models=False)


def test_ids_are_kept_so_every_reference_still_points_at_its_row(source_url: str, target_path: Path):
    m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    with closing(sqlite3.connect(target_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # the ids are the source's, not renumbered: a child's foreign key names a parent that exists
        assert conn.execute("select count(*) from epics e join projects p on p.id = e.project_id").fetchone()[0] == 3


def test_a_row_inserted_afterwards_continues_the_numbering(source_url: str, target_path: Path):
    m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)
    engine = make_engine(f"sqlite:///{target_path}")
    with engine.begin() as conn:
        highest = conn.exec_driver_sql("select max(id) from projects").scalar_one()
        conn.exec_driver_sql(
            "insert into projects (name, ticket_source, created_at, updated_at) values ('new', 'jira', '2026-01-01', '2026-01-01')"
        )
        assert conn.exec_driver_sql("select max(id) from projects").scalar_one() == highest + 1
    engine.dispose()


def test_naive_times_are_taken_as_utc_and_counted():
    report = m.Report()
    naive = datetime(2026, 10, 12, 9, 30)
    aware = datetime(2026, 10, 12, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    row = m.normalize_row({"a": naive, "b": aware, "c": "text", "d": None}, report)

    assert row["a"] == datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
    assert row["b"] == aware and row["c"] == "text" and row["d"] is None
    assert report.naive_times_taken_as_utc == 1


# --- what it refuses ------------------------------------------------------------


def test_a_target_that_already_has_rows_is_refused_and_left_as_it_was(source_url: str, target_path: Path):
    with closing(sqlite3.connect(target_path)) as conn:
        conn.execute("insert into app_config (key, value, secret, updated_at) values ('mine', 'v', 0, '2026-01-01 00:00:00')")
        conn.commit()

    with pytest.raises(m.MigrationError, match="already has rows: app_config"):
        m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    counts = _counts(target_path)
    assert counts["app_config"] == 1 and sum(counts.values()) == 1


def test_a_source_at_another_revision_is_refused_and_creates_no_target(source_url: str, tmp_path: Path, monkeypatch):
    target = tmp_path / "never" / "elstin.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")

    with pytest.raises(m.MigrationError, match=f"at revision {BASELINE}, and this script reads {m.SOURCE_REVISION}"):
        m.run(source_url, assume_yes=True, say=quiet)  # the real expected revision: Postgres-era 20261005_0037

    assert not target.exists() and not target.parent.exists()


def test_a_source_whose_columns_differ_is_refused(source_url: str, target_path: Path):
    with closing(sqlite3.connect(make_url(source_url).database)) as conn:
        conn.execute("alter table app_config drop column secret")
        conn.commit()

    with pytest.raises(m.MigrationError, match=r"Table 'app_config' differs:.*only in elstin now \['secret'\]"):
        m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    assert sum(_counts(target_path).values()) == 0


def test_a_source_that_is_not_elstins_is_refused(tmp_path: Path, target_path: Path):
    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("create table unrelated (id integer primary key)")
        conn.commit()

    with pytest.raises(m.MigrationError, match="no alembic_version"):
        m.run(f"sqlite:///{other}", assume_yes=True, skip_checkpoints=True, say=quiet)


def test_without_the_driver_it_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)  # an import of it now fails

    with pytest.raises(m.MigrationError, match=r'pip install "psycopg\[binary\]"'):
        m.open_source(make_url("postgresql+psycopg://u:p@localhost/db"))


def test_a_bare_postgresql_url_is_read_with_psycopg_not_psycopg2():
    assert m._source_url("postgresql://u:p@h/db").drivername == "postgresql+psycopg"
    assert m._source_url("postgresql+psycopg://u:p@h/db").drivername == "postgresql+psycopg"


# --- dry run, and all-or-nothing --------------------------------------------------


def test_a_dry_run_reports_and_writes_nothing(source_url: str, tmp_path: Path, monkeypatch):
    target = tmp_path / "never" / "elstin.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")
    said: list[str] = []

    m.run(source_url, dry_run=True, expected_revision=BASELINE, say=said.append)

    assert not target.parent.exists()
    assert any("51 rows in 17 of 17 tables" in line for line in said) and any("Dry run" in line for line in said)


def test_a_failure_part_way_through_leaves_the_target_empty(source_url: str, target_path: Path, monkeypatch):
    real = m.normalize_row
    seen = [0]

    def explode_on_the_thirtieth_row(row, report):
        seen[0] += 1
        if seen[0] == 30:
            raise RuntimeError("the disk caught fire")
        return real(row, report)

    monkeypatch.setattr(m, "normalize_row", explode_on_the_thirtieth_row)

    with pytest.raises(RuntimeError, match="caught fire"):
        m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    assert seen[0] == 30  # it got well into the copy first
    assert sum(_counts(target_path).values()) == 0


def test_a_count_that_does_not_add_up_keeps_nothing(source_url: str, target_path: Path, monkeypatch):
    real = m.copy_rows

    def drop_a_row(source, target, plans, report, say=print):
        real(source, target, plans, report, say)
        target.exec_driver_sql("delete from app_config where key = (select min(key) from app_config)")

    monkeypatch.setattr(m, "copy_rows", drop_a_row)

    with pytest.raises(m.MigrationError, match="Table 'app_config'.*3 rows and 2 arrived"):
        m.run(source_url, assume_yes=True, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    assert sum(_counts(target_path).values()) == 0


def test_confirmation_is_asked_and_a_no_copies_nothing(source_url: str, target_path: Path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")

    with pytest.raises(m.MigrationError, match="Cancelled"):
        m.run(source_url, skip_checkpoints=True, expected_revision=BASELINE, say=quiet)

    assert sum(_counts(target_path).values()) == 0
