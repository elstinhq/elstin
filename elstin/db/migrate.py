"""Bring the database to the schema the code expects, in-process.

elstin is one process with one database file, so there is no separate migration
step to forget: the app (elstin/app.py) and the standalone daemon
(elstin/daemon/main.py) call this before they serve anything, and a first launch
creates the file and every table in it. `alembic upgrade head` still works for
doing it by hand.
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config

from elstin.resources import resource_root

# alembic.ini and alembic/ sit at the repo root, beside the `elstin` package (the
# Dockerfile copies both next to it); inside the Mac app's bundle they are at its root.
_ROOT = resource_root()


def alembic_config() -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    # alembic/env.py would otherwise replace the app's logging setup with alembic.ini's.
    config.attributes["configure_logger"] = False
    return config


def upgrade_to_head() -> None:
    command.upgrade(alembic_config(), "head")
