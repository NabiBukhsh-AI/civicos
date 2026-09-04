"""Tenancy: municipalities, their sub-divisions, departments and taxonomy.

A "tenant" here is one municipality - a town, a district council, a municipal
committee, a cantonment board. Everything else in CivicOS hangs off this table,
which is what makes the platform portable to any administration rather than
hard-wired to one.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from civicos.db.base import (
    ActorStampMixin,
    Base,
    MetadataMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from civicos.db.types import Coordinate, MutableJSONDict, MutableJSONList, StringEnum
from civicos.domain.enums import (
    AdminUnitType,
    MunicipalityTier,
    Priority,
    TenantStatus,
)

if TYPE_CHECKING:
    from civicos.domain.identity import User


class Municipality(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base):
    """One municipality / town / council. The tenant root."""

    __tablename__ = "municipalities"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_municipalities_slug"),
        Index("ix_municipalities_status", "status"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(160))
    tier: Mapped[MunicipalityTier] = mapped_column(
        StringEnum(MunicipalityTier), default=MunicipalityTier.TOWN, nullable=False
    )
    status: Mapped[TenantStatus] = mapped_column(
        StringEnum(TenantStatus), default=TenantStatus.ACTIVE, nullable=False
    )

    parent_authority: Mapped[str | None] = mapped_column(String(160))
    country_code: Mapped[str] = mapped_column(String(2), default="XX", nullable=False)
    region: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    # Contact / branding surface used by the public portal.
    helpline: Mapped[str | None] = mapped_column(String(40))
    whatsapp_number: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(160))
    website: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    logo_url: Mapped[str | None] = mapped_column(String(500))
    primary_colour: Mapped[str] = mapped_column(String(9), default="#0F766E", nullable=False)

    # Map defaults for the operations console.
    centre_latitude: Mapped[float | None] = mapped_column(Coordinate)
    centre_longitude: Mapped[float | None] = mapped_column(Coordinate)
    default_zoom: Mapped[int] = mapped_column(Integer, default=13, nullable=False)
    boundary: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    """GeoJSON-style ``[[lat, lon], ...]`` ring, used for in/out-of-area checks."""

    population: Mapped[int | None] = mapped_column(Integer)
    area_sq_km: Mapped[float | None] = mapped_column(Coordinate)

    default_language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)
    supported_languages: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=lambda: ["en"], nullable=False
    )

    #: Feature switches, e.g. ``{"ai_triage": true, "public_map": false}``.
    features: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)
    #: Per-tenant overrides for SLA behaviour, AI limits, notification defaults.
    settings: Mapped[dict[str, Any]] = mapped_column(MutableJSONDict, default=dict, nullable=False)

    admin_units: Mapped[list[AdminUnit]] = relationship(
        back_populates="municipality", cascade="all, delete-orphan", lazy="selectin"
    )
    departments: Mapped[list[Department]] = relationship(
        back_populates="municipality", cascade="all, delete-orphan", lazy="selectin"
    )

    def feature_enabled(self, key: str, default: bool = True) -> bool:
        value = self.features.get(key, default)
        return bool(value)

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)


class AdminUnit(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """A ward / union council / neighbourhood inside a municipality.

    Self-referential so a town can model Zone -> UC -> Ward without schema
    changes; ``path`` caches the ancestry for fast subtree queries.
    """

    __tablename__ = "admin_units"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_admin_units_tenant_code"),
        Index("ix_admin_units_tenant_type", "tenant_id", "unit_type"),
        Index("ix_admin_units_parent", "parent_id"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(160))
    unit_type: Mapped[AdminUnitType] = mapped_column(
        StringEnum(AdminUnitType), default=AdminUnitType.WARD, nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )
    path: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    """Materialised ancestry, e.g. ``/zone-a/uc-3/ward-11``."""

    centre_latitude: Mapped[float | None] = mapped_column(Coordinate)
    centre_longitude: Mapped[float | None] = mapped_column(Coordinate)
    boundary: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    population: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    municipality: Mapped[Municipality] = relationship(back_populates="admin_units")
    parent: Mapped[AdminUnit | None] = relationship(remote_side="AdminUnit.id")
    representatives: Mapped[list[Representative]] = relationship(
        back_populates="admin_unit", cascade="all, delete-orphan"
    )


class Department(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """An operational unit that owns categories of work (Sanitation, Water...)."""

    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_departments_tenant_code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)

    head_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    contact_email: Mapped[str | None] = mapped_column(String(160))

    #: Escalation ladder - user ids consulted in order when an SLA breaches.
    escalation_chain: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    working_hours: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONDict,
        default=lambda: {"start": "09:00", "end": "17:00", "weekend_days": [6]},
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    municipality: Mapped[Municipality] = relationship(back_populates="departments")
    head: Mapped[User | None] = relationship(foreign_keys=[head_user_id], lazy="joined")
    categories: Mapped[list[IssueCategory]] = relationship(back_populates="department")


class IssueCategory(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """The complaint taxonomy.

    Seeded with a sensible municipal default set, then editable per tenant.
    ``keywords`` and ``ai_hints`` feed both the rule-based router and the LLM
    triage prompt, so a town can teach the classifier local vocabulary
    ("gutter", "nala", "K-Electric pole") without touching code.
    """

    __tablename__ = "issue_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_issue_categories_tenant_slug"),
        Index("ix_issue_categories_tenant_active", "tenant_id", "is_active"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(64))
    colour: Mapped[str | None] = mapped_column(String(9))

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issue_categories.id", ondelete="SET NULL")
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )

    default_priority: Mapped[Priority] = mapped_column(
        StringEnum(Priority), default=Priority.NORMAL, nullable=False
    )
    keywords: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    ai_hints: Mapped[str | None] = mapped_column(Text)
    requires_photo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_location: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allows_anonymous: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    department: Mapped[Department | None] = relationship(back_populates="categories")
    parent: Mapped[IssueCategory | None] = relationship(remote_side="IssueCategory.id")


class SLAPolicy(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, ActorStampMixin, Base):
    """Response and resolution targets.

    Resolution order is most-specific-first: (category, priority) beats
    (category, any) beats (any, priority) beats the tenant default.
    """

    __tablename__ = "sla_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "category_id", "priority", name="uq_sla_policies_scope"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("issue_categories.id", ondelete="CASCADE"), index=True
    )
    priority: Mapped[Priority | None] = mapped_column(StringEnum(Priority))

    response_minutes: Mapped[int] = mapped_column(Integer, default=240, nullable=False)
    resolution_minutes: Mapped[int] = mapped_column(Integer, default=4320, nullable=False)
    #: Fraction of the window at which the issue is flagged "at risk".
    warning_threshold: Mapped[float] = mapped_column(Coordinate, default=0.75, nullable=False)
    business_hours_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    escalate_on_breach: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    category: Mapped[IssueCategory | None] = relationship()

    @property
    def specificity(self) -> int:
        """Higher wins when several policies match an issue."""
        return (2 if self.category_id else 0) + (1 if self.priority else 0)


class Representative(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """An elected or appointed office-holder tied to an area.

    Gives residents a "who represents me" lookup and gives the platform an
    escalation target that is accountable to voters rather than to a department.
    """

    __tablename__ = "representatives"
    __table_args__ = (Index("ix_representatives_tenant_unit", "tenant_id", "admin_unit_id"),)

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    party: Mapped[str | None] = mapped_column(String(120))
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(160))
    office_address: Mapped[str | None] = mapped_column(Text)
    office_hours: Mapped[str | None] = mapped_column(String(255))
    photo_url: Mapped[str | None] = mapped_column(String(500))
    term_start: Mapped[str | None] = mapped_column(String(10))
    term_end: Mapped[str | None] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    admin_unit: Mapped[AdminUnit | None] = relationship(back_populates="representatives")
