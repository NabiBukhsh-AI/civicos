"""Civic issues - the complaint / service-request record at the centre of CivicOS."""

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

from civicos.core.config import get_settings
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
    Vector,
)
from civicos.domain.enums import (
    IssueEventType,
    IssueStatus,
    Priority,
    ReportChannel,
    Sentiment,
    Severity,
    SLAState,
    Visibility,
)
from civicos.domain.identity import User
from civicos.domain.tenancy import AdminUnit, Department, IssueCategory

_EMBEDDING_DIMENSIONS = get_settings().ai.embedding_dimensions


class Issue(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base):
    """One civic problem reported by a resident, an inspector or a sensor.

    The record deliberately keeps three parallel views of the same complaint:

    * what the reporter said (``description``, ``description_original``),
    * what the AI concluded (``ai_*`` columns, kept separate and never
      overwriting human input), and
    * what the municipality decided (``category_id``, ``priority``, workflow).

    Keeping them apart is what makes the AI auditable: a supervisor can always
    see that the model suggested "Sanitation / high" and that a human moved it.
    """

    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_issues_tenant_reference"),
        Index("ix_issues_tenant_status", "tenant_id", "status"),
        Index("ix_issues_tenant_category", "tenant_id", "category_id"),
        Index("ix_issues_tenant_department", "tenant_id", "department_id"),
        Index("ix_issues_tenant_created", "tenant_id", "created_at"),
        Index("ix_issues_tenant_assignee", "tenant_id", "assigned_to_id"),
        Index("ix_issues_tenant_unit", "tenant_id", "admin_unit_id"),
        Index("ix_issues_geo", "tenant_id", "latitude", "longitude"),
        Index("ix_issues_geohash", "tenant_id", "geohash"),
        Index("ix_issues_fingerprint", "tenant_id", "fingerprint"),
        Index("ix_issues_resolution_due", "tenant_id", "resolution_due_at"),
    )

    # -- identity -------------------------------------------------------------
    reference: Mapped[str] = mapped_column(String(24), nullable=False)
    """Human-quotable code, e.g. ``ABC-7K2QF4``; what a resident reads out on the phone."""

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    description_original: Mapped[str | None] = mapped_column(Text)
    """Verbatim text as submitted, before translation or PII redaction."""
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    # -- classification -------------------------------------------------------
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issue_categories.id", ondelete="SET NULL")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    status: Mapped[IssueStatus] = mapped_column(
        StringEnum(IssueStatus), default=IssueStatus.SUBMITTED, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        StringEnum(Priority), default=Priority.NORMAL, nullable=False
    )
    severity: Mapped[Severity | None] = mapped_column(StringEnum(Severity))
    channel: Mapped[ReportChannel] = mapped_column(
        StringEnum(ReportChannel), default=ReportChannel.WEB, nullable=False
    )
    tags: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    # -- location -------------------------------------------------------------
    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    geohash: Mapped[str | None] = mapped_column(String(12))
    location_accuracy_m: Mapped[float | None] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(Text)
    landmark: Mapped[str | None] = mapped_column(String(255))
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )

    # -- reporter -------------------------------------------------------------
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reporter_name: Mapped[str | None] = mapped_column(String(160))
    reporter_phone: Mapped[str | None] = mapped_column(String(32))
    reporter_email: Mapped[str | None] = mapped_column(String(160))
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    contact_consent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # -- ownership ------------------------------------------------------------
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    assigned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # -- AI (advisory, never authoritative) -----------------------------------
    ai_category_slug: Mapped[str | None] = mapped_column(String(64))
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    ai_priority: Mapped[Priority | None] = mapped_column(StringEnum(Priority))
    ai_severity: Mapped[Severity | None] = mapped_column(StringEnum(Severity))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_analysis: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    ai_triaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ai_model: Mapped[str | None] = mapped_column(String(80))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMBEDDING_DIMENSIONS))
    """Semantic vector of title+description, used for duplicate detection and search."""

    # -- deduplication --------------------------------------------------------
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issues.id", ondelete="SET NULL")
    )
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confirmations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    """How many other residents said "me too" - the strongest prioritisation signal we have."""

    # -- SLA ------------------------------------------------------------------
    sla_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sla_policies.id", ondelete="SET NULL")
    )
    response_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_response_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sla_response_state: Mapped[SLAState] = mapped_column(
        StringEnum(SLAState), default=SLAState.ON_TRACK, nullable=False
    )
    sla_resolution_state: Mapped[SLAState] = mapped_column(
        StringEnum(SLAState), default=SLAState.ON_TRACK, nullable=False
    )
    escalation_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # -- outcome --------------------------------------------------------------
    resolution_note: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    satisfaction_rating: Mapped[int | None] = mapped_column(Integer)
    satisfaction_comment: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[Sentiment | None] = mapped_column(StringEnum(Sentiment))

    # -- publication ----------------------------------------------------------
    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.PUBLIC, nullable=False
    )
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flag_reason: Mapped[str | None] = mapped_column(String(255))
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # -- relationships --------------------------------------------------------
    category: Mapped[IssueCategory | None] = relationship(lazy="joined")
    department: Mapped[Department | None] = relationship(lazy="joined")
    admin_unit: Mapped[AdminUnit | None] = relationship(lazy="joined")
    reporter: Mapped[User | None] = relationship(foreign_keys=[reporter_id], lazy="joined")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assigned_to_id], lazy="joined")
    duplicate_of: Mapped["Issue | None"] = relationship(remote_side="Issue.id")

    attachments: Mapped[list["IssueAttachment"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", order_by="IssueAttachment.created_at"
    )
    events: Mapped[list["IssueEvent"]] = relationship(
        back_populates="issue",
        cascade="all, delete-orphan",
        order_by="IssueEvent.created_at",
    )
    comments: Mapped[list["IssueComment"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", order_by="IssueComment.created_at"
    )
    followers: Mapped[list["IssueFollower"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan"
    )

    # -- derived --------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.status.is_open if isinstance(self.status, IssueStatus) else False

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def age_hours(self) -> float:
        from civicos.core.clock import ensure_utc, utcnow

        return (utcnow() - ensure_utc(self.created_at)).total_seconds() / 3600

    @property
    def is_breached(self) -> bool:
        return SLAState.BREACHED in {self.sla_response_state, self.sla_resolution_state}

    def public_reporter_name(self) -> str:
        if self.is_anonymous:
            return "Anonymous resident"
        return self.reporter_name or (self.reporter.display() if self.reporter else "Resident")


class IssueAttachment(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Evidence attached to an issue: photos, video, voice notes, documents.

    EXIF-derived location and capture time are stored as first-class columns
    because they are the strongest evidence that a photo was really taken at
    the reported spot - the "before/after" pair is what closes a work order.
    """

    __tablename__ = "issue_attachments"
    __table_args__ = (Index("ix_issue_attachments_issue", "issue_id", "created_at"),)

    issue_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(String(16), default="photo", nullable=False)
    """photo | video | audio | document"""
    stage: Mapped[str] = mapped_column(String(16), default="report", nullable=False)
    """report | before | progress | after | verification"""

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    public_url: Mapped[str | None] = mapped_column(String(500))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)

    captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    device: Mapped[str | None] = mapped_column(String(120))
    exif: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)

    ai_analysis: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    ai_caption: Mapped[str | None] = mapped_column(Text)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    issue: Mapped[Issue] = relationship(back_populates="attachments")

    @property
    def has_geotag(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class IssueEvent(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Append-only timeline entry. Never updated, never deleted.

    This is the record that answers "who moved this complaint, when, and why" -
    the question every municipal audit eventually asks.
    """

    __tablename__ = "issue_events"
    __table_args__ = (
        Index("ix_issue_events_issue_created", "issue_id", "created_at"),
        Index("ix_issue_events_tenant_type", "tenant_id", "event_type"),
    )

    issue_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[IssueEventType] = mapped_column(
        StringEnum(IssueEventType), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_label: Mapped[str] = mapped_column(String(120), default="system", nullable=False)

    from_value: Mapped[str | None] = mapped_column(String(64))
    to_value: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    issue: Mapped[Issue] = relationship(back_populates="events")


class IssueComment(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Conversation on an issue, split between public replies and internal notes."""

    __tablename__ = "issue_comments"
    __table_args__ = (Index("ix_issue_comments_issue", "issue_id", "created_at"),)

    issue_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    author_label: Mapped[str] = mapped_column(String(120), default="Resident", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.PUBLIC, nullable=False
    )
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sentiment: Mapped[Sentiment | None] = mapped_column(StringEnum(Sentiment))

    issue: Mapped[Issue] = relationship(back_populates="comments")
    author: Mapped[User | None] = relationship(lazy="joined")


class IssueFollower(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """"Me too" / follow relationship.

    Doubles as the upvote mechanism: ``confirmed`` means the follower says they
    see the same problem, which feeds prioritisation and duplicate merging.
    """

    __tablename__ = "issue_followers"
    __table_args__ = (
        UniqueConstraint("issue_id", "user_id", name="uq_issue_followers_issue_user"),
    )

    issue_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    issue: Mapped[Issue] = relationship(back_populates="followers")
