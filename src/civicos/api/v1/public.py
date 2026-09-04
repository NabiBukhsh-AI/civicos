"""Public portal and open data.

Everything here is unauthenticated and deliberately redacted. It exists because
transparency is a municipal obligation, not a feature: residents can see what
has been reported, what is scheduled, where money is going and who represents
them, without an account and without exposing anyone's personal data.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select

from civicos.api.deps import SessionDep, TenantDep
from civicos.core.clock import utcnow
from civicos.core.i18n import available_languages
from civicos.domain.budget import BudgetLine, BudgetPeriod, DevelopmentProject
from civicos.domain.engagement import Announcement, EmergencyAlert
from civicos.domain.enums import (
    AnnouncementType,
    IssueStatus,
    ServiceApplicationStatus,
    Visibility,
)
from civicos.domain.issues import Issue
from civicos.domain.services import ServiceSchedule, ServiceType
from civicos.domain.tenancy import AdminUnit, IssueCategory, Representative
from civicos.schemas.issues import PublicIssueOut
from civicos.schemas.operations import (
    AlertOut,
    AnnouncementOut,
    ServiceScheduleOut,
    ServiceTypeOut,
)
from civicos.schemas.tenancy import (
    AdminUnitOut,
    CategoryOut,
    LanguageOut,
    PublicMunicipalityOut,
    RepresentativeOut,
)

router = APIRouter(prefix="/public", tags=["Public Portal"])

#: Public responses are cached at the edge; the data changes by the minute at
#: worst, and an unauthenticated endpoint should never hit the database hard.
_CACHE_CONTROL = "public, max-age=120, stale-while-revalidate=600"


@router.get("/municipality", response_model=PublicMunicipalityOut)
async def municipality(tenant: TenantDep, response: Response) -> PublicMunicipalityOut:
    """Branding, contact details and map defaults for the public portal."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return PublicMunicipalityOut.model_validate(tenant)


@router.get("/languages", response_model=list[LanguageOut])
async def languages(tenant: TenantDep, response: Response) -> list[LanguageOut]:
    response.headers["Cache-Control"] = "public, max-age=3600"
    supported = set(tenant.supported_languages or ["en"])
    return [
        LanguageOut(**entry)
        for entry in available_languages()
        if entry["code"] in supported or not supported
    ]


@router.get("/categories", response_model=list[CategoryOut])
async def categories(
    session: SessionDep, tenant: TenantDep, response: Response
) -> list[CategoryOut]:
    """The reporting taxonomy, for building a "report a problem" form."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    rows = (
        await session.scalars(
            select(IssueCategory)
            .where(IssueCategory.tenant_id == tenant.id, IssueCategory.is_active.is_(True))
            .order_by(IssueCategory.display_order, IssueCategory.name)
        )
    ).all()
    return [CategoryOut.model_validate(row) for row in rows]


@router.get("/admin-units", response_model=list[AdminUnitOut])
async def admin_units(
    session: SessionDep, tenant: TenantDep, response: Response
) -> list[AdminUnitOut]:
    response.headers["Cache-Control"] = _CACHE_CONTROL
    rows = (
        await session.scalars(
            select(AdminUnit)
            .where(AdminUnit.tenant_id == tenant.id, AdminUnit.is_active.is_(True))
            .order_by(AdminUnit.path)
        )
    ).all()
    return [AdminUnitOut.model_validate(row) for row in rows]


@router.get("/representatives", response_model=list[RepresentativeOut])
async def representatives(
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    admin_unit_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[RepresentativeOut]:
    """ "Who represents me" - the directory of elected and appointed officials."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    statement = select(Representative).where(
        Representative.tenant_id == tenant.id, Representative.is_active.is_(True)
    )
    if admin_unit_id:
        statement = statement.where(Representative.admin_unit_id == admin_unit_id)
    rows = (await session.scalars(statement.order_by(Representative.title))).all()
    return [RepresentativeOut.model_validate(row) for row in rows]


@router.get("/issues", response_model=list[PublicIssueOut])
async def public_issues(
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    status_filter: Annotated[IssueStatus | None, Query(alias="status")] = None,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
    admin_unit_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PublicIssueOut]:
    """Reports published on the public map.

    Reporter identity, free-text descriptions and internal notes are excluded -
    only the location, category and status of the problem are published.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    if not tenant.feature_enabled("public_issues"):
        return []

    statement = (
        select(Issue, IssueCategory.name, AdminUnit.name)
        .outerjoin(IssueCategory, Issue.category_id == IssueCategory.id)
        .outerjoin(AdminUnit, Issue.admin_unit_id == AdminUnit.id)
        .where(
            Issue.tenant_id == tenant.id,
            Issue.deleted_at.is_(None),
            Issue.visibility == Visibility.PUBLIC,
            Issue.is_flagged.is_(False),
            Issue.status.not_in([IssueStatus.DUPLICATE, IssueStatus.REJECTED]),
        )
        .order_by(Issue.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        statement = statement.where(Issue.status == status_filter)
    if category_id:
        statement = statement.where(Issue.category_id == category_id)
    if admin_unit_id:
        statement = statement.where(Issue.admin_unit_id == admin_unit_id)

    rows = (await session.execute(statement)).all()
    return [
        PublicIssueOut(
            reference=issue.reference,
            title=issue.title,
            status=issue.status,
            category=category_name,
            latitude=issue.latitude,
            longitude=issue.longitude,
            admin_unit=unit_name,
            confirmations=issue.confirmations,
            created_at=issue.created_at,
            resolved_at=issue.resolved_at,
        )
        for issue, category_name, unit_name in rows
    ]


@router.get("/issues/{reference}/status", response_model=dict)
async def track_report(reference: str, session: SessionDep, tenant: TenantDep) -> dict[str, Any]:
    """Track a report by its reference code - the anonymous reporter's lifeline."""
    from civicos.core.errors import NotFoundError
    from civicos.repositories.issues import IssueRepository

    issue = await IssueRepository(session, tenant.id).get_by_reference(reference)
    if issue is None:
        raise NotFoundError("No report with that reference.", code="issue_not_found")

    return {
        "reference": issue.reference,
        "status": str(issue.status),
        "category": issue.category.name if issue.category else None,
        "department": issue.department.name if issue.department else None,
        "created_at": issue.created_at.isoformat(),
        "resolution_due_at": (
            issue.resolution_due_at.isoformat() if issue.resolution_due_at else None
        ),
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "resolution_note": issue.resolution_note,
        "confirmations": issue.confirmations,
        "merged_into": str(issue.duplicate_of_id) if issue.duplicate_of_id else None,
    }


@router.get("/announcements", response_model=list[AnnouncementOut])
async def announcements(
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    announcement_type: Annotated[AnnouncementType | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[AnnouncementOut]:
    response.headers["Cache-Control"] = _CACHE_CONTROL
    now = utcnow()
    statement = (
        select(Announcement)
        .where(
            Announcement.tenant_id == tenant.id,
            Announcement.deleted_at.is_(None),
            Announcement.visibility == Visibility.PUBLIC,
            Announcement.published_at.is_not(None),
            Announcement.published_at <= now,
        )
        .order_by(Announcement.is_pinned.desc(), Announcement.published_at.desc())
        .limit(limit)
    )
    if announcement_type:
        statement = statement.where(Announcement.announcement_type == announcement_type)
    rows = (await session.scalars(statement)).all()
    return [
        AnnouncementOut.model_validate(row)
        for row in rows
        if row.expires_at is None or row.expires_at > now
    ]


@router.get("/alerts", response_model=list[AlertOut])
async def active_alerts(
    session: SessionDep, tenant: TenantDep, response: Response
) -> list[AlertOut]:
    """Live emergency alerts. Never cached for long - this is safety information."""
    response.headers["Cache-Control"] = "public, max-age=30"
    rows = (
        await session.scalars(
            select(EmergencyAlert)
            .where(
                EmergencyAlert.tenant_id == tenant.id,
                EmergencyAlert.is_active.is_(True),
                EmergencyAlert.cancelled_at.is_(None),
            )
            .order_by(EmergencyAlert.issued_at.desc())
        )
    ).all()
    now = utcnow()
    return [
        AlertOut.model_validate(row)
        for row in rows
        if row.expires_at is None or row.expires_at > now
    ]


@router.get("/services", response_model=list[ServiceTypeOut])
async def services(
    session: SessionDep, tenant: TenantDep, response: Response
) -> list[ServiceTypeOut]:
    """The service catalogue: what can be applied for, what it costs, how long."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    rows = (
        await session.scalars(
            select(ServiceType)
            .where(ServiceType.tenant_id == tenant.id, ServiceType.is_active.is_(True))
            .order_by(ServiceType.display_order, ServiceType.name)
        )
    ).all()
    return [ServiceTypeOut.model_validate(row) for row in rows]


@router.get("/schedules", response_model=list[ServiceScheduleOut])
async def schedules(
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    admin_unit_id: Annotated[uuid.UUID | None, Query()] = None,
    service_kind: Annotated[str | None, Query(max_length=64)] = None,
) -> list[ServiceScheduleOut]:
    """When waste is collected, water is supplied, drains are cleaned.

    Publishing the round removes a whole class of complaints and makes a missed
    round visible - and therefore reportable.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    statement = select(ServiceSchedule).where(
        ServiceSchedule.tenant_id == tenant.id,
        ServiceSchedule.is_active.is_(True),
        ServiceSchedule.visibility == Visibility.PUBLIC,
    )
    if admin_unit_id:
        statement = statement.where(ServiceSchedule.admin_unit_id == admin_unit_id)
    if service_kind:
        statement = statement.where(ServiceSchedule.service_kind == service_kind)
    rows = (await session.scalars(statement.order_by(ServiceSchedule.name))).all()
    return [ServiceScheduleOut.model_validate(row) for row in rows]


@router.get("/budget", response_model=dict)
async def budget(session: SessionDep, tenant: TenantDep, response: Response) -> dict[str, Any]:
    """Published budget: allocation and spend by head, for the current period."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    period = await session.scalar(
        select(BudgetPeriod).where(
            BudgetPeriod.tenant_id == tenant.id,
            BudgetPeriod.is_current.is_(True),
            BudgetPeriod.is_published.is_(True),
        )
    )
    if period is None:
        return {"period": None, "lines": [], "message": "No published budget."}

    lines = (
        await session.scalars(
            select(BudgetLine)
            .where(BudgetLine.period_id == period.id)
            .order_by(BudgetLine.allocated_amount.desc())
        )
    ).all()
    return {
        "period": {
            "code": period.code,
            "name": period.name,
            "starts_on": period.starts_on.isoformat(),
            "ends_on": period.ends_on.isoformat(),
            "currency": period.currency_code,
            "total_allocated": period.total_allocated,
            "total_spent": period.total_spent,
            "utilisation": period.utilisation,
        },
        "lines": [
            {
                "code": line.code,
                "name": line.name,
                "category": str(line.category),
                "allocated": line.effective_allocation,
                "spent": line.spent_amount,
                "utilisation": line.utilisation,
            }
            for line in lines
        ],
    }


@router.get("/projects", response_model=list[dict])
async def projects(
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    admin_unit_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Development schemes and their progress, by ward."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    statement = select(DevelopmentProject).where(
        DevelopmentProject.tenant_id == tenant.id,
        DevelopmentProject.deleted_at.is_(None),
        DevelopmentProject.visibility == Visibility.PUBLIC,
    )
    if admin_unit_id:
        statement = statement.where(DevelopmentProject.admin_unit_id == admin_unit_id)
    rows = (await session.scalars(statement.order_by(DevelopmentProject.created_at.desc()))).all()
    return [
        {
            "code": project.code,
            "name": project.name,
            "description": project.description,
            "status": str(project.status),
            "estimated_cost": project.estimated_cost,
            "spent_amount": project.spent_amount,
            "progress_percent": project.progress_percent,
            "contractor_name": project.contractor_name,
            "planned_start": project.planned_start.isoformat() if project.planned_start else None,
            "planned_end": project.planned_end.isoformat() if project.planned_end else None,
            "is_delayed": project.is_delayed,
            "latitude": project.latitude,
            "longitude": project.longitude,
            "beneficiaries": project.beneficiaries,
        }
        for project in rows
    ]


@router.get("/stats", response_model=dict)
async def public_stats(
    session: SessionDep, tenant: TenantDep, response: Response
) -> dict[str, Any]:
    """Headline performance figures, published for accountability.

    A municipality that publishes its own resolution rate is one that can be
    held to it - which is the point.
    """
    response.headers["Cache-Control"] = _CACHE_CONTROL
    from datetime import timedelta

    since = utcnow() - timedelta(days=30)
    created = await session.scalar(
        select(func.count())
        .select_from(Issue)
        .where(
            Issue.tenant_id == tenant.id,
            Issue.deleted_at.is_(None),
            Issue.created_at >= since,
        )
    )
    resolved = await session.scalar(
        select(func.count())
        .select_from(Issue)
        .where(
            Issue.tenant_id == tenant.id,
            Issue.deleted_at.is_(None),
            Issue.resolved_at.is_not(None),
            Issue.resolved_at >= since,
        )
    )
    open_now = await session.scalar(
        select(func.count())
        .select_from(Issue)
        .where(
            Issue.tenant_id == tenant.id,
            Issue.deleted_at.is_(None),
            Issue.status.in_([s for s in IssueStatus if s.is_open]),
        )
    )
    applications = await session.scalar(
        select(func.count())
        .select_from(ServiceType)
        .where(ServiceType.tenant_id == tenant.id, ServiceType.is_active.is_(True))
    )
    return {
        "period_days": 30,
        "reports_received": int(created or 0),
        "reports_resolved": int(resolved or 0),
        "reports_open": int(open_now or 0),
        "resolution_rate": round((resolved or 0) / created, 4) if created else 0.0,
        "services_offered": int(applications or 0),
        "generated_at": utcnow().isoformat(),
    }


@router.get("/open-data/issues.geojson", response_model=dict)
async def issues_geojson(
    session: SessionDep,
    tenant: TenantDep,
    response: Response,
    days: Annotated[int, Query(ge=1, le=365)] = 90,
) -> dict[str, Any]:
    """Open-data export in GeoJSON, ready for QGIS or a mapping library."""
    response.headers["Cache-Control"] = _CACHE_CONTROL
    from datetime import timedelta

    if not tenant.feature_enabled("open_data"):
        return {"type": "FeatureCollection", "features": []}

    since = utcnow() - timedelta(days=days)
    rows = (
        await session.execute(
            select(Issue, IssueCategory.name)
            .outerjoin(IssueCategory, Issue.category_id == IssueCategory.id)
            .where(
                Issue.tenant_id == tenant.id,
                Issue.deleted_at.is_(None),
                Issue.visibility == Visibility.PUBLIC,
                Issue.is_flagged.is_(False),
                Issue.latitude.is_not(None),
                Issue.created_at >= since,
                Issue.status.not_in([IssueStatus.DUPLICATE, IssueStatus.REJECTED]),
            )
            .limit(5000)
        )
    ).all()

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [issue.longitude, issue.latitude],
                },
                "properties": {
                    "reference": issue.reference,
                    "category": category_name,
                    "status": str(issue.status),
                    "priority": str(issue.priority),
                    "confirmations": issue.confirmations,
                    "reported_at": issue.created_at.isoformat(),
                    "resolved_at": (issue.resolved_at.isoformat() if issue.resolved_at else None),
                },
            }
            for issue, category_name in rows
        ],
        "metadata": {
            "municipality": tenant.name,
            "generated_at": utcnow().isoformat(),
            "period_days": days,
            "licence": "Open data - attribution requested.",
        },
    }


@router.get("/service-status/{reference}", response_model=dict)
async def track_application(
    reference: str, session: SessionDep, tenant: TenantDep
) -> dict[str, Any]:
    """Track a permit or licence application by its reference."""
    from civicos.core.errors import NotFoundError
    from civicos.domain.services import ServiceApplication

    application = await session.scalar(
        select(ServiceApplication).where(
            ServiceApplication.tenant_id == tenant.id,
            ServiceApplication.reference == reference.upper(),
        )
    )
    if application is None:
        raise NotFoundError("No application with that reference.", code="application_not_found")
    return {
        "reference": application.reference,
        "service": application.service_type.name if application.service_type else None,
        "status": str(application.status),
        "submitted_at": (
            application.submitted_at.isoformat() if application.submitted_at else None
        ),
        "due_at": application.due_at.isoformat() if application.due_at else None,
        "decided_at": application.decided_at.isoformat() if application.decided_at else None,
        "info_request": application.info_request,
        "certificate_number": application.certificate_number,
        "valid_until": application.valid_until.isoformat() if application.valid_until else None,
        "is_overdue": application.is_overdue,
        "awaiting_applicant": application.status is ServiceApplicationStatus.INFO_REQUIRED,
    }
