from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from elstin.db.base import Base
from elstin.db.engine import get_database_url, make_engine
from elstin.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata

config = context.config

# Not when the app runs the migration in-process (elstin/db/migrate.py): fileConfig
# would replace the app's own logging setup with alembic.ini's.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    # Not `disable_existing_loggers` (fileConfig's default): a migration run
    # in-process (the tests do) would otherwise silence every logger created
    # before it, and a later test reading them through caplog saw nothing.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = get_database_url()
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata

# SQLite cannot ALTER most things in place; batch mode rebuilds the table
# (create new, copy, drop old, rename) behind the same `op.*` calls, so write
# migrations with `op.batch_alter_table`. It is a no-op for everything SQLite
# can do natively.
# transactional_ddl: SQLite can roll back CREATE/ALTER/DROP, and elstin/db/engine.py
# opens its own transaction rather than the driver's, so a migration that fails
# halfway leaves the database as it was instead of half-changed.
_CONTEXT = dict(target_metadata=target_metadata, render_as_batch=True, compare_type=True, transactional_ddl=True)


def run_migrations_offline() -> None:
    context.configure(url=database_url, literal_binds=True, dialect_opts={"paramstyle": "named"}, **_CONTEXT)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Foreign keys off: a batch migration drops and recreates a table that other
    # tables point at, which SQLite refuses while enforcement is on.
    engine = make_engine(database_url, foreign_keys=False)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, **_CONTEXT)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
