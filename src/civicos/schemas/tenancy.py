"""Municipality configuration contracts: tenants, units, departments, taxonomy."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from civicos.domain.enums import AdminUnitType, MunicipalityTier, Priority, TenantStatus
from civicos.schemas.common import APIModel, InputModel


class MunicipalityCreateRequest(InputModel):
    slug: str = Field(
        min_length=2,
        max_length=64,
        pattern="^[a-z0-9][a-z0-9-]*$",
        description="URL-safe identifier used for subdomain and header routing.",
    )
    name: str = Field(min_length=2, max_length=160)
    local_name: str | None = Field(default=None, max_length=160)
    tier: MunicipalityTier = MunicipalityTier.TOWN
    parent_authority: str | None = Field(default=None, max_length=160)
    country_code: str = Field(default="XX", min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=120)
    timezone: str = Field(default="UTC", max_length=64)
    currency_code: str = Field(default="USD", min_length=3, max_length=3)
    default_language: str = Field(default="en", max_length=8)
    supported_languages: list[str] = Field(default_factory=lambda: ["en"], max_length=12)
    centre_latitude: float | None = Field(default=None, ge=-90, le=90)
    centre_longitude: float | None = Field(default=None, ge=-180, le=180)
    population: int | None = Field(default=None, ge=0)
    seed_defaults: bool = Field(
        default=True,
        description="Create the standard departments, categories and SLA policies.",
    )


class MunicipalityUpdateRequest(InputModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    local_name: str | None = Field(default=None, max_length=160)
    status: TenantStatus | None = None
    helpline: str | None = Field(default=None, max_length=40)
    whatsapp_number: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=160)
    website: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    logo_url: str | None = Field(default=None, max_length=500)
    primary_colour: str | None = Field(default=None, max_length=9)
    centre_latitude: float | None = Field(default=None, ge=-90, le=90)
    centre_longitude: float | None = Field(default=None, ge=-180, le=180)
    default_zoom: int | None = Field(default=None, ge=1, le=20)
    boundary: list[list[float]] | None = Field(
        default=None, description="Polygon ring as [[lat, lon], ...]."
    )
    timezone: str | None = Field(default=None, max_length=64)
    default_language: str | None = Field(default=None, max_length=8)
    supported_languages: list[str] | None = Field(default=None, max_length=12)
    features: dict[str, bool] | None = None
    settings: dict[str, Any] | None = None


class MunicipalityOut(APIModel):
    id: uuid.UUID
    slug: str
    name: str
    local_name: str | None = None
    tier: MunicipalityTier
    status: TenantStatus
    parent_authority: str | None = None
    country_code: str
    region: str | None = None
    timezone: str
    currency_code: str
    helpline: str | None = None
    whatsapp_number: str | None = None
    email: str | None = None
    website: str | None = None
    logo_url: str | None = None
    primary_colour: str
    centre_latitude: float | None = None
    centre_longitude: float | None = None
    default_zoom: int
    population: int | None = None
    default_language: str
    supported_languages: list[str]
    features: dict[str, Any]
    created_at: datetime


class PublicMunicipalityOut(APIModel):
    """What the public portal is allowed to see about a municipality."""

    slug: str
    name: str
    local_name: str | None = None
    tier: MunicipalityTier
    helpline: str | None = None
    whatsapp_number: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    logo_url: str | None = None
    primary_colour: str
    centre_latitude: float | None = None
    centre_longitude: float | None = None
    default_zoom: int
    default_language: str
    supported_languages: list[str]


class AdminUnitCreateRequest(InputModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    local_name: str | None = Field(default=None, max_length=160)
    unit_type: AdminUnitType = AdminUnitType.WARD
    parent_id: uuid.UUID | None = None
    centre_latitude: float | None = Field(default=None, ge=-90, le=90)
    centre_longitude: float | None = Field(default=None, ge=-180, le=180)
    boundary: list[list[float]] = Field(default_factory=list)
    population: int | None = Field(default=None, ge=0)


class AdminUnitOut(APIModel):
    id: uuid.UUID
    code: str
    name: str
    local_name: str | None = None
    unit_type: AdminUnitType
    parent_id: uuid.UUID | None = None
    path: str
    centre_latitude: float | None = None
    centre_longitude: float | None = None
    population: int | None = None
    is_active: bool


class DepartmentCreateRequest(InputModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=160)
    local_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    head_user_id: uuid.UUID | None = None
    contact_phone: str | None = Field(default=None, max_length=40)
    contact_email: str | None = Field(default=None, max_length=160)
    escalation_chain: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    working_hours: dict[str, Any] | None = None


class DepartmentOut(APIModel):
    id: uuid.UUID
    code: str
    name: str
    local_name: str | None = None
    description: str | None = None
    head_user_id: uuid.UUID | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    working_hours: dict[str, Any]
    is_active: bool


class CategoryCreateRequest(InputModel):
    slug: str = Field(min_length=2, max_length=64, pattern="^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=120)
    local_name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    parent_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    default_priority: Priority = Priority.NORMAL
    keywords: list[str] = Field(
        default_factory=list,
        max_length=40,
        description="Local vocabulary that should route to this category.",
    )
    ai_hints: str | None = Field(
        default=None,
        max_length=1000,
        description="Free-text guidance injected into the triage prompt.",
    )
    icon: str | None = Field(default=None, max_length=64)
    colour: str | None = Field(default=None, max_length=9)
    requires_photo: bool = False
    requires_location: bool = True
    allows_anonymous: bool = True
    is_emergency: bool = False
    display_order: int = 100


class CategoryUpdateRequest(InputModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    local_name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    department_id: uuid.UUID | None = None
    default_priority: Priority | None = None
    keywords: list[str] | None = Field(default=None, max_length=40)
    ai_hints: str | None = Field(default=None, max_length=1000)
    icon: str | None = Field(default=None, max_length=64)
    colour: str | None = Field(default=None, max_length=9)
    requires_photo: bool | None = None
    requires_location: bool | None = None
    allows_anonymous: bool | None = None
    is_emergency: bool | None = None
    is_active: bool | None = None
    display_order: int | None = None


class CategoryOut(APIModel):
    id: uuid.UUID
    slug: str
    name: str
    local_name: str | None = None
    description: str | None = None
    parent_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    default_priority: Priority
    keywords: list[str]
    icon: str | None = None
    colour: str | None = None
    requires_photo: bool
    requires_location: bool
    allows_anonymous: bool
    is_emergency: bool
    is_active: bool
    display_order: int


class SLAPolicyRequest(InputModel):
    name: str = Field(min_length=2, max_length=120)
    category_id: uuid.UUID | None = None
    priority: Priority | None = None
    response_minutes: int = Field(ge=5, le=525_600)
    resolution_minutes: int = Field(ge=15, le=525_600)
    warning_threshold: float = Field(default=0.75, ge=0.1, le=0.99)
    business_hours_only: bool = True
    escalate_on_breach: bool = True


class SLAPolicyOut(APIModel):
    id: uuid.UUID
    name: str
    category_id: uuid.UUID | None = None
    priority: Priority | None = None
    response_minutes: int
    resolution_minutes: int
    warning_threshold: float
    business_hours_only: bool
    escalate_on_breach: bool
    is_active: bool


class RepresentativeRequest(InputModel):
    name: str = Field(min_length=2, max_length=160)
    local_name: str | None = Field(default=None, max_length=160)
    title: str = Field(min_length=2, max_length=120)
    party: str | None = Field(default=None, max_length=120)
    admin_unit_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=160)
    office_address: str | None = Field(default=None, max_length=500)
    office_hours: str | None = Field(default=None, max_length=255)
    photo_url: str | None = Field(default=None, max_length=500)
    term_start: str | None = Field(default=None, max_length=10)
    term_end: str | None = Field(default=None, max_length=10)


class RepresentativeOut(APIModel):
    id: uuid.UUID
    name: str
    local_name: str | None = None
    title: str
    party: str | None = None
    admin_unit_id: uuid.UUID | None = None
    phone: str | None = None
    email: str | None = None
    office_address: str | None = None
    office_hours: str | None = None
    photo_url: str | None = None
    term_start: str | None = None
    term_end: str | None = None
    is_active: bool


class LanguageOut(APIModel):
    code: str
    name: str
    native_name: str
    direction: str
