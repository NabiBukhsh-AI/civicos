"""Identity: staff accounts, residents, machine clients and sessions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from civicos.core.permissions import Role
from civicos.db.base import (
    Base,
    MetadataMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from civicos.db.types import MutableJSONList, StringEnum, UTCDateTime
from civicos.domain.enums import NotificationChannel, UserStatus, VerificationMethod


class User(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base):
    """A person with an account.

    Staff and residents share one table: a resident who is later hired as a
    sanitary inspector keeps their reporting history. ``role`` decides what they
    can do; :mod:`civicos.core.permissions` maps it to concrete permissions.

    Either ``email`` or ``phone`` is required. In many deployments the phone
    number is the primary identity for residents, so both are unique-per-tenant
    and either can be used to sign in.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "phone", name="uq_users_tenant_phone"),
        Index("ix_users_tenant_role", "tenant_id", "role"),
        Index("ix_users_tenant_department", "tenant_id", "department_id"),
    )

    email: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str | None] = mapped_column(String(255))

    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[Role] = mapped_column(StringEnum(Role), default=Role.CITIZEN, nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        StringEnum(UserStatus), default=UserStatus.ACTIVE, nullable=False
    )

    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL"), index=True
    )
    designation: Mapped[str | None] = mapped_column(String(120))
    employee_number: Mapped[str | None] = mapped_column(String(48))

    #: Extra grants beyond the role, e.g. a clerk temporarily allowed to assign.
    extra_permissions: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )

    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    notification_channels: Mapped[list[Any]] = mapped_column(
        MutableJSONList,
        default=lambda: [NotificationChannel.IN_APP.value],
        nullable=False,
    )
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    address: Mapped[str | None] = mapped_column(Text)

    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_method: Mapped[VerificationMethod] = mapped_column(
        StringEnum(VerificationMethod), default=VerificationMethod.NONE, nullable=False
    )
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    #: Aggregate reputation, nudged by verified reports and false alarms.
    trust_score: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    reports_submitted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reports_confirmed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_locked(self) -> bool:
        from civicos.core.clock import utcnow  # local import avoids a cycle

        return self.locked_until is not None and self.locked_until > utcnow()

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE and not self.is_deleted

    @property
    def identifier(self) -> str:
        return self.email or self.phone or str(self.id)

    def display(self) -> str:
        return self.local_name or self.full_name


class UserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A refresh-token session.

    Storing sessions server-side is what makes "sign out everywhere" and
    per-device revocation possible; the refresh token itself is only ever
    stored as a hash.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_user_sessions_token_hash", "refresh_token_hash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(String(64))

    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    device_label: Mapped[str | None] = mapped_column(String(120))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_active(self) -> bool:
        from civicos.core.clock import utcnow

        return self.revoked_at is None and self.expires_at > utcnow()


class ApiKey(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Credential for machine clients: IoT sensors, the WhatsApp gateway,
    a partner NGO's dashboard, the open-data mirror."""

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("key_prefix", name="uq_api_keys_prefix"),
        Index("ix_api_keys_tenant_active", "tenant_id", "revoked_at"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    allowed_ips: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)

    @property
    def is_active(self) -> bool:
        from civicos.core.clock import utcnow

        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > utcnow()


class OneTimeCode(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Short-lived codes for phone verification and password reset.

    Phone-OTP sign-in matters here: many residents have no email address, and a
    six-digit SMS code is the only login they will ever complete.
    """

    __tablename__ = "one_time_codes"
    __table_args__ = (Index("ix_one_time_codes_target", "tenant_id", "target", "purpose"),)

    target: Mapped[str] = mapped_column(String(160), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
