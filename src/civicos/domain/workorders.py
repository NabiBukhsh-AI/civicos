"""Field operations: crews, work orders and the material they consume."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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
from civicos.domain.enums import (
    CrewShift,
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)
from civicos.domain.identity import User


class Crew(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """A field team: sanitation gang, water tanker crew, electrician pair."""

    __tablename__ = "crews"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_crews_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )
    supervisor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    shift: Mapped[CrewShift] = mapped_column(
        StringEnum(CrewShift), default=CrewShift.MORNING, nullable=False
    )
    #: Admin unit ids this crew normally covers.
    coverage_unit_ids: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    #: Category slugs the crew is competent for; drives auto-dispatch.
    skills: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    vehicle_registration: Mapped[str | None] = mapped_column(String(32))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    capacity_per_day: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    members: Mapped[list[CrewMember]] = relationship(
        back_populates="crew", cascade="all, delete-orphan", lazy="selectin"
    )
    supervisor: Mapped[User | None] = relationship(foreign_keys=[supervisor_id])


class CrewMember(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "crew_members"
    __table_args__ = (UniqueConstraint("crew_id", "user_id", name="uq_crew_members_crew_user"),)

    crew_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("crews.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_in_crew: Mapped[str | None] = mapped_column(String(64))
    is_lead: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    crew: Mapped[Crew] = relationship(back_populates="members")
    user: Mapped[User] = relationship(lazy="joined")


class WorkOrder(
    UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base
):
    """A unit of field work.

    Work orders are decoupled from issues on purpose: one pothole complaint can
    spawn three orders (survey, patch, re-inspect), and preventive maintenance
    orders exist with no complaint behind them at all.
    """

    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_work_orders_tenant_reference"),
        Index("ix_work_orders_tenant_status", "tenant_id", "status"),
        Index("ix_work_orders_tenant_crew", "tenant_id", "crew_id"),
        Index("ix_work_orders_tenant_scheduled", "tenant_id", "scheduled_for"),
        Index("ix_work_orders_issue", "issue_id"),
    )

    reference: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)

    issue_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issues.id", ondelete="SET NULL")
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )

    order_type: Mapped[WorkOrderType] = mapped_column(
        StringEnum(WorkOrderType), default=WorkOrderType.CORRECTIVE, nullable=False
    )
    status: Mapped[WorkOrderStatus] = mapped_column(
        StringEnum(WorkOrderStatus), default=WorkOrderStatus.DRAFT, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        StringEnum(Priority), default=Priority.NORMAL, nullable=False
    )

    crew_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("crews.id", ondelete="SET NULL")
    )
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    address: Mapped[str | None] = mapped_column(Text)

    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    estimated_hours: Mapped[float | None] = mapped_column(Float)
    actual_hours: Mapped[float | None] = mapped_column(Float)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    actual_cost: Mapped[float | None] = mapped_column(Float)

    checklist: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    """``[{"label": "...", "done": false, "note": null}]`` - completed on the phone."""
    completion_note: Mapped[str | None] = mapped_column(Text)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    requires_verification: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Signed off in the field: name + timestamp + optional signature image key.
    signoff: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)

    crew: Mapped[Crew | None] = relationship(lazy="joined")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id], lazy="joined")
    updates: Mapped[list[WorkOrderUpdate]] = relationship(
        back_populates="work_order",
        cascade="all, delete-orphan",
        order_by="WorkOrderUpdate.created_at",
    )
    materials: Mapped[list[MaterialUsage]] = relationship(
        back_populates="work_order", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.status not in {
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.VERIFIED,
            WorkOrderStatus.CANCELLED,
        }

    @property
    def is_overdue(self) -> bool:
        from civicos.core.clock import ensure_utc, utcnow

        return bool(self.due_at and self.is_open and ensure_utc(self.due_at) < utcnow())


class WorkOrderUpdate(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Progress note from the field, optionally geotagged.

    Field staff often work offline; ``recorded_at`` is when the phone captured
    the update, ``created_at`` is when it reached the server.
    """

    __tablename__ = "work_order_updates"
    __table_args__ = (Index("ix_work_order_updates_order", "work_order_id", "created_at"),)

    work_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[WorkOrderStatus | None] = mapped_column(StringEnum(WorkOrderStatus))
    note: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    recorded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    payload: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)

    work_order: Mapped[WorkOrder] = relationship(back_populates="updates")


class MaterialUsage(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """What a job actually consumed - the input to cost-per-repair analytics."""

    __tablename__ = "material_usage"
    __table_args__ = (Index("ix_material_usage_order", "work_order_id"),)

    work_order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False
    )
    item_name: Mapped[str] = mapped_column(String(160), nullable=False)
    item_code: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(24), default="unit", nullable=False)
    unit_cost: Mapped[float | None] = mapped_column(Float)

    work_order: Mapped[WorkOrder] = relationship(back_populates="materials")

    @property
    def total_cost(self) -> float | None:
        return None if self.unit_cost is None else round(self.unit_cost * self.quantity, 2)
