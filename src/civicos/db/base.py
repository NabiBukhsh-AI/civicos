"""Declarative base and the mixins every table is composed from."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, MetaData, String, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from civicos.db.types import MutableJSONDict, UTCDateTime

# Deterministic constraint names keep Alembic autogenerate diffs readable and
# make `ALTER TABLE ... DROP CONSTRAINT` scripts portable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        exclude = exclude or set()
        return {
            column.key: getattr(self, column.key)
            for column in self.__table__.columns
            if column.key not in exclude
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    """UUIDv4 primary keys.

    Chosen over serial integers so that records can be created offline by field
    apps and synced later without id collisions, and so that public reference
    URLs do not leak how many complaints a town receives.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, sort_order=-100
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False, index=True, sort_order=90
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        sort_order=91,
    )


class SoftDeleteMixin:
    """Municipal records are legal records: we archive, we do not delete."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True, index=True, sort_order=92
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class TenantMixin:
    """Scopes a row to one municipality.

    Every tenant-owned table carries this; the repository layer refuses to build
    a query without a tenant filter, which is the single most important
    invariant in a multi-tenant civic system.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            Uuid(as_uuid=True),
            ForeignKey("municipalities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            sort_order=-90,
        )


class ActorStampMixin:
    """Who created / last touched a row."""

    @declared_attr
    def created_by_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(
            Uuid(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            sort_order=93,
        )

    @declared_attr
    def updated_by_id(cls) -> Mapped[uuid.UUID | None]:  # noqa: N805
        return mapped_column(
            Uuid(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            sort_order=94,
        )


class MetadataMixin:
    """Free-form extension point.

    Towns always have one extra field nobody anticipated - a katchi abadi code,
    a UC-level scheme number. ``extra`` lets them store it without a migration,
    and ``tags`` gives cheap faceting.
    """

    extra: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False, sort_order=95
    )


class SlugMixin:
    slug: Mapped[str] = mapped_column(String(80), nullable=False, index=True, sort_order=-80)


def tenant_scoped_index(table_name: str, *columns: str, unique: bool = False) -> Index:
    """Helper for the ``(tenant_id, ...)`` composite indexes we use everywhere."""
    name = f"ix_{table_name}_tenant_{'_'.join(columns)}"
    return Index(name, "tenant_id", *columns, unique=unique)
