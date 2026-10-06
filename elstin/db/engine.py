"""The single required process env var lives here and nowhere else.

Every other setting is read from the `app_config` table (see
elstin/config/store.py) — DATABASE_URL is the one unavoidable exception,
since you can't store the DB connection string inside the DB you're trying
to reach.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class ConfigError(RuntimeError):
    pass


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ConfigError(
            "DATABASE_URL is not set. elstin takes no other environment "
            "configuration — set this one connection string (docker-compose "
            "wires it automatically) and configure everything else at "
            "/settings."
        )
    return url


def get_database_url() -> str:
    """Public accessor for callers outside the engine itself — e.g. the
    LangGraph Postgres checkpointer and the daemon, which need the raw
    connection string rather than a SQLAlchemy engine."""
    return _database_url()


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(_database_url(), pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
