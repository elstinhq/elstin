"""Column types shared by the models.

SQLite has no timestamp-with-timezone type: it stores whatever wall-clock text
it is given and returns a naive datetime. elstin compares stored times with
`datetime.now(timezone.utc)` in dozens of places, so a naive value would
either raise ("can't compare offset-naive and offset-aware datetimes") or —
worse — be read back as local time, shifting a retry or a deadline by the
machine's UTC offset.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """A timezone-aware datetime, stored as UTC.

    Written: converted to UTC first, so the stored text sorts and compares
    correctly whatever zone it came in. A naive value is a bug at the call site
    (which zone is it in?) and is refused rather than guessed at.
    Read: the UTC zone is put back."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime {value!r}: elstin stores timezone-aware datetimes only")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)
