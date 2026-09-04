"""Citizen engagement: announcements, emergency alerts, surveys and feedback."""

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
    AlertCategory,
    AlertSeverity,
    AnnouncementType,
    QuestionType,
    Sentiment,
    SurveyStatus,
    Visibility,
)


class Announcement(
    UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base
):
    """Notices, news, tenders and planned-outage information.

    ``body_translations`` holds pre-translated copies so a notice reaches Urdu,
    Sindhi and Balochi readers at the same moment rather than "when someone gets
    round to it".
    """

    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_tenant_published", "tenant_id", "published_at"),
        Index("ix_announcements_tenant_type", "tenant_id", "announcement_type"),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_translations: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    announcement_type: Mapped[AnnouncementType] = mapped_column(
        StringEnum(AnnouncementType), default=AnnouncementType.NOTICE, nullable=False
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    #: Empty means town-wide.
    admin_unit_ids: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.PUBLIC, nullable=False
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    event_starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    event_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    location: Mapped[str | None] = mapped_column(String(300))

    attachment_keys: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    cover_image_url: Mapped[str | None] = mapped_column(String(500))
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_live(self) -> bool:
        from civicos.core.clock import ensure_utc, utcnow

        now = utcnow()
        if self.published_at is None or ensure_utc(self.published_at) > now:
            return False
        return self.expires_at is None or ensure_utc(self.expires_at) > now


class EmergencyAlert(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """A time-critical broadcast: urban flooding, a heatwave, a water cut.

    Kept separate from announcements because the delivery guarantees differ -
    alerts fan out to SMS/WhatsApp/push immediately, can be geo-targeted to a
    radius, and are expected to be cancelled explicitly when the danger passes.
    """

    __tablename__ = "emergency_alerts"
    __table_args__ = (
        Index("ix_emergency_alerts_tenant_active", "tenant_id", "is_active"),
        Index("ix_emergency_alerts_tenant_issued", "tenant_id", "issued_at"),
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    message_translations: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    instructions: Mapped[str | None] = mapped_column(Text)

    severity: Mapped[AlertSeverity] = mapped_column(
        StringEnum(AlertSeverity), default=AlertSeverity.ADVISORY, nullable=False
    )
    category: Mapped[AlertCategory] = mapped_column(
        StringEnum(AlertCategory), default=AlertCategory.OTHER, nullable=False
    )

    admin_unit_ids: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    centre_latitude: Mapped[float | None] = mapped_column(Coordinate)
    centre_longitude: Mapped[float | None] = mapped_column(Coordinate)
    radius_meters: Mapped[int | None] = mapped_column(Integer)

    channels: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=lambda: ["in_app"], nullable=False
    )
    issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancellation_note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    recipients_targeted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recipients_delivered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    issued_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_live(self) -> bool:
        from civicos.core.clock import ensure_utc, utcnow

        if not self.is_active or self.cancelled_at is not None:
            return False
        return self.expires_at is None or ensure_utc(self.expires_at) > utcnow()


class Survey(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A consultation or satisfaction survey."""

    __tablename__ = "surveys"
    __table_args__ = (Index("ix_surveys_tenant_status", "tenant_id", "status"),)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[SurveyStatus] = mapped_column(
        StringEnum(SurveyStatus), default=SurveyStatus.DRAFT, nullable=False
    )
    admin_unit_ids: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    opens_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closes_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_verification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    response_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Model-written digest of free-text answers, refreshed as responses land.
    ai_summary: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    questions: Mapped[list[SurveyQuestion]] = relationship(
        back_populates="survey",
        cascade="all, delete-orphan",
        order_by="SurveyQuestion.display_order",
        lazy="selectin",
    )
    responses: Mapped[list[SurveyResponse]] = relationship(
        back_populates="survey", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        from civicos.core.clock import ensure_utc, utcnow

        if self.status is not SurveyStatus.OPEN:
            return False
        now = utcnow()
        if self.opens_at and ensure_utc(self.opens_at) > now:
            return False
        return not (self.closes_at and ensure_utc(self.closes_at) < now)


class SurveyQuestion(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "survey_questions"
    __table_args__ = (Index("ix_survey_questions_survey", "survey_id", "display_order"),)

    survey_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_translations: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict, default=dict, nullable=False
    )
    question_type: Mapped[QuestionType] = mapped_column(
        StringEnum(QuestionType), default=QuestionType.SINGLE_CHOICE, nullable=False
    )
    options: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    survey: Mapped[Survey] = relationship(back_populates="questions")


class SurveyResponse(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "survey_responses"
    __table_args__ = (
        UniqueConstraint("survey_id", "respondent_key", name="uq_survey_responses_respondent"),
        Index("ix_survey_responses_survey", "survey_id"),
    )

    survey_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    respondent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    """Hashed identity; lets an anonymous survey still reject double-voting."""

    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )
    #: ``{question_id: answer}``
    answers: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)
    sentiment: Mapped[Sentiment | None] = mapped_column(StringEnum(Sentiment))

    survey: Mapped[Survey] = relationship(back_populates="responses")


class Feedback(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Unsolicited feedback about the service itself (not about a pothole)."""

    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_tenant_created", "tenant_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer)
    sentiment: Mapped[Sentiment | None] = mapped_column(StringEnum(Sentiment))
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    themes: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(160))
    is_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
