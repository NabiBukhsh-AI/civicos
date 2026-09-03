"""Analytics and reporting endpoints."""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from civicos.api.deps import SessionDep, TenantDep, require_permission
from civicos.core.clock import utcnow
from civicos.core.permissions import Resource, perm
from civicos.domain.tenancy import AdminUnit, Department, IssueCategory
from civicos.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["Analytics"])

READ = perm(Resource.ANALYTICS, "read")
EXPORT = perm(Resource.ANALYTICS, "export")


@router.get("/dashboard", response_model=dict, dependencies=[Depends(require_permission(READ))])
async def dashboard(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """The operational overview: volume, SLA compliance, backlog, workload."""
    return await analytics_service.dashboard(session, tenant.id, days=days)


@router.get("/trend", response_model=list, dependencies=[Depends(require_permission(READ))])
async def trend(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=7, le=365)] = 30,
) -> list[dict[str, Any]]:
    """Daily created-vs-resolved series. The clearest signal of whether the
    backlog is growing or shrinking."""
    return await analytics_service.trend(session, tenant.id, days=days)


@router.get("/hotspots", response_model=list, dependencies=[Depends(require_permission(READ))])
async def hotspots(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    cell_meters: Annotated[int, Query(ge=50, le=2000)] = 250,
    min_count: Annotated[int, Query(ge=2, le=100)] = 3,
) -> list[dict[str, Any]]:
    """Geographic concentrations of reports - where to send a standing crew."""
    results = await analytics_service.hotspots(
        session, tenant.id, days=days, cell_meters=cell_meters, min_count=min_count
    )
    return [hotspot.to_dict() for hotspot in results]


@router.get("/breakdown", response_model=list, dependencies=[Depends(require_permission(READ))])
async def breakdown(
    session: SessionDep,
    tenant: TenantDep,
    dimension: Annotated[str, Query(pattern="^(category|department|admin_unit)$")] = "category",
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    from datetime import timedelta  # noqa: PLC0415

    model = {
        "category": IssueCategory,
        "department": Department,
        "admin_unit": AdminUnit,
    }[dimension]
    return await analytics_service.breakdown(
        session, tenant.id, model, since=utcnow() - timedelta(days=days), limit=limit
    )


@router.get("/sla", response_model=dict, dependencies=[Depends(require_permission(READ))])
async def sla(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    from datetime import timedelta  # noqa: PLC0415

    return await analytics_service.sla_summary(
        session, tenant.id, utcnow() - timedelta(days=days)
    )


@router.get("/departments", response_model=list, dependencies=[Depends(require_permission(READ))])
async def departments(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> list[dict[str, Any]]:
    """Per-department scorecard: volume, backlog and SLA compliance."""
    from datetime import timedelta  # noqa: PLC0415

    return await analytics_service.department_performance(
        session, tenant.id, utcnow() - timedelta(days=days)
    )


@router.get("/services", response_model=dict, dependencies=[Depends(require_permission(READ))])
async def services(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    return await analytics_service.service_summary(session, tenant.id, days=days)


@router.get("/export/issues.csv", dependencies=[Depends(require_permission(EXPORT))])
async def export_issues(
    session: SessionDep,
    tenant: TenantDep,
    days: Annotated[int, Query(ge=1, le=1095)] = 90,
) -> StreamingResponse:
    """Stream the issue register as CSV.

    Streamed rather than buffered so a multi-year export does not have to fit
    in memory on a small server.
    """
    from datetime import timedelta  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from civicos.domain.issues import Issue  # noqa: PLC0415
    from civicos.services import audit_service  # noqa: PLC0415
    from civicos.domain.enums import AuditAction  # noqa: PLC0415

    await audit_service.record(
        session,
        action=AuditAction.EXPORT,
        entity_type="issue",
        summary=f"Issue register exported ({days} days)",
        tenant_id=tenant.id,
    )
    await session.commit()

    columns = [
        "reference",
        "title",
        "status",
        "priority",
        "severity",
        "category",
        "department",
        "channel",
        "latitude",
        "longitude",
        "address",
        "confirmations",
        "created_at",
        "first_response_at",
        "resolved_at",
        "resolution_due_at",
        "sla_resolution_state",
        "satisfaction_rating",
    ]

    async def rows() -> Any:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        statement = (
            select(Issue)
            .where(
                Issue.tenant_id == tenant.id,
                Issue.deleted_at.is_(None),
                Issue.created_at >= utcnow() - timedelta(days=days),
            )
            .order_by(Issue.created_at.desc())
            .execution_options(yield_per=500)
        )
        async for issue in await session.stream_scalars(statement):
            writer.writerow(
                [
                    issue.reference,
                    issue.title,
                    str(issue.status),
                    str(issue.priority),
                    str(issue.severity) if issue.severity else "",
                    issue.category.name if issue.category else "",
                    issue.department.name if issue.department else "",
                    str(issue.channel),
                    issue.latitude or "",
                    issue.longitude or "",
                    issue.address or "",
                    issue.confirmations,
                    issue.created_at.isoformat(),
                    issue.first_response_at.isoformat() if issue.first_response_at else "",
                    issue.resolved_at.isoformat() if issue.resolved_at else "",
                    issue.resolution_due_at.isoformat() if issue.resolution_due_at else "",
                    str(issue.sla_resolution_state),
                    issue.satisfaction_rating or "",
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    filename = f"{tenant.slug}-issues-{utcnow():%Y%m%d}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
