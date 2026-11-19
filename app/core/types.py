"""Reusable SQLAlchemy column types."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """A timezone-aware UTC datetime that survives SQLite.

    SQLite has no native timestamp type, so SQLAlchemy hands back a naive
    datetime and every comparison against an aware one raises TypeError. Store
    naive-UTC, re-attach the tzinfo on read: the whole application then works in
    aware datetimes with no second code path per backend.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime passed to a UtcDateTime column")
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


def utcnow() -> dt.datetime:
    """Timezone-aware 'now'. Used as the Python-side column default."""
    return dt.datetime.now(dt.UTC)
