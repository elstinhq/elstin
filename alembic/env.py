from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from elstin.db.base import Base
from elstin.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata

config = context.config

if config.config_file_name is not None:
    # Not `disable_existing_loggers` (fileConfig's default): a migration run
    # in-process (the tests do) would otherwise silence every logger created
    # before it, and a later test reading them through caplog saw nothing.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set — alembic needs it the same way the app does "
        "(see elstin/db/engine.py). No .env file; export it or let docker-compose wire it."
    )
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
