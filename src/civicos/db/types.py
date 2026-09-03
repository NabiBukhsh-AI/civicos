"""Portable SQLAlchemy column types.

CivicOS must run on both SQLite (a laptop, a CI runner, a single-office pilot)
and PostgreSQL (production). These type decorators pick the best native
representation per dialect and present one Python-side API to the models.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Sequence

from sqlalchemy import DateTime, Dialect, Float, String, Text, TypeDecorator
from sqlalchemy import JSON as SAJSON
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.mutable import MutableDict, MutableList

#: ``JSONB`` on PostgreSQL, plain ``JSON`` elsewhere.
JSONColumn = SAJSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

#: Mutable variants so in-place edits (``obj.meta["k"] = v``) are flushed.
MutableJSONDict = MutableDict.as_mutable(JSONColumn)
MutableJSONList = MutableList.as_mutable(JSONColumn)


class UTCDateTime(TypeDecorator):
    """``TIMESTAMPTZ`` that always hands Python a UTC-aware datetime.

    SQLite has no timezone support and returns naive values; without this the
    same query yields aware datetimes in production and naive ones in tests,
    which is a classic source of ``can't compare offset-naive and offset-aware``
    bugs inside SLA calculations.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class StringEnum(TypeDecorator):
    """Store a ``StrEnum`` as a VARCHAR.

    Native PostgreSQL enums require a migration for every new value; municipal
    taxonomies change often, so we trade the database-level constraint for
    application-level validation and painless evolution.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type, length: int = 48) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return str(value.value)
        return str(self.enum_class(value).value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # Tolerate rows written by a newer schema version.
            return value


class Vector(TypeDecorator):
    """Embedding column.

    On PostgreSQL with the ``pgvector`` extension this compiles to a real
    ``vector(N)`` column that supports ANN indexes. Everywhere else it degrades
    to JSON, and similarity is computed in Python by the numpy vector backend.
    """

    impl = SAJSON
    cache_ok = True

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions
        super().__init__()

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector as PGVector  # noqa: PLC0415

                return dialect.type_descriptor(PGVector(self.dimensions))
            except ImportError:
                return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(SAJSON())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        values = [float(v) for v in value]
        if dialect.name == "postgresql":
            try:
                import pgvector.sqlalchemy  # noqa: F401, PLC0415

                return values
            except ImportError:
                return values
        return values

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return [float(part) for part in value.strip("[]").split(",") if part]
        return [float(v) for v in value]


class Coordinate(TypeDecorator):
    """Latitude/longitude stored as a double with a sane precision guard."""

    impl = Float
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> float | None:
        if value is None:
            return None
        return round(float(value), 7)


def as_float_list(value: Sequence[float] | None) -> list[float]:
    return [float(v) for v in value] if value else []
