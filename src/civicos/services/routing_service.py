"""Routing: which department owns this, which ward is it in, which crew goes.

Routing is rules-first by design. The category taxonomy already encodes
department ownership, and a town's own boundary polygons already encode ward
membership; the model is only consulted when the rules are silent. That keeps
routing explainable ("it went to Sanitation because the category says so"),
which is what a supervisor needs when a resident asks why nobody came.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.clock import utcnow
from civicos.core.geo import Point, haversine_meters, point_in_polygon
from civicos.core.permissions import Role
from civicos.domain.enums import IssueStatus, Priority, UserStatus, WorkOrderStatus
from civicos.domain.identity import User
from civicos.domain.issues import Issue
from civicos.domain.tenancy import AdminUnit, Department, IssueCategory
from civicos.domain.workorders import Crew, WorkOrder

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RoutingDecision:
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    reason: str = ""

    def describe(self) -> str:
        return self.reason or "No routing rule matched."


async def resolve_admin_unit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    latitude: float | None,
    longitude: float | None,
) -> AdminUnit | None:
    """Place a coordinate inside a ward / union council.

    Uses uploaded boundary polygons when a tenant has them, and falls back to
    the nearest unit centroid when it does not - which is the common case for a
    town that has GPS points but no GIS department.
    """
    if latitude is None or longitude is None:
        return None

    point = Point(latitude, longitude)
    units = (
        await session.scalars(
            select(AdminUnit).where(AdminUnit.tenant_id == tenant_id, AdminUnit.is_active.is_(True))
        )
    ).all()
    if not units:
        return None

    for unit in units:
        polygon = _as_polygon(unit.boundary)
        if polygon and point_in_polygon(point, polygon):
            return unit

    with_centres = [
        (unit, haversine_meters(point, Point(unit.centre_latitude, unit.centre_longitude)))
        for unit in units
        if unit.centre_latitude is not None and unit.centre_longitude is not None
    ]
    if not with_centres:
        return None
    nearest, distance = min(with_centres, key=lambda pair: pair[1])
    # Beyond 5 km a centroid match is meaningless; better to leave it unassigned.
    return nearest if distance <= 5000 else None


def _as_polygon(boundary: list) -> list[tuple[float, float]]:
    polygon: list[tuple[float, float]] = []
    for entry in boundary or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                polygon.append((float(entry[0]), float(entry[1])))
            except (TypeError, ValueError):
                continue
    return polygon


async def resolve_department(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    category: IssueCategory | None,
    suggested_slug: str | None = None,
) -> Department | None:
    """Department ownership: the category first, then the model's suggestion."""
    if category is not None and category.department_id:
        return await session.get(Department, category.department_id)

    if suggested_slug:
        department = await session.scalar(
            select(Department).where(
                Department.tenant_id == tenant_id,
                Department.code == suggested_slug,
                Department.is_active.is_(True),
            )
        )
        if department is not None:
            return department
    return None


async def route_issue(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    issue: Issue,
    *,
    category: IssueCategory | None,
    suggested_department_slug: str | None = None,
    auto_assign: bool = True,
) -> RoutingDecision:
    """Work out department, ward and (optionally) an owner for a new issue."""
    reasons: list[str] = []
    decision = RoutingDecision()

    unit = await resolve_admin_unit(session, tenant_id, issue.latitude, issue.longitude)
    if unit is not None:
        decision.admin_unit_id = unit.id
        reasons.append(f"located in {unit.name}")

    department = await resolve_department(
        session, tenant_id, category=category, suggested_slug=suggested_department_slug
    )
    if department is not None:
        decision.department_id = department.id
        reasons.append(
            f"category '{category.slug}' is owned by {department.name}"
            if category is not None and category.department_id
            else f"routed to {department.name}"
        )

    if auto_assign and department is not None:
        assignee = await pick_assignee(
            session,
            tenant_id,
            department_id=department.id,
            admin_unit_id=decision.admin_unit_id,
        )
        if assignee is not None:
            decision.assignee_id = assignee.id
            reasons.append(f"assigned to {assignee.display()} (lightest open queue)")

    decision.reason = "; ".join(reasons)
    return decision


async def pick_assignee(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    department_id: uuid.UUID,
    admin_unit_id: uuid.UUID | None = None,
) -> User | None:
    """Least-loaded supervisor in the department, preferring the right ward.

    Deliberately simple and transparent: staff can predict it, and it does not
    quietly concentrate work on whoever closes tickets fastest.
    """
    candidates = (
        await session.scalars(
            select(User).where(
                User.tenant_id == tenant_id,
                User.department_id == department_id,
                User.status == UserStatus.ACTIVE,
                User.deleted_at.is_(None),
                User.role.in_([Role.SUPERVISOR, Role.DEPARTMENT_HEAD, Role.FIELD_AGENT]),
            )
        )
    ).all()
    if not candidates:
        return None

    if admin_unit_id is not None:
        ward_staff = [user for user in candidates if user.admin_unit_id == admin_unit_id]
        if ward_staff:
            candidates = ward_staff

    supervisors = [user for user in candidates if user.role is Role.SUPERVISOR]
    pool = supervisors or candidates

    open_counts = dict(
        (
            await session.execute(
                select(Issue.assigned_to_id, func.count())
                .where(
                    Issue.tenant_id == tenant_id,
                    Issue.assigned_to_id.in_([user.id for user in pool]),
                    Issue.status.in_([s for s in IssueStatus if s.is_open]),
                    Issue.deleted_at.is_(None),
                )
                .group_by(Issue.assigned_to_id)
            )
        ).all()
    )
    return min(pool, key=lambda user: open_counts.get(user.id, 0))


async def pick_crew(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    department_id: uuid.UUID | None,
    category_slug: str | None,
    admin_unit_id: uuid.UUID | None,
    priority: Priority = Priority.NORMAL,
) -> Crew | None:
    """Choose a crew by skill, coverage area and remaining capacity today."""
    statement = select(Crew).where(Crew.tenant_id == tenant_id, Crew.is_active.is_(True))
    if department_id:
        statement = statement.where(Crew.department_id == department_id)
    crews = (await session.scalars(statement)).all()
    if not crews:
        return None

    scored: list[tuple[Crew, float]] = []
    load = await _crew_load(session, tenant_id, [crew.id for crew in crews])

    for crew in crews:
        score = 0.0
        if category_slug and category_slug in (crew.skills or []):
            score += 3.0
        if admin_unit_id and str(admin_unit_id) in {
            str(unit) for unit in (crew.coverage_unit_ids or [])
        }:
            score += 2.0
        assigned_today = load.get(crew.id, 0)
        remaining = crew.capacity_per_day - assigned_today
        if remaining <= 0 and priority not in {Priority.EMERGENCY, Priority.URGENT}:
            continue  # full for today, and this can wait
        score += min(remaining, 5) * 0.4
        scored.append((crew, score))

    if not scored:
        return None
    return max(scored, key=lambda pair: pair[1])[0]


async def _crew_load(
    session: AsyncSession, tenant_id: uuid.UUID, crew_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Open work orders per crew scheduled for the next 24 hours."""
    if not crew_ids:
        return {}
    horizon = utcnow() + timedelta(hours=24)
    rows = (
        await session.execute(
            select(WorkOrder.crew_id, func.count())
            .where(
                WorkOrder.tenant_id == tenant_id,
                WorkOrder.crew_id.in_(list(crew_ids)),
                WorkOrder.status.in_(
                    [
                        WorkOrderStatus.SCHEDULED,
                        WorkOrderStatus.DISPATCHED,
                        WorkOrderStatus.IN_PROGRESS,
                    ]
                ),
                WorkOrder.deleted_at.is_(None),
                WorkOrder.scheduled_for <= horizon,
            )
            .group_by(WorkOrder.crew_id)
        )
    ).all()
    return {crew_id: int(count) for crew_id, count in rows if crew_id}


async def match_category(
    session: AsyncSession, tenant_id: uuid.UUID, slug: str | None
) -> IssueCategory | None:
    if not slug:
        return None
    return await session.scalar(
        select(IssueCategory).where(
            IssueCategory.tenant_id == tenant_id,
            IssueCategory.slug == slug,
            IssueCategory.is_active.is_(True),
        )
    )


async def taxonomy_for_prompt(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[dict[str, object]]:
    """The tenant's live taxonomy, shaped for the triage prompt."""
    rows = (
        await session.execute(
            select(IssueCategory, Department)
            .outerjoin(Department, IssueCategory.department_id == Department.id)
            .where(
                IssueCategory.tenant_id == tenant_id,
                IssueCategory.is_active.is_(True),
            )
            .order_by(IssueCategory.display_order)
        )
    ).all()
    return [
        {
            "slug": category.slug,
            "name": category.name,
            "keywords": list(category.keywords or []),
            "ai_hints": category.ai_hints,
            "department": department.name if department else None,
        }
        for category, department in rows
    ]
