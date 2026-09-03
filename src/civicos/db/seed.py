"""Seed data.

A municipality that has to invent its own taxonomy before it can accept a
single complaint will not get started. These defaults are a working
configuration on day one - a standard department structure, a complaint
taxonomy with local-vocabulary keywords, sensible SLA targets and a service
catalogue - all of which the tenant then edits to fit how it actually works.

Nothing here is specific to any one town.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.permissions import Role
from civicos.core.security import hash_password
from civicos.domain.enums import (
    AdminUnitType,
    AssetType,
    MunicipalityTier,
    Priority,
    ScheduleFrequency,
    Visibility,
)
from civicos.domain.identity import User
from civicos.domain.services import ServiceSchedule, ServiceType
from civicos.domain.tenancy import (
    AdminUnit,
    Department,
    IssueCategory,
    Municipality,
    SLAPolicy,
)

logger = structlog.get_logger(__name__)


#: code -> (name, description)
DEFAULT_DEPARTMENTS: tuple[tuple[str, str, str], ...] = (
    ("sanitation", "Sanitation & Solid Waste", "Street sweeping, waste collection, transfer stations."),
    ("water-sanitation", "Water & Sewerage", "Water supply, sewerage, drainage and pumping."),
    ("works", "Public Works", "Roads, footpaths, street lighting and municipal buildings."),
    ("parks-horticulture", "Parks & Horticulture", "Parks, playgrounds, trees and green spaces."),
    ("public-health", "Public Health", "Vector control, food safety, animal control, dispensaries."),
    ("enforcement", "Enforcement & Building Control", "Encroachment, illegal construction, licensing."),
    ("emergency-services", "Emergency Services", "Fire, rescue and disaster response."),
    ("administration", "Administration", "Records, revenue, procurement and general administration."),
)

#: slug, name, department, default priority, emergency?, keywords
DEFAULT_CATEGORIES: tuple[tuple[str, str, str, Priority, bool, tuple[str, ...]], ...] = (
    (
        "solid-waste", "Waste not collected", "sanitation", Priority.NORMAL, False,
        ("garbage", "trash", "rubbish", "waste", "bin", "dump", "litter", "refuse"),
    ),
    (
        "street-sweeping", "Street not swept", "sanitation", Priority.LOW, False,
        ("sweeping", "dirty street", "dust", "debris"),
    ),
    (
        "sewerage", "Sewerage overflow / blockage", "water-sanitation", Priority.HIGH, False,
        ("sewer", "sewage", "gutter", "manhole", "overflow", "blocked drain", "smell"),
    ),
    (
        "water-supply", "Water supply problem", "water-sanitation", Priority.HIGH, False,
        ("no water", "water supply", "low pressure", "pipeline", "leak", "tanker", "contaminated"),
    ),
    (
        "drainage", "Drainage / flooding", "water-sanitation", Priority.HIGH, False,
        ("drain", "flood", "waterlogging", "standing water", "rain water", "storm drain"),
    ),
    (
        "roads", "Road / footpath damage", "works", Priority.NORMAL, False,
        ("pothole", "road", "footpath", "pavement", "broken road", "speed breaker", "kerb"),
    ),
    (
        "streetlights", "Street light fault", "works", Priority.NORMAL, False,
        ("streetlight", "street light", "lamp", "pole", "dark", "bulb", "not working"),
    ),
    (
        "public-buildings", "Municipal building issue", "works", Priority.NORMAL, False,
        ("building", "office", "school building", "toilet block", "community centre"),
    ),
    (
        "parks", "Park / green space issue", "parks-horticulture", Priority.LOW, False,
        ("park", "playground", "tree", "green belt", "grass", "bench", "swing"),
    ),
    (
        "public-health", "Public health hazard", "public-health", Priority.HIGH, False,
        ("mosquito", "dengue", "fogging", "spray", "disease", "unhygienic", "food"),
    ),
    (
        "animal-control", "Stray or dead animal", "public-health", Priority.NORMAL, False,
        ("stray", "dog", "animal", "cattle", "dead animal", "bite"),
    ),
    (
        "sanitation", "Public toilet / sanitation", "public-health", Priority.NORMAL, False,
        ("toilet", "washroom", "latrine", "sanitation block"),
    ),
    (
        "encroachment", "Encroachment / illegal construction", "enforcement", Priority.NORMAL, False,
        ("encroach", "illegal construction", "occupied", "kiosk", "extension", "unauthorised"),
    ),
    (
        "noise", "Noise nuisance", "enforcement", Priority.LOW, False,
        ("noise", "loudspeaker", "generator", "loud music"),
    ),
    (
        "fire-safety", "Fire or immediate danger", "emergency-services", Priority.EMERGENCY, True,
        ("fire", "smoke", "burning", "gas leak", "explosion", "collapse", "electrocution"),
    ),
    (
        "other", "Something else", "administration", Priority.NORMAL, False,
        (),
    ),
)

#: (name, category slug or None, priority or None, response mins, resolution mins)
DEFAULT_SLA_POLICIES: tuple[tuple[str, str | None, Priority | None, int, int], ...] = (
    ("Default", None, None, 240, 4320),
    ("Emergency", None, Priority.EMERGENCY, 15, 240),
    ("Urgent", None, Priority.URGENT, 60, 1440),
    ("High priority", None, Priority.HIGH, 120, 2880),
    ("Low priority", None, Priority.LOW, 480, 10080),
    ("Fire and immediate danger", "fire-safety", None, 10, 120),
    ("Sewerage overflow", "sewerage", None, 120, 1440),
    ("Water supply", "water-supply", None, 120, 1440),
    ("Drainage and flooding", "drainage", None, 60, 1440),
    ("Waste collection", "solid-waste", None, 240, 2880),
    ("Street lighting", "streetlights", None, 480, 7200),
)

#: slug, name, fee, processing days, required documents
DEFAULT_SERVICES: tuple[tuple[str, str, float, int, tuple[str, ...]], ...] = (
    ("trade-licence", "Trade licence", 0.0, 14, ("identity_document", "premises_proof", "photograph")),
    ("building-noc", "Building no-objection certificate", 0.0, 30, ("identity_document", "site_plan", "ownership_proof")),
    ("water-connection", "New water connection", 0.0, 21, ("identity_document", "ownership_proof")),
    ("water-tanker", "Water tanker request", 0.0, 2, ("identity_document",)),
    ("advertising-permit", "Advertising / hoarding permit", 0.0, 14, ("identity_document", "site_photograph")),
    ("event-permit", "Public event permit", 0.0, 10, ("identity_document", "event_plan")),
    ("birth-certificate", "Birth certificate", 0.0, 7, ("identity_document", "hospital_record")),
    ("death-certificate", "Death certificate", 0.0, 7, ("identity_document", "medical_certificate")),
    ("tree-cutting", "Tree cutting / pruning permission", 0.0, 14, ("identity_document", "site_photograph")),
)

#: kind, name, frequency, days of week
DEFAULT_SCHEDULES: tuple[tuple[str, str, ScheduleFrequency, tuple[int, ...]], ...] = (
    ("waste_collection", "Household waste collection", ScheduleFrequency.DAILY, ()),
    ("street_sweeping", "Street sweeping", ScheduleFrequency.DAILY, ()),
    ("drain_cleaning", "Drain cleaning", ScheduleFrequency.WEEKLY, (1,)),
    ("fogging", "Anti-mosquito fogging", ScheduleFrequency.FORTNIGHTLY, (3,)),
    ("water_supply", "Water supply hours", ScheduleFrequency.DAILY, ()),
)


async def seed_tenant_defaults(
    session: AsyncSession, tenant: Municipality
) -> dict[str, int]:
    """Populate a new municipality with a working default configuration."""
    created = {"departments": 0, "categories": 0, "sla_policies": 0, "services": 0}

    departments: dict[str, Department] = {}
    for code, name, description in DEFAULT_DEPARTMENTS:
        existing = await session.scalar(
            select(Department).where(
                Department.tenant_id == tenant.id, Department.code == code
            )
        )
        if existing is not None:
            departments[code] = existing
            continue
        department = Department(
            tenant_id=tenant.id, code=code, name=name, description=description
        )
        session.add(department)
        departments[code] = department
        created["departments"] += 1
    await session.flush()

    categories: dict[str, IssueCategory] = {}
    for order, (slug, name, department_code, priority, is_emergency, keywords) in enumerate(
        DEFAULT_CATEGORIES
    ):
        existing = await session.scalar(
            select(IssueCategory).where(
                IssueCategory.tenant_id == tenant.id, IssueCategory.slug == slug
            )
        )
        if existing is not None:
            categories[slug] = existing
            continue
        category = IssueCategory(
            tenant_id=tenant.id,
            slug=slug,
            name=name,
            department_id=departments[department_code].id,
            default_priority=priority,
            is_emergency=is_emergency,
            keywords=list(keywords),
            requires_photo=slug in {"encroachment", "roads", "solid-waste"},
            display_order=(order + 1) * 10,
        )
        session.add(category)
        categories[slug] = category
        created["categories"] += 1
    await session.flush()

    for name, category_slug, priority, response, resolution in DEFAULT_SLA_POLICIES:
        category = categories.get(category_slug) if category_slug else None
        if category_slug and category is None:
            continue
        exists = await session.scalar(
            select(SLAPolicy).where(
                SLAPolicy.tenant_id == tenant.id,
                SLAPolicy.category_id == (category.id if category else None),
                SLAPolicy.priority == priority,
            )
        )
        if exists is not None:
            continue
        session.add(
            SLAPolicy(
                tenant_id=tenant.id,
                name=name,
                category_id=category.id if category else None,
                priority=priority,
                response_minutes=response,
                resolution_minutes=resolution,
                # Emergencies do not observe office hours.
                business_hours_only=priority is not Priority.EMERGENCY,
            )
        )
        created["sla_policies"] += 1

    for order, (slug, name, fee, days, documents) in enumerate(DEFAULT_SERVICES):
        exists = await session.scalar(
            select(ServiceType).where(
                ServiceType.tenant_id == tenant.id, ServiceType.slug == slug
            )
        )
        if exists is not None:
            continue
        session.add(
            ServiceType(
                tenant_id=tenant.id,
                slug=slug,
                name=name,
                department_id=departments["administration"].id,
                fee_amount=fee,
                processing_days=days,
                validity_days=365 if "licence" in slug or "permit" in slug else None,
                requires_inspection=slug in {"building-noc", "trade-licence"},
                required_documents=[
                    {"code": code, "label": code.replace("_", " ").title(), "required": True}
                    for code in documents
                ],
                form_schema=_default_form_schema(slug),
                display_order=(order + 1) * 10,
            )
        )
        created["services"] += 1

    tenant.features = {
        "ai_triage": True,
        "assistant": True,
        "vision": True,
        "public_issues": True,
        "public_map": True,
        "open_data": True,
        "auto_assignment": True,
        "surveys": True,
        "budget_transparency": True,
        **(tenant.features or {}),
    }
    tenant.settings = {
        "ai_triage_confidence": 0.5,
        "duplicate_auto_merge": True,
        "citizen_can_reopen": True,
        **(tenant.settings or {}),
    }

    await session.flush()
    logger.info("tenant_defaults_seeded", tenant=tenant.slug, **created)
    return created


def _default_form_schema(slug: str) -> list[dict[str, Any]]:
    """A minimal, sensible form per service; tenants edit these freely."""
    common = [
        {"code": "applicant_address", "label": "Applicant address", "type": "text", "required": True},
        {"code": "contact_number", "label": "Contact number", "type": "text", "required": True},
    ]
    specific: dict[str, list[dict[str, Any]]] = {
        "trade-licence": [
            {"code": "business_name", "label": "Business name", "type": "text", "required": True},
            {"code": "business_type", "label": "Nature of business", "type": "text", "required": True},
            {"code": "premises_area", "label": "Premises area (sq ft)", "type": "number", "required": False},
        ],
        "building-noc": [
            {"code": "plot_number", "label": "Plot number", "type": "text", "required": True},
            {"code": "storeys", "label": "Number of storeys", "type": "number", "required": True},
            {"code": "covered_area", "label": "Covered area (sq ft)", "type": "number", "required": True},
        ],
        "water-tanker": [
            {"code": "quantity_litres", "label": "Quantity required (litres)", "type": "number", "required": True},
            {"code": "urgency", "label": "Required by", "type": "date", "required": False},
        ],
        "event-permit": [
            {"code": "event_name", "label": "Event name", "type": "text", "required": True},
            {"code": "event_date", "label": "Event date", "type": "date", "required": True},
            {"code": "expected_attendance", "label": "Expected attendance", "type": "number", "required": True},
        ],
    }
    return common + specific.get(slug, [])


async def seed_demo_tenant(
    session: AsyncSession,
    *,
    slug: str = "demo",
    name: str = "Demo Municipality",
    admin_email: str = "admin@example.gov",
    admin_password: str = "ChangeMe123!",
) -> Municipality:
    """Create a fully configured demo municipality for local development.

    Deliberately not called on startup: seeding a database is an explicit
    action (``civicos seed``), never a side effect of booting the server.
    """
    existing = await session.scalar(select(Municipality).where(Municipality.slug == slug))
    if existing is not None:
        logger.info("demo_tenant_exists", slug=slug)
        return existing

    tenant = Municipality(
        slug=slug,
        name=name,
        tier=MunicipalityTier.TOWN,
        timezone="UTC",
        default_language="en",
        supported_languages=["en"],
        helpline="1234",
        email=f"info@{slug}.example",
        population=250_000,
        centre_latitude=0.0,
        centre_longitude=0.0,
    )
    session.add(tenant)
    await session.flush()

    await seed_tenant_defaults(session, tenant)

    units = []
    for index in range(1, 6):
        unit = AdminUnit(
            tenant_id=tenant.id,
            code=f"W{index:02d}",
            name=f"Ward {index}",
            unit_type=AdminUnitType.WARD,
            path=f"/ward-{index}",
            population=50_000,
        )
        session.add(unit)
        units.append(unit)
    await session.flush()

    session.add(
        User(
            tenant_id=tenant.id,
            email=admin_email,
            full_name="Municipal Administrator",
            role=Role.TENANT_ADMIN,
            password_hash=hash_password(admin_password),
            is_verified=True,
            language="en",
        )
    )

    for kind, schedule_name, frequency, days in DEFAULT_SCHEDULES:
        for unit in units[:2]:
            session.add(
                ServiceSchedule(
                    tenant_id=tenant.id,
                    name=f"{schedule_name} - {unit.name}",
                    service_kind=kind,
                    admin_unit_id=unit.id,
                    frequency=frequency,
                    days_of_week=list(days),
                    start_time="07:00",
                    end_time="11:00",
                    effective_from=date.today() - timedelta(days=30),
                    visibility=Visibility.PUBLIC,
                )
            )

    await session.flush()
    logger.info("demo_tenant_seeded", slug=slug, admin=admin_email)
    return tenant


async def seed_demo_assets(
    session: AsyncSession, tenant: Municipality, count: int = 40
) -> int:
    """Add sample assets so the registry and map are not empty in a demo."""
    from civicos.domain.assets import Asset  # noqa: PLC0415

    units = (
        await session.scalars(select(AdminUnit).where(AdminUnit.tenant_id == tenant.id))
    ).all()
    if not units:
        return 0

    types = [AssetType.STREETLIGHT, AssetType.DRAIN, AssetType.WASTE_BIN, AssetType.PARK]
    created = 0
    for index in range(count):
        asset_type = types[index % len(types)]
        unit = units[index % len(units)]
        code = f"{asset_type.value[:3].upper()}-{index + 1:04d}"
        if await session.scalar(
            select(Asset).where(Asset.tenant_id == tenant.id, Asset.code == code)
        ):
            continue
        session.add(
            Asset(
                tenant_id=tenant.id,
                code=code,
                name=f"{asset_type.value.replace('_', ' ').title()} {index + 1}",
                asset_type=asset_type,
                admin_unit_id=unit.id,
                inspection_interval_days=180,
                qr_payload=f"civicos:{tenant.slug}:asset:{code}",
            )
        )
        created += 1
    await session.flush()
    return created


async def tenant_exists(session: AsyncSession, slug: str) -> bool:
    return (
        await session.scalar(select(Municipality.id).where(Municipality.slug == slug))
    ) is not None


async def ensure_superadmin(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    email: str,
    password: str,
    full_name: str = "Platform Administrator",
) -> User:
    """Create or promote the platform administrator account."""
    user = await session.scalar(
        select(User).where(User.tenant_id == tenant_id, User.email == email.lower())
    )
    if user is None:
        user = User(
            tenant_id=tenant_id,
            email=email.lower(),
            full_name=full_name,
            role=Role.SUPER_ADMIN,
            password_hash=hash_password(password),
            is_superadmin=True,
            is_verified=True,
        )
        session.add(user)
    else:
        user.role = Role.SUPER_ADMIN
        user.is_superadmin = True
        user.password_hash = hash_password(password)
    await session.flush()
    return user
