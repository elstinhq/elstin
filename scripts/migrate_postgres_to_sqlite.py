"""Copy an old, Postgres-backed elstin database into the SQLite one.

elstin used to keep its state in Postgres and now keeps it in one SQLite file.
Nothing converts a database on its own — the new app starts empty — so this is
the one-off bridge: it reads the Postgres database and writes every row of it,
and every story's latest LangGraph checkpoint, into the SQLite files the app
uses now. The Postgres database is only read (one read-only, repeatable-read
transaction); it is left exactly as it was, so it stays a backup.

    # the container the old start-local.sh used; the connection details are read
    # from the container itself, so no password goes on the command line
    python scripts/migrate_postgres_to_sqlite.py --from-container code_forge-forge-postgres

    # or say where it is
    python scripts/migrate_postgres_to_sqlite.py --from postgresql+psycopg://elstin:elstin@localhost:5433/elstin

    python scripts/migrate_postgres_to_sqlite.py --from-container NAME --dry-run   # read and report, write nothing

Before you run it:
  * Stop elstin (the app and any scripts/start-local.sh), and let runs finish or
    park: a run in flight writes to the Postgres you are about to copy.
  * The Postgres database must be at the last revision the old code knew
    (20261005_0037) — start the *old* checkout once and it migrates itself; the
    script refuses anything else, because a schema that differs would copy wrong.
  * The SQLite target must be empty: the default location, or DATABASE_URL. If you
    have already used the new app, `python scripts/clear_db.py` empties it.
  * Postgres is no longer a dependency of elstin, so the driver is not installed
    for you: `pip install "psycopg[binary]" langgraph-checkpoint-postgres`
    (the second only for the checkpoints; `--no-checkpoints` skips them).

What comes across: every table, as it is (ids included), all or nothing — the rows
go in one transaction and are counted and foreign-key-checked before it commits.
Times arrive as the same instants, in UTC. Each story's *latest* checkpoint comes
too, because a story's plan, its dev history and where a parked run stands live
only there; the older checkpoints of a run (its history) do not. Settings, API
keys and tokens come across with everything else, in the clear — as they were.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, MetaData, Table, create_engine, func, select
from sqlalchemy.engine import URL, make_url

from elstin.db.base import Base
from elstin.db.engine import checkpoints_path, database_path, get_database_url, make_engine
from elstin.db.migrate import upgrade_to_head
from elstin.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata

# The last revision of the Postgres-era Alembic chain (alembic/versions/20261005_0037_po_sessions.py,
# in git history): the schema this script knows how to read.
SOURCE_REVISION = "20261005_0037"

# Tables in the source that are not elstin's own: Alembic's bookkeeping, and the LangGraph
# checkpointer's, which is copied separately (and differently — see copy_checkpoints).
NOT_COPIED_AS_ROWS = {"alembic_version", "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}

CHUNK_ROWS = 1000


class MigrationError(RuntimeError):
    """Something that makes copying unsafe or impossible — said in a sentence a person can act on."""


@dataclass
class TablePlan:
    name: str
    source: Table
    target: Table
    rows: int = 0


@dataclass
class Report:
    copied: dict[str, int] = field(default_factory=dict)
    checkpoint_threads: int = 0
    naive_times_taken_as_utc: int = 0
    not_copied: list[str] = field(default_factory=list)


# --- reaching the source -----------------------------------------------------


def _container_engine() -> str:
    for candidate in ("docker", "podman"):
        if shutil.which(candidate) and subprocess.run([candidate, "info"], capture_output=True).returncode == 0:
            return candidate
    raise MigrationError("No running container engine (docker or podman) to read the Postgres container from.")


def source_url_from_container(name: str) -> URL:
    """The connection URL of a running Postgres container, from what the
    container itself says: its published port and POSTGRES_USER / PASSWORD / DB."""
    engine = _container_engine()

    def inspect_container(template: str) -> str:
        done = subprocess.run([engine, "container", "inspect", "-f", template, name], capture_output=True, text=True)
        if done.returncode != 0:
            raise MigrationError(f"No container named '{name}' ({engine} said: {done.stderr.strip() or 'not found'}).")
        return done.stdout.strip()

    if inspect_container("{{.State.Running}}") != "true":
        raise MigrationError(f"The container '{name}' is not running. Start it first: {engine} start {name}")
    env = dict(
        line.split("=", 1)
        for line in inspect_container("{{range .Config.Env}}{{println .}}{{end}}").splitlines()
        if "=" in line
    )
    port = inspect_container('{{range (index .HostConfig.PortBindings "5432/tcp")}}{{.HostPort}}{{end}}')
    if not port:
        raise MigrationError(f"'{name}' publishes no host port for 5432, so this machine cannot reach it.")
    missing = [key for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB") if not env.get(key)]
    if missing:
        raise MigrationError(f"'{name}' has no {', '.join(missing)} in its environment, so the connection cannot be derived.")
    return URL.create("postgresql+psycopg", env["POSTGRES_USER"], env["POSTGRES_PASSWORD"], "localhost", int(port), env["POSTGRES_DB"])


def _source_url(raw: str | URL) -> URL:
    url = make_url(raw)
    if url.drivername == "postgresql":  # bare scheme: SQLAlchemy would pick psycopg2, which is not what elstin used
        url = url.set(drivername="postgresql+psycopg")
    return url


def open_source(url: URL) -> Engine:
    if url.get_backend_name() == "postgresql":
        try:
            import psycopg  # noqa: F401
        except ModuleNotFoundError as exc:
            raise MigrationError(
                'Reading Postgres needs its driver, which elstin no longer installs: pip install "psycopg[binary]"'
            ) from exc
    return create_engine(url, future=True)


def read_snapshot(engine: Engine) -> Connection:
    """A connection whose reads all see one moment, and which cannot write."""
    connection = engine.connect()
    if engine.dialect.name == "postgresql":
        connection = connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
    return connection


# --- checking it is what we think it is --------------------------------------


def reflect_source(source: Connection) -> MetaData:
    meta = MetaData()
    meta.reflect(bind=source)
    return meta


def check_source(source: Connection, source_meta: MetaData, expected_revision: str = SOURCE_REVISION) -> None:
    if "alembic_version" not in source_meta.tables:
        raise MigrationError("The source has no alembic_version table, so it is not an elstin database.")
    revisions = [row[0] for row in source.execute(select(source_meta.tables["alembic_version"].c.version_num))]
    if revisions != [expected_revision]:
        found = ", ".join(revisions) or "none"
        raise MigrationError(
            f"The source database is at revision {found}, and this script reads {expected_revision}. "
            "Start the old checkout of elstin once against it (it migrates itself), or run `alembic upgrade head` "
            "from that checkout, then run this again. A schema that differs would not copy correctly."
        )


def plan_tables(source: Connection, source_meta: MetaData) -> tuple[list[TablePlan], list[str]]:
    plans: list[TablePlan] = []
    for target in Base.metadata.sorted_tables:  # parents before children
        if target.name not in source_meta.tables:
            raise MigrationError(f"The source has no table '{target.name}', though its revision says it should.")
        src = source_meta.tables[target.name]
        src_cols, dst_cols = {c.name for c in src.columns}, {c.name for c in target.columns}
        if src_cols != dst_cols:
            raise MigrationError(
                f"Table '{target.name}' differs: only in the source {sorted(src_cols - dst_cols) or '-'}, "
                f"only in elstin now {sorted(dst_cols - src_cols) or '-'}. Nothing was copied."
            )
        plans.append(TablePlan(target.name, src, target, source.execute(select(func.count()).select_from(src)).scalar_one()))
    unknown = sorted(name for name in source_meta.tables if name not in Base.metadata.tables and name not in NOT_COPIED_AS_ROWS)
    return plans, unknown


def ensure_target_empty(target: Connection) -> None:
    filled = {
        table.name: count
        for table in Base.metadata.sorted_tables
        if (count := target.execute(select(func.count()).select_from(table)).scalar_one())
    }
    if filled:
        listing = ", ".join(f"{name} ({count})" for name, count in filled.items())
        raise MigrationError(
            f"The SQLite database {database_path()} already has rows: {listing}. This script only fills an empty one; "
            "empty it with `python scripts/clear_db.py`, or point DATABASE_URL at a new file."
        )
    path = checkpoints_path()
    if path.exists():
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(path)) as conn:
            tables = {name for (name,) in conn.execute("select name from sqlite_master where type = 'table'")}
            if "checkpoints" in tables and conn.execute("select count(*) from checkpoints").fetchone()[0]:
                raise MigrationError(f"The checkpoint file {path} already has checkpoints; empty it with `python scripts/clear_db.py`.")


# --- copying the rows --------------------------------------------------------


def normalize_row(row: dict[str, Any], report: Report) -> dict[str, Any]:
    """A source row as the target's column types want it. Postgres hands back aware
    datetimes in the session's zone; they are stored as the same instant in UTC by
    the target's UtcDateTime. A naive one (a column that was `timestamp` rather than
    `timestamptz`) was written as UTC by elstin, so it is taken as UTC, and counted."""
    out = {}
    for name, value in row.items():
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
            report.naive_times_taken_as_utc += 1
        out[name] = value
    return out


def _chunks(source: Connection, table: Table) -> Iterator[list[dict[str, Any]]]:
    order = list(table.primary_key.columns) or list(table.columns)
    result = source.execute(select(table).order_by(*order), execution_options={"stream_results": True, "max_row_buffer": CHUNK_ROWS})
    for rows in result.mappings().partitions(CHUNK_ROWS):
        yield [dict(row) for row in rows]


def copy_rows(source: Connection, target: Connection, plans: list[TablePlan], report: Report, say=print) -> None:
    for plan in plans:
        copied = 0
        for rows in _chunks(source, plan.source):
            target.execute(plan.target.insert(), [normalize_row(row, report) for row in rows])
            copied += len(rows)
        report.copied[plan.name] = copied
        if copied:
            say(f"  {plan.name:<28}{copied:>9,} rows")


def verify(target: Connection, plans: list[TablePlan]) -> None:
    for plan in plans:
        got = target.execute(select(func.count()).select_from(plan.target)).scalar_one()
        if got != plan.rows:
            raise MigrationError(f"Table '{plan.name}': the source has {plan.rows} rows and {got} arrived. Nothing was kept.")
    broken = target.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if broken:
        first = ", ".join(f"{row[0]} row {row[1]} -> {row[2]}" for row in broken[:5])
        raise MigrationError(f"{len(broken)} row(s) point at rows that did not arrive ({first}). Nothing was kept.")


# --- copying the checkpoints -------------------------------------------------


def has_checkpoints(source_meta: MetaData) -> bool:
    return "checkpoints" in source_meta.tables


async def copy_checkpoints(source: Connection, source_meta: MetaData, source_url: URL, destination: Path, say=print) -> int:
    """Each thread's (story's) latest checkpoint, from Postgres into a fresh SQLite
    checkpoint file at `destination`. The two savers store a checkpoint differently
    (Postgres splits the channel values out into a blobs table; SQLite keeps them
    inline), so it is read whole through one saver and written through the other
    rather than copied as rows."""
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ModuleNotFoundError as exc:
        raise MigrationError(
            'Reading the checkpoints needs the old checkpointer: pip install langgraph-checkpoint-postgres "psycopg[binary]" '
            "(or run with --no-checkpoints, and every story's plan and run state is lost)."
        ) from exc
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    table = source_meta.tables["checkpoints"]
    threads = [(row[0], row[1]) for row in source.execute(select(table.c.thread_id, table.c.checkpoint_ns).distinct())]
    conninfo = source_url.set(drivername="postgresql").render_as_string(hide_password=False)
    copied = 0
    # No `setup()` on the Postgres saver: it creates tables, and the source is read-only.
    async with AsyncPostgresSaver.from_conn_string(conninfo) as old, aiosqlite.connect(destination) as conn:
        new = AsyncSqliteSaver(conn)
        await new.setup()
        for thread_id, namespace in threads:
            latest = await old.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}})
            if latest is None:
                continue
            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": namespace}}
            await new.aput(config, latest.checkpoint, latest.metadata, latest.checkpoint.get("channel_versions", {}))
            at = {"configurable": {**config["configurable"], "checkpoint_id": latest.checkpoint["id"]}}
            by_task: dict[str, list[tuple[str, Any]]] = {}
            for task_id, channel, value in latest.pending_writes or []:
                by_task.setdefault(task_id, []).append((channel, value))
            for task_id, writes in by_task.items():
                await new.aput_writes(at, writes, task_id)
            copied += 1
    say(f"  {'checkpoints (latest per story)':<28}{copied:>9,} stories")
    return copied


# --- the whole thing ---------------------------------------------------------


def run(
    source_url: str | URL,
    *,
    dry_run: bool = False,
    skip_checkpoints: bool = False,
    assume_yes: bool = False,
    expected_revision: str = SOURCE_REVISION,
    say=print,
) -> Report:
    url = _source_url(source_url)
    report = Report()
    warnings.filterwarnings("ignore", message=".*does \\*not\\* support Decimal objects natively.*")

    say(f"From  {url.render_as_string(hide_password=True)}")
    say(f"To    {database_path()}")
    source_engine = open_source(url)
    target_engine: Engine | None = None
    temp_checkpoints: Path | None = None
    try:
        with read_snapshot(source_engine) as source:
            # The source is checked before the target is touched at all, so a source
            # this script will not read leaves no SQLite file behind.
            source_meta = reflect_source(source)
            check_source(source, source_meta, expected_revision)
            plans, report.not_copied = plan_tables(source, source_meta)
            total = sum(plan.rows for plan in plans)
            with_checkpoints = has_checkpoints(source_meta) and not skip_checkpoints
            say(f"\n{total:,} rows in {sum(1 for p in plans if p.rows)} of {len(plans)} tables"
                + (", plus each story's latest checkpoint" if with_checkpoints else "")
                + (f"; tables elstin does not know, not copied: {', '.join(report.not_copied)}" if report.not_copied else "") + "\n")
            if dry_run:
                for plan in plans:
                    if plan.rows:
                        say(f"  {plan.name:<28}{plan.rows:>9,} rows")
                say("\nDry run: nothing was written, and the SQLite database was not looked at.")
                return report

            upgrade_to_head()  # a first launch would do the same; the target needs its tables
            target_engine = make_engine(get_database_url(), foreign_keys=False)  # checked at the end instead (verify)
            with target_engine.connect() as probe:
                ensure_target_empty(probe)
                probe.rollback()  # the probe's reads opened a write transaction; do not hold it
            if not assume_yes:
                if input("Copy it into that database? Type 'yes': ").strip().lower() != "yes":
                    raise MigrationError("Cancelled.")

            if with_checkpoints:
                temp_checkpoints = Path(tempfile.mkdtemp(prefix="elstin-checkpoints-")) / "checkpoints.db"
                report.checkpoint_threads = asyncio.run(copy_checkpoints(source, source_meta, url, temp_checkpoints, say))

            with target_engine.begin() as target:  # one transaction: all of it, or none
                copy_rows(source, target, plans, report, say)
                verify(target, plans)

            if temp_checkpoints is not None:
                final = checkpoints_path()
                final.parent.mkdir(parents=True, exist_ok=True)
                for leftover in (final.with_name(final.name + "-wal"), final.with_name(final.name + "-shm")):
                    leftover.unlink(missing_ok=True)
                os.replace(temp_checkpoints, final)
    finally:
        source_engine.dispose()
        if target_engine is not None:
            target_engine.dispose()
        if temp_checkpoints is not None:
            shutil.rmtree(temp_checkpoints.parent, ignore_errors=True)

    say(f"\nDone: {sum(report.copied.values()):,} rows" + (f" and {report.checkpoint_threads} stories' checkpoints" if report.checkpoint_threads else "") + " copied.")
    if report.naive_times_taken_as_utc:
        say(f"{report.naive_times_taken_as_utc} time value(s) had no timezone and were taken as UTC, which is how elstin wrote them.")
    say("The Postgres database was only read; keep it as a backup until you are sure. Start elstin as usual.")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--from", dest="source", metavar="URL", help="SQLAlchemy URL of the Postgres database (or set POSTGRES_URL).")
    where.add_argument("--from-container", metavar="NAME", help="A running Postgres container; its connection details are read from it.")
    parser.add_argument("--dry-run", action="store_true", help="Read the Postgres database and report what would be copied; touch no SQLite file.")
    parser.add_argument("--no-checkpoints", action="store_true", help="Skip LangGraph's checkpoints (each story's plan and run state).")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    try:
        if args.from_container:
            url: str | URL = source_url_from_container(args.from_container)
        elif args.source or os.environ.get("POSTGRES_URL"):
            url = args.source or os.environ["POSTGRES_URL"]
        else:
            parser.error("say where the Postgres database is: --from URL, --from-container NAME, or POSTGRES_URL")
        run(url, dry_run=args.dry_run, skip_checkpoints=args.no_checkpoints, assume_yes=args.yes)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
