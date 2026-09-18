"""Wipe every row from the app's own tables — for local/dev use.

Truncates everything registered on `Base.metadata` (see
forge/db/models/__init__.py), plus the LangGraph checkpointer's own
tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes` — created by
`AsyncPostgresSaver.setup()`, see forge/orchestration/graph.py; they carry
every story's durable planner/dev/verifier state, so leaving them out
means a "cleared" story still resumes mid-run instead of starting fresh).
Uses CASCADE, so foreign-key ordering never matters, and RESTART IDENTITY
so serial ids start back at 1. Both schema-version tables — Alembic's
`alembic_version` and the checkpointer's own `checkpoint_migrations` — are
left untouched: this clears data, not schema, so neither migrations nor
`setup()` need to re-run afterwards. Only tables that actually exist are
truncated, since the checkpointer's tables aren't created until the first
story runs.

Usage:
    export DATABASE_URL=postgresql+psycopg://forge:forge@localhost:5433/forge
    python scripts/clear_db.py            # prompts for confirmation
    python scripts/clear_db.py --yes      # skips the prompt
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from forge.db.base import Base
from forge.db.engine import get_engine
from forge.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata

CHECKPOINT_TABLES = ["checkpoints", "checkpoint_blobs", "checkpoint_writes"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    engine = get_engine()
    url = make_url(engine.url)

    existing = set(inspect(engine).get_table_names())
    wanted = [table.name for table in Base.metadata.sorted_tables] + CHECKPOINT_TABLES
    table_names = [name for name in wanted if name in existing]
    missing = [name for name in wanted if name not in existing]

    if not table_names:
        print("No known tables exist yet — nothing to clear.")
        return

    print(f"About to TRUNCATE {len(table_names)} table(s) on {url.host}:{url.port}/{url.database}:")
    for name in table_names:
        print(f"  - {name}")
    if missing:
        print(f"(skipping, not created yet: {', '.join(missing)})")

    if not args.yes:
        reply = input("Type 'yes' to continue: ")
        if reply.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)

    quoted = ", ".join(f'"{name}"' for name in table_names)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))

    print("Done.")


if __name__ == "__main__":
    main()
