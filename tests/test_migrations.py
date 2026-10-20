"""The Alembic chain: the app brings its own database to the current schema, the
migrated schema is exactly what the models declare, and a migration that fails
leaves the database as it was.

A fresh database every test file builds on is itself the migrated one
(tests/conftest.py's `_migrated_database`), so a model that drifts from the
migration breaks the suite; `test_the_migrated_schema_is_what_the_models_declare`
is what says so by name."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from elstin.db.migrate import _ROOT, alembic_config, upgrade_to_head


@pytest.fixture()
def empty_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fresh" / "elstin.db"  # the directory does not exist yet either
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    return path


def _tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {name for (name,) in conn.execute("select name from sqlite_master where type = 'table'")}


def test_a_first_launch_creates_the_file_and_every_table(empty_database: Path):
    from elstin.db import models  # noqa: F401 — registers every model on Base.metadata
    from elstin.db.base import Base

    upgrade_to_head()

    assert set(Base.metadata.tables) <= _tables(empty_database)


def test_upgrading_a_database_already_at_head_changes_nothing(empty_database: Path):
    upgrade_to_head()
    with closing(sqlite3.connect(empty_database)) as conn:
        conn.execute("insert into app_config (key, value, secret, updated_at) values ('k', 'v', 0, '2026-01-01 00:00:00')")
        conn.commit()

    upgrade_to_head()

    with closing(sqlite3.connect(empty_database)) as conn:
        assert conn.execute("select value from app_config where key = 'k'").fetchall() == [("v",)]


def test_the_migrated_schema_is_what_the_models_declare(empty_database: Path):
    """`alembic check` raises when autogenerate would write a new revision: a
    model changed without one."""
    upgrade_to_head()

    command.check(alembic_config())


def test_a_migration_that_fails_halfway_leaves_the_database_as_it_was(empty_database: Path, tmp_path: Path):
    scripts = tmp_path / "alembic"
    shutil.copytree(_ROOT / "alembic", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "20261012_9999_fails_halfway.py").write_text(
        '"""creates a table, then fails"""\n'
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "revision = '9999'\ndown_revision = '0001'\nbranch_labels = None\ndepends_on = None\n"
        "def upgrade():\n"
        "    op.create_table('half_made', sa.Column('id', sa.Integer(), primary_key=True))\n"
        "    op.execute('SELECT * FROM a_table_that_does_not_exist')\n"
        "def downgrade():\n    pass\n"
    )
    config = alembic_config()
    config.set_main_option("script_location", str(scripts))
    command.upgrade(config, "0001")

    with pytest.raises(Exception, match="a_table_that_does_not_exist"):
        command.upgrade(config, "9999")

    assert "half_made" not in _tables(empty_database)
    with closing(sqlite3.connect(empty_database)) as conn:
        assert conn.execute("select version_num from alembic_version").fetchall() == [("0001",)]
