"""Rows for every table of a schema, made up from nothing but its column types.

Used to fill a database the way a long-lived one looks — every table has rows,
every column type is exercised (JSON, Decimal, booleans, NULLs, unicode, times in
a zone other than UTC), foreign keys point at rows that exist, a table that
references itself points at an earlier row — without a fixture per table that
would have to be kept in step with the models. Not a test module: tests and
one-off scripts import `fill`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Connection, DateTime, Enum, Integer, LargeBinary, MetaData, Numeric, String, Table, Text
from sqlalchemy.types import TypeDecorator

IST = timezone(timedelta(hours=5, minutes=30))


def _value(table: Table, column, i: int) -> Any:
    kind = column.type
    while isinstance(kind, TypeDecorator):  # UtcDateTime and the like: what it stores is what to make
        kind = kind.impl_instance
    label = f"{table.name}.{column.name}#{i}"
    if isinstance(kind, Boolean):
        return i % 2 == 0
    if isinstance(kind, Integer):  # BigInteger is one
        return i * 10 + 7
    if isinstance(kind, Numeric):
        return Decimal(i) + Decimal("0.25")
    if isinstance(kind, JSON):
        return {"i": i, "nested": [1, "two", {"three": 3.5}], "unicode": "ü✓", "nothing": None}
    if isinstance(kind, DateTime):
        return datetime(2026, 10, 1 + i, 15, 30, 45, 123456, tzinfo=IST)  # 10:00:45 UTC, not 15:30
    if isinstance(kind, LargeBinary):
        return bytes([i, 255, 0, 7])
    if isinstance(kind, Enum):
        return kind.enums[i % len(kind.enums)]
    if isinstance(kind, (String, Text)):
        text = f"{label} naïve — 日本語 ✓" if not getattr(kind, "length", None) or kind.length >= 48 else label
        return text[: kind.length] if getattr(kind, "length", None) else text
    raise NotImplementedError(f"no synthetic value for {table.name}.{column.name}: {kind!r}")


def fill(connection: Connection, metadata: MetaData, rows_per_table: int = 3) -> dict[str, int]:
    """Insert `rows_per_table` rows into every table of `metadata`, parents first.
    Returns how many rows each table got. The caller commits."""
    made: dict[str, list[dict[str, Any]]] = {}
    for table in metadata.sorted_tables:
        made[table.name] = []
        for i in range(rows_per_table):
            row: dict[str, Any] = {}
            skipped_serial = []
            for column in table.columns:
                if column.foreign_keys:
                    parent = next(iter(column.foreign_keys)).column
                    options = made.get(parent.table.name, [])
                    if options:
                        row[column.name] = options[min(i, len(options) - 1)][parent.name]
                    elif column.nullable:
                        row[column.name] = None
                    else:
                        raise RuntimeError(f"{table.name}.{column.name} needs a row in {parent.table.name}, which has none")
                elif column.primary_key and isinstance(column.type, Integer) and not isinstance(column.type, Boolean):
                    skipped_serial.append(column)  # the database numbers it
                elif column.nullable and i == 1:
                    row[column.name] = None  # a NULL in every nullable column, in the middle row
                else:
                    row[column.name] = _value(table, column, i)
            result = connection.execute(table.insert().values(**row))
            for column, value in zip(table.primary_key.columns, result.inserted_primary_key or ()):
                row.setdefault(column.name, value)
            made[table.name].append(row)
    return {name: len(rows) for name, rows in made.items()}
