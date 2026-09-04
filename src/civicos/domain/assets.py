"""Municipal asset registry and inspections.

Knowing what you own is the precondition for maintaining it. The registry is
deliberately generic - one table with a type discriminator and a JSON
``specifications`` blob - because a town that starts with streetlights will
want drains next, and neither should require a migration.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from civicos.db.base import (
    Base,
    MetadataMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from civicos.db.types import (
    Coordinate,
    MutableJSONDict,
    MutableJSONList,
    StringEnum,
    UTCDateTime,
)
from civicos.domain.enums import AssetCondition, AssetType, Severity


class Asset(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base):
    """A physical thing the municipality owns or maintains."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_assets_tenant_code"),
        Index("ix_assets_tenant_type", "tenant_id", "asset_type"),
        Index("ix_assets_tenant_unit", "tenant_id", "admin_unit_id"),
        Index("ix_assets_tenant_condition", "tenant_id", "condition"),
        Index("ix_assets_geo", "tenant_id", "latitude", "longitude"),
        Index("ix_assets_next_inspection", "tenant_id", "next_inspection_due"),
    )

    code: Mapped[str] = mapped_column(String(48), nullable=False)
    """Stencilled on the pole / manhole cover so field staff can scan or type it."""
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(StringEnum(AssetType), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL")
    )

    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    geohash: Mapped[str | None] = mapped_column(String(12))
    address: Mapped[str | None] = mapped_column(Text)
    #: For linear assets (roads, drains): ``[[lat, lon], ...]``.
    geometry: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    condition: Mapped[AssetCondition] = mapped_column(
        StringEnum(AssetCondition), default=AssetCondition.GOOD, nullable=False
    )
    is_operational: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    installed_on: Mapped[date | None] = mapped_column(Date)
    expected_life_years: Mapped[int | None] = mapped_column(Integer)
    purchase_cost: Mapped[float | None] = mapped_column(Float)
    manufacturer: Mapped[str | None] = mapped_column(String(160))
    model: Mapped[str | None] = mapped_column(String(160))
    serial_number: Mapped[str | None] = mapped_column(String(120))

    #: Type-specific fields: wattage, pipe diameter, seating capacity...
    specifications: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )

    last_inspected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_inspection_due: Mapped[datetime | None] = mapped_column(UTCDateTime)
    inspection_interval_days: Mapped[int | None] = mapped_column(Integer)
    last_serviced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    open_issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_maintenance_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    photo_url: Mapped[str | None] = mapped_column(String(500))
    qr_payload: Mapped[str | None] = mapped_column(String(255))
    """Encoded into a sticker so a resident can scan a broken streetlight and
    file a pre-located report in two taps."""

    inspections: Mapped[list[AssetInspection]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetInspection.inspected_at.desc()",
    )

    @property
    def age_years(self) -> float | None:
        if not self.installed_on:
            return None
        from civicos.core.clock import local_date

        return round((local_date() - self.installed_on).days / 365.25, 1)

    @property
    def needs_inspection(self) -> bool:
        from civicos.core.clock import ensure_utc, utcnow

        return bool(self.next_inspection_due and ensure_utc(self.next_inspection_due) <= utcnow())

    @property
    def is_end_of_life(self) -> bool:
        age = self.age_years
        return bool(age and self.expected_life_years and age >= self.expected_life_years)


class AssetInspection(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A dated condition assessment, optionally produced by AI from a photo."""

    __tablename__ = "asset_inspections"
    __table_args__ = (Index("ix_asset_inspections_asset", "asset_id", "inspected_at"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="SET NULL")
    )
    inspector_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    inspected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    condition: Mapped[AssetCondition] = mapped_column(StringEnum(AssetCondition), nullable=False)
    severity: Mapped[Severity | None] = mapped_column(StringEnum(Severity))
    findings: Mapped[str | None] = mapped_column(Text)
    recommended_action: Mapped[str | None] = mapped_column(Text)
    estimated_repair_cost: Mapped[float | None] = mapped_column(Float)
    photo_keys: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    is_ai_assisted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_analysis: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )

    asset: Mapped[Asset] = relationship(back_populates="inspections")
