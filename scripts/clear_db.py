"""Wipe every row from the app's own tables — for local/dev use.

Empties everything registered on `Base.metadata` (see
elstin/db/models/__init__.py), plus the LangGraph checkpointer's own tables
(`checkpoints`, `writes` — in the separate checkpoint file beside the database,
see elstin/db/engine.py's `checkpoints_path`; they carry every story's durable
planner/dev/verifier state, so leaving them out means a "cleared" story still
resumes mid-run instead of starting fresh). Rows go child tables first, so
foreign keys never get in the way. `alembic_version` is left untouched: this
clears data, not schema, so no migration needs to re-run afterwards. Only tables
that actually exist are cleared, since the checkpoint file isn't created until
the first story runs.

Stop the server first. The database is one file; this opens it like the app
does, but a run in flight would just write its state back.

Usage:
    python scripts/clear_db.py            # prompts for confirmation
    python scripts/clear_db.py --yes      # skips the prompt
    DATABASE_URL=sqlite:////path/to/elstin.db python scripts/clear_db.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

from sqlalchemy import inspect, text

from elstin.db.base import Base
from elstin.db.engine import checkpoints_path, database_path, get_engine
from elstin.db.models import *  # noqa: F401,F403 — registers every model on Base.metadata

CHECKPOINT_TABLES = ["checkpoints", "writes"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    engine = get_engine()
    existing = set(inspect(engine).get_table_names())
    # Children first: foreign keys are enforced.
    wanted = [table.name for table in reversed(Base.metadata.sorted_tables)]
    table_names = [name for name in wanted if name in existing]
    missing = [name for name in wanted if name not in existing]

    checkpoint_file = checkpoints_path()
    checkpoint_tables: list[str] = []
    if checkpoint_file.exists():
        with closing(sqlite3.connect(checkpoint_file)) as conn:
            have = {name for (name,) in conn.execute("select name from sqlite_master where type = 'table'")}
        checkpoint_tables = [name for name in CHECKPOINT_TABLES if name in have]

    if not table_names and not checkpoint_tables:
        print("No known tables exist yet — nothing to clear.")
        return

    print(f"About to DELETE every row from {len(table_names)} table(s) in {database_path()}:")
    for name in table_names:
        print(f"  - {name}")
    if checkpoint_tables:
        print(f"and from {len(checkpoint_tables)} table(s) in {checkpoint_file}:")
        for name in checkpoint_tables:
            print(f"  - {name}")
    if missing:
        print(f"(skipping, not created yet: {', '.join(missing)})")

    if not args.yes:
        reply = input("Type 'yes' to continue: ")
        if reply.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)

    with engine.begin() as conn:
        for name in table_names:
            conn.execute(text(f'DELETE FROM "{name}"'))
    if checkpoint_tables:
        with closing(sqlite3.connect(checkpoint_file)) as conn:
            for name in checkpoint_tables:
                conn.execute(f'DELETE FROM "{name}"')
            conn.commit()

    print("Done.")


if __name__ == "__main__":
    main()
