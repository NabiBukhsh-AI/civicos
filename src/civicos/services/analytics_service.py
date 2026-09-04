"""Analytics: the numbers a municipality is actually judged on.

Everything here is computed in SQL and returned as plain data. The AI layer may
later turn these figures into prose (see :mod:`civicos.ai.briefing`), but it
never produces the figures themselves - that separation is what makes a
generated briefing safe to hand to a council.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.clock import day_range, ensure_utc, utcnow
from civicos.core.geo import cell_center, grid_cell
from civicos.domain.enums import (
    IssueStatus,
    Priority,
    ServiceApplicationStatus,
    SLAState,
    WorkOrderStatus,
)
from civicos.domain.issues import Issue
from civicos.domain.knowledge import AIUsage
from civicos.domain.operations import DailyMetric
from civicos.domain.services import ServiceApplication
from civicos.domain.tenancy import AdminUnit, Department, IssueCategory
from civicos.domain.workorders import WorkOrder

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class Hotspot:
    latitude: float
    longitude: float
    count: int
    radius_meters: int
    top_category: str | None = None
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": round(self.latitude, 6),
            "longitude": round(self.longitude, 6),
            "count": self.count,
            "radius_meters": self.radius_meters,
            "top_category": self.top_category,
            "label": self.label,
        }


async def dashboard(
    session: AsyncSession, tenant_id: uuid.UUID, *, days: int = 30
) -> dict[str, Any]:
    """The headline operational picture."""
    since, _ = day_range(days)

    totals = await _issue_totals(session, tenant_id, since)
    sla = await sla_summary(session, tenant_id, since)
    categories = await breakdown(session, tenant_id, IssueCategory, since=since)
    departments = await department_performance(session, tenant_id, since)
    channels = await _channel_breakdown(session, tenant_id, since)
    workload = await _work_order_totals(session, tenant_id, since)
    oldest = await _oldest_open_days(session, tenant_id)

    return {
        "period_days": days,
        "generated_at": utcnow().isoformat(),
        "totals": totals,
        "sla": sla,
        "top_categories": categories,
        "departments": departments,
        "channels": channels,
        "work_orders": workload,
        "oldest_open_days": oldest,
    }


async def _issue_totals(
    session: AsyncSession, tenant_id: uuid.UUID, since: datetime
) -> dict[str, Any]:
    created = await _count(session, Issue, tenant_id, Issue.created_at >= since)
    resolved = await _count(
        session, Issue, tenant_id, Issue.resolved_at.is_not(None), Issue.resolved_at >= since
    )
    open_now = await _count(
        session, Issue, tenant_id, Issue.status.in_([s for s in IssueStatus if s.is_open])
    )
    unassigned = await _count(
        session,
        Issue,
        tenant_id,
        Issue.assigned_to_id.is_(None),
        Issue.status.in_([s for s in IssueStatus if s.is_open]),
    )
    emergencies = await _count(
        session,
        Issue,
        tenant_id,
        Issue.priority == Priority.EMERGENCY,
        Issue.status.in_([s for s in IssueStatus if s.is_open]),
    )

    from civicos.repositories.issues import IssueRepository

    stats = await IssueRepository(session, tenant_id).resolution_stats(since)
    return {
        "issues_created": created,
        "issues_resolved": resolved,
        "open_issues": open_now,
        "unassigned_issues": unassigned,
        "open_emergencies": emergencies,
        "resolution_rate": round(resolved / created, 4) if created else 0.0,
        **stats,
    }


async def sla_summary(
    session: AsyncSession, tenant_id: uuid.UUID, since: datetime
) -> dict[str, Any]:
    """Compliance counts for both SLA stages."""
    rows = (
        await session.execute(
            select(Issue.sla_response_state, Issue.sla_resolution_state, func.count())
            .where(
                Issue.tenant_id == tenant_id,
                Issue.deleted_at.is_(None),
                Issue.created_at >= since,
            )
            .group_by(Issue.sla_response_state, Issue.sla_resolution_state)
        )
    ).all()

    counters: dict[str, int] = defaultdict(int)
    for response_state, resolution_state, count in rows:
        counters[f"response_{_state_key(response_state)}"] += count
        counters[f"resolution_{_state_key(resolution_state)}"] += count

    def rate(prefix: str) -> float:
        met = counters.get(f"{prefix}_met", 0)
        breached = counters.get(f"{prefix}_breached", 0)
        decided = met + breached
        return round(met / decided, 4) if decided else 1.0

    return {
        "response_met": counters.get("response_met", 0),
        "response_breached": counters.get("response_breached", 0),
        "response_at_risk": counters.get("response_at_risk", 0),
        "response_compliance": rate("response"),
        "resolution_met": counters.get("resolution_met", 0),
        "resolution_breached": counters.get("resolution_breached", 0),
        "resolution_at_risk": counters.get("resolution_at_risk", 0),
        "resolution_compliance": rate("resolution"),
    }


def _state_key(state: Any) -> str:
    value = state.value if isinstance(state, SLAState) else str(state)
    return value.replace("-", "_")


async def breakdown(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    dimension: type,
    *,
    since: datetime | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Issue counts grouped by category, department or admin unit."""
    column, label_column = {
        IssueCategory: (Issue.category_id, IssueCategory.name),
        Department: (Issue.department_id, Department.name),
        AdminUnit: (Issue.admin_unit_id, AdminUnit.name),
    }[dimension]

    statement = (
        select(column, label_column, func.count())
        .select_from(Issue)
        .outerjoin(dimension, column == dimension.id)
        .where(Issue.tenant_id == tenant_id, Issue.deleted_at.is_(None))
        .group_by(column, label_column)
        .order_by(func.count().desc())
        .limit(limit)
    )
    if since is not None:
        statement = statement.where(Issue.created_at >= since)

    rows = (await session.execute(statement)).all()
    return [
        {
            "id": str(identifier) if identifier else None,
            "label": label or "Unassigned",
            "count": int(count or 0),
        }
        for identifier, label, count in rows
    ]


async def department_performance(
    session: AsyncSession, tenant_id: uuid.UUID, since: datetime
) -> list[dict[str, Any]]:
    """Per-department volume, backlog and SLA compliance."""
    rows = (
        await session.execute(
            select(
                Department.id,
                Department.name,
                Issue.status,
                Issue.sla_resolution_state,
                func.count(),
            )
            .select_from(Issue)
            .join(Department, Issue.department_id == Department.id)
            .where(
                Issue.tenant_id == tenant_id,
                Issue.deleted_at.is_(None),
                Issue.created_at >= since,
            )
            .group_by(Department.id, Department.name, Issue.status, Issue.sla_resolution_state)
        )
    ).all()

    aggregate: dict[str, dict[str, Any]] = {}
    for department_id, name, status, sla_state, count in rows:
        entry = aggregate.setdefault(
            str(department_id),
            {
                "department_id": str(department_id),
                "name": name,
                "total": 0,
                "open": 0,
                "resolved": 0,
                "sla_met": 0,
                "sla_breached": 0,
            },
        )
        entry["total"] += count
        if isinstance(status, IssueStatus) and status.is_open:
            entry["open"] += count
        if status in {IssueStatus.RESOLVED, IssueStatus.VERIFIED, IssueStatus.CLOSED}:
            entry["resolved"] += count
        if sla_state is SLAState.MET:
            entry["sla_met"] += count
        elif sla_state is SLAState.BREACHED:
            entry["sla_breached"] += count

    result = list(aggregate.values())
    for entry in result:
        decided = entry["sla_met"] + entry["sla_breached"]
        entry["sla_compliance"] = round(entry["sla_met"] / decided, 4) if decided else 1.0
    result.sort(key=lambda item: item["total"], reverse=True)
    return result


async def _channel_breakdown(
    session: AsyncSession, tenant_id: uuid.UUID, since: datetime
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Issue.channel, func.count())
            .where(
                Issue.tenant_id == tenant_id,
                Issue.deleted_at.is_(None),
                Issue.created_at >= since,
            )
            .group_by(Issue.channel)
            .order_by(func.count().desc())
        )
    ).all()
    return [{"channel": str(channel), "count": int(count)} for channel, count in rows]


async def _work_order_totals(
    session: AsyncSession, tenant_id: uuid.UUID, since: datetime
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(WorkOrder.status, func.count())
            .where(
                WorkOrder.tenant_id == tenant_id,
                WorkOrder.deleted_at.is_(None),
                WorkOrder.created_at >= since,
            )
            .group_by(WorkOrder.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    open_statuses = {
        str(WorkOrderStatus.DRAFT),
        str(WorkOrderStatus.SCHEDULED),
        str(WorkOrderStatus.DISPATCHED),
        str(WorkOrderStatus.IN_PROGRESS),
        str(WorkOrderStatus.BLOCKED),
    }
    return {
        "by_status": counts,
        "open": sum(count for status, count in counts.items() if status in open_statuses),
        "completed": counts.get(str(WorkOrderStatus.COMPLETED), 0)
        + counts.get(str(WorkOrderStatus.VERIFIED), 0),
    }


async def _oldest_open_days(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    oldest = await session.scalar(
        select(func.min(Issue.created_at)).where(
            Issue.tenant_id == tenant_id,
            Issue.deleted_at.is_(None),
            Issue.status.in_([s for s in IssueStatus if s.is_open]),
        )
    )
    if oldest is None:
        return 0
    return max(0, (utcnow() - ensure_utc(oldest)).days)


async def hotspots(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    days: int = 30,
    cell_meters: int = 250,
    min_count: int = 3,
    limit: int = 20,
) -> list[Hotspot]:
    """Grid-bin geotagged reports to find recurring trouble spots.

    Deliberately grid-based rather than a clustering algorithm: the cells are
    stable between runs, so "Ward 7 grid still hot this week" is a comparison a
    supervisor can trust, and it costs one indexed query.
    """
    since, _ = day_range(days)
    rows = (
        await session.execute(
            select(Issue.latitude, Issue.longitude, IssueCategory.name)
            .select_from(Issue)
            .outerjoin(IssueCategory, Issue.category_id == IssueCategory.id)
            .where(
                Issue.tenant_id == tenant_id,
                Issue.deleted_at.is_(None),
                Issue.latitude.is_not(None),
                Issue.longitude.is_not(None),
                Issue.created_at >= since,
                Issue.status.not_in([IssueStatus.DUPLICATE, IssueStatus.REJECTED]),
            )
        )
    ).all()

    buckets: dict[tuple[int, int], list[str | None]] = defaultdict(list)
    for latitude, longitude, category_name in rows:
        buckets[grid_cell(latitude, longitude, cell_meters)].append(category_name)

    results: list[Hotspot] = []
    for cell, categories in buckets.items():
        if len(categories) < min_count:
            continue
        named = [name for name in categories if name]
        top = max(set(named), key=named.count) if named else None
        centre = cell_center(cell, cell_meters, reference_latitude=0.0)
        results.append(
            Hotspot(
                latitude=centre.latitude,
                longitude=centre.longitude,
                count=len(categories),
                radius_meters=cell_meters,
                top_category=top,
            )
        )

    results.sort(key=lambda hotspot: hotspot.count, reverse=True)
    return results[:limit]


async def trend(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Daily created/resolved series for the dashboard chart."""
    since, _ = day_range(days)
    created_rows = (
        await session.execute(
            select(func.date(Issue.created_at), func.count())
            .where(
                Issue.tenant_id == tenant_id,
                Issue.deleted_at.is_(None),
                Issue.created_at >= since,
            )
            .group_by(func.date(Issue.created_at))
        )
    ).all()
    resolved_rows = (
        await session.execute(
            select(func.date(Issue.resolved_at), func.count())
            .where(
                Issue.tenant_id == tenant_id,
                Issue.deleted_at.is_(None),
                Issue.resolved_at.is_not(None),
                Issue.resolved_at >= since,
            )
            .group_by(func.date(Issue.resolved_at))
        )
    ).all()

    created = {str(day): int(count) for day, count in created_rows if day}
    resolved = {str(day): int(count) for day, count in resolved_rows if day}

    series: list[dict[str, Any]] = []
    cursor = since.date()
    today = utcnow().date()
    while cursor <= today:
        key = cursor.isoformat()
        series.append(
            {
                "date": key,
                "created": created.get(key, 0),
                "resolved": resolved.get(key, 0),
            }
        )
        cursor += timedelta(days=1)
    return series


async def service_summary(
    session: AsyncSession, tenant_id: uuid.UUID, *, days: int = 30
) -> dict[str, Any]:
    """Permit / licence pipeline health."""
    since, _ = day_range(days)
    rows = (
        await session.execute(
            select(ServiceApplication.status, func.count())
            .where(
                ServiceApplication.tenant_id == tenant_id,
                ServiceApplication.deleted_at.is_(None),
                ServiceApplication.created_at >= since,
            )
            .group_by(ServiceApplication.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    decided = counts.get(str(ServiceApplicationStatus.APPROVED), 0) + counts.get(
        str(ServiceApplicationStatus.REJECTED), 0
    )
    submitted = sum(counts.values())
    return {
        "by_status": counts,
        "submitted": submitted,
        "decided": decided,
        "decision_rate": round(decided / submitted, 4) if submitted else 0.0,
    }


async def ai_summary(
    session: AsyncSession, tenant_id: uuid.UUID, *, days: int = 30
) -> dict[str, Any]:
    from civicos.ai.usage import tenant_usage_summary

    return await tenant_usage_summary(session, tenant_id, days=days)


async def rollup_day(
    session: AsyncSession, tenant_id: uuid.UUID, target: date | None = None
) -> DailyMetric:
    """Compute and upsert one day's aggregate row.

    Run nightly. Keeps executive dashboards instant on a multi-year table and
    preserves the series even after old records are archived.
    """
    target = target or (utcnow().date() - timedelta(days=1))
    start = datetime.combine(target, datetime.min.time()).replace(tzinfo=utcnow().tzinfo)
    end = start + timedelta(days=1)

    created = await _count(
        session, Issue, tenant_id, Issue.created_at >= start, Issue.created_at < end
    )
    resolved = await _count(
        session, Issue, tenant_id, Issue.resolved_at >= start, Issue.resolved_at < end
    )
    open_end = await _count(
        session,
        Issue,
        tenant_id,
        Issue.created_at < end,
        Issue.status.in_([s for s in IssueStatus if s.is_open]),
    )
    sla = await sla_summary(session, tenant_id, start)
    ai = await session.execute(
        select(func.count(), func.coalesce(func.sum(AIUsage.estimated_cost_usd), 0.0)).where(
            AIUsage.tenant_id == tenant_id,
            AIUsage.created_at >= start,
            AIUsage.created_at < end,
        )
    )
    ai_calls, ai_cost = ai.first() or (0, 0.0)

    existing = await session.scalar(
        select(DailyMetric).where(
            DailyMetric.tenant_id == tenant_id,
            DailyMetric.metric_date == target,
            DailyMetric.dimension == "overall",
            DailyMetric.dimension_value == "all",
        )
    )
    metric = existing or DailyMetric(
        tenant_id=tenant_id, metric_date=target, dimension="overall", dimension_value="all"
    )
    metric.issues_created = created
    metric.issues_resolved = resolved
    metric.issues_open_end_of_day = open_end
    metric.sla_response_met = sla["response_met"]
    metric.sla_response_breached = sla["response_breached"]
    metric.sla_resolution_met = sla["resolution_met"]
    metric.sla_resolution_breached = sla["resolution_breached"]
    metric.ai_calls = int(ai_calls or 0)
    metric.ai_cost_usd = round(float(ai_cost or 0.0), 4)

    if existing is None:
        session.add(metric)
    await session.flush()
    return metric


async def _count(session: AsyncSession, model: type, tenant_id: uuid.UUID, *conditions: Any) -> int:
    statement = (
        select(func.count())
        .select_from(model)
        .where(model.tenant_id == tenant_id, model.deleted_at.is_(None), *conditions)
    )
    return int(await session.scalar(statement) or 0)
