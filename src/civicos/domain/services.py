"""Citizen services: permits, licences, NOCs, and recurring service schedules.

This is the "counter" side of a municipality - the queue a resident joins to
get a trade licence, a building NOC, a water tanker, or a death certificate.
Modelling it as a configurable ``ServiceType`` plus a generic application means
a town can publish a new service by inserting a row, not by shipping code.
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
from civicos.domain.enums import (
    ScheduleFrequency,
    ServiceApplicationStatus,
    Visibility,
)
from civicos.domain.identity import User


class ServiceType(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """A service the municipality offers to residents or businesses."""

    __tablename__ = "service_types"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_service_types_tenant_slug"),
        Index("ix_service_types_tenant_active", "tenant_id", "is_active"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )

    #: JSON-schema-like field definitions rendered dynamically by the client.
    form_schema: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    #: ``[{"code": "cnic", "label": "CNIC copy", "required": true}]``
    required_documents: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    #: Ordered review steps, e.g. ``["clerk", "inspection", "department_head"]``.
    approval_steps: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )

    fee_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fee_description: Mapped[str | None] = mapped_column(String(255))
    processing_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    validity_days: Mapped[int | None] = mapped_column(Integer)
    requires_inspection: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_payment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    icon: Mapped[str | None] = mapped_column(String(64))


class ServiceApplication(
    UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base
):
    """One resident's application for one service."""

    __tablename__ = "service_applications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "reference", name="uq_service_applications_tenant_reference"
        ),
        Index("ix_service_applications_tenant_status", "tenant_id", "status"),
        Index("ix_service_applications_applicant", "tenant_id", "applicant_id"),
        Index("ix_service_applications_due", "tenant_id", "due_at"),
    )

    reference: Mapped[str] = mapped_column(String(24), nullable=False)
    service_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("service_types.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[ServiceApplicationStatus] = mapped_column(
        StringEnum(ServiceApplicationStatus),
        default=ServiceApplicationStatus.DRAFT,
        nullable=False,
    )

    applicant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    applicant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    applicant_phone: Mapped[str | None] = mapped_column(String(32))
    applicant_email: Mapped[str | None] = mapped_column(String(160))
    #: Stored as a one-way hash; the plain number never touches the database.
    applicant_id_hash: Mapped[str | None] = mapped_column(String(64))

    #: Answers keyed by the ``form_schema`` field codes.
    form_data: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    premises_address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )

    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    info_request: Mapped[str | None] = mapped_column(Text)

    fee_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fee_paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payment_reference: Mapped[str | None] = mapped_column(String(80))
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    certificate_number: Mapped[str | None] = mapped_column(String(64))
    issued_on: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    certificate_key: Mapped[str | None] = mapped_column(String(500))

    #: AI pre-check: completeness, obvious mismatches, missing documents.
    ai_review: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )

    service_type: Mapped[ServiceType] = relationship(lazy="joined")
    applicant: Mapped[User | None] = relationship(foreign_keys=[applicant_id], lazy="joined")
    documents: Mapped[list["ApplicationDocument"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationEvent.created_at",
    )

    @property
    def is_open(self) -> bool:
        return self.status not in {
            ServiceApplicationStatus.APPROVED,
            ServiceApplicationStatus.ISSUED,
            ServiceApplicationStatus.REJECTED,
            ServiceApplicationStatus.EXPIRED,
            ServiceApplicationStatus.CANCELLED,
        }

    @property
    def is_overdue(self) -> bool:
        from civicos.core.clock import ensure_utc, utcnow

        return bool(self.due_at and self.is_open and ensure_utc(self.due_at) < utcnow())


class ApplicationDocument(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "application_documents"
    __table_args__ = (Index("ix_application_documents_application", "application_id"),)

    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("service_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_code: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(255))

    application: Mapped[ServiceApplication] = relationship(back_populates="documents")


class ApplicationEvent(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Audit trail for an application - the paper file, digitised."""

    __tablename__ = "application_events"
    __table_args__ = (Index("ix_application_events_application", "application_id", "created_at"),)

    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("service_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_label: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(48))
    to_status: Mapped[str | None] = mapped_column(String(48))
    note: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    application: Mapped[ServiceApplication] = relationship(back_populates="events")


class ServiceSchedule(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """A recurring municipal service round.

    Publishing "your street is swept on Tuesday and Friday, water comes at
    06:00 on alternate days" is one of the highest-value, lowest-cost things a
    town can do: it removes a whole class of complaints and makes missed rounds
    visible and reportable.
    """

    __tablename__ = "service_schedules"
    __table_args__ = (
        Index("ix_service_schedules_tenant_unit", "tenant_id", "admin_unit_id"),
        Index("ix_service_schedules_tenant_kind", "tenant_id", "service_kind"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    service_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    """waste_collection | water_supply | street_sweeping | drain_cleaning | fogging | tanker_round"""
    description: Mapped[str | None] = mapped_column(Text)

    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="CASCADE")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    crew_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("crews.id", ondelete="SET NULL")
    )

    frequency: Mapped[ScheduleFrequency] = mapped_column(
        StringEnum(ScheduleFrequency), default=ScheduleFrequency.DAILY, nullable=False
    )
    #: 0=Monday ... 6=Sunday
    days_of_week: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    start_time: Mapped[str | None] = mapped_column(String(5))
    end_time: Mapped[str | None] = mapped_column(String(5))
    route_points: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )

    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    last_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reliability_score: Mapped[float] = mapped_column(Coordinate, default=1.0, nullable=False)
    """Rolling ratio of rounds completed on time - published so residents can see it."""

    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.PUBLIC, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
