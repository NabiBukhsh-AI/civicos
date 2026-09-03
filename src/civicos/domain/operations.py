"""Operational plumbing: audit log, outbound notifications, saved analytics."""

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
from sqlalchemy.orm import Mapped, mapped_column

from civicos.db.base import (
    Base,
    MetadataMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from civicos.db.types import MutableJSONDict, MutableJSONList, StringEnum, UTCDateTime
from civicos.domain.enums import (
    AuditAction,
    NotificationChannel,
    NotificationStatus,
)


class AuditLog(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Append-only record of consequential actions.

    Written for every state change, permission change, export and AI invocation.
    ``before``/``after`` hold only the changed fields, which keeps the table
    small enough to retain for years without partitioning.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_audit_logs_actor", "tenant_id", "actor_id", "created_at"),
    )

    action: Mapped[AuditAction] = mapped_column(StringEnum(AuditAction), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    entity_label: Mapped[str | None] = mapped_column(String(255))

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_label: Mapped[str] = mapped_column(String(160), default="system", nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(48))

    summary: Mapped[str | None] = mapped_column(String(500))
    before: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    after: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)

    request_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Notification(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One outbound message on one channel.

    Rows are created queued and updated by the delivery worker, so a supervisor
    can always answer "did the complainant actually get told?".
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_tenant_status", "tenant_id", "status"),
        Index("ix_notifications_recipient", "tenant_id", "user_id", "read_at"),
        Index("ix_notifications_entity", "entity_type", "entity_id"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    destination: Mapped[str | None] = mapped_column(String(160))
    """Phone/email used when the recipient has no account (anonymous reporters)."""

    channel: Mapped[NotificationChannel] = mapped_column(
        StringEnum(NotificationChannel), default=NotificationChannel.IN_APP, nullable=False
    )
    status: Mapped[NotificationStatus] = mapped_column(
        StringEnum(NotificationStatus), default=NotificationStatus.QUEUED, nullable=False
    )

    template_key: Mapped[str | None] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    action_url: Mapped[str | None] = mapped_column(String(500))

    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))

    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(120))


class DailyMetric(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Pre-aggregated daily counters.

    Dashboards over a multi-year complaints table get slow; a nightly rollup
    keeps the executive view instant and gives us a stable series even after
    records are archived.
    """

    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "metric_date", "dimension", "dimension_value",
            name="uq_daily_metrics_scope",
        ),
        Index("ix_daily_metrics_tenant_date", "tenant_id", "metric_date"),
    )

    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), default="overall", nullable=False)
    """overall | category | department | admin_unit | channel"""
    dimension_value: Mapped[str] = mapped_column(String(64), default="all", nullable=False)

    issues_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    issues_resolved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    issues_open_end_of_day: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sla_response_met: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sla_response_breached: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sla_resolution_met: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sla_resolution_breached: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_resolution_hours: Mapped[float | None] = mapped_column(Float)
    avg_first_response_hours: Mapped[float | None] = mapped_column(Float)
    avg_satisfaction: Mapped[float | None] = mapped_column(Float)
    work_orders_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ai_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extra_counters: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )


class SavedView(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """A named filter set - "my ward's overdue drainage complaints".

    Small feature, large effect: it is how supervisors actually work, and it
    doubles as the subscription target for scheduled digest emails.
    """

    __tablename__ = "saved_views"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_id", "name", name="uq_saved_views_owner_name"),
    )

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    resource: Mapped[str] = mapped_column(String(48), default="issues", nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    digest_frequency: Mapped[str | None] = mapped_column(String(16))
    """none | daily | weekly - drives the scheduled digest worker."""


class WebhookEndpoint(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Outbound integration hook.

    Towns rarely run only one system; this lets CivicOS push events into an
    existing ERP, a WhatsApp gateway or a provincial dashboard without anyone
    polling the API.
    """

    __tablename__ = "webhook_endpoints"
    __table_args__ = (Index("ix_webhook_endpoints_tenant_active", "tenant_id", "is_active"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_delivery_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
